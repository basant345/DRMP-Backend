"""
services/report_content.py — Content & matching layer for the DRMP report
generator.

This module is ONLY a reader for the static editorial content shipped by
cNARMADA (report_content/drmp_report_content.json + report_content/photos/)
— Part 1 (common) text, Part 2 per-city profile/water-supply/sewerage/
future-plans text, and the computed-line formula. It does not touch GIS
data, STP siting, or ward statistics; those stay exactly as computed by
services/stp_service.py and utils/gis_analysis.py and are only read by
routes/reports.py when it assembles a report.

City matching: the JSON's `app_name` field is the bridge to this app's own
city names (as used by validate_city / discover_cities). Bherunda is
already recorded with app_name "Nasrullaganj" in the content file, so a
plain case-insensitive match on app_name is all that's needed — no special
casing here. The five cities whose GIS data isn't in the app yet
(Narsinghpur, Sainkheda, Sanawad, Anjad, Sendhwa) have app_name = null in
the JSON and so simply never match; they are not special-cased, they are
just absent.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("drmp.report_content")

CONTENT_DIR = Path(__file__).parent.parent / "report_content"
CONTENT_JSON = CONTENT_DIR / "drmp_report_content.json"

_cache: Optional[Dict[str, Any]] = None


def load_content() -> Dict[str, Any]:
    """Load and cache drmp_report_content.json."""
    global _cache
    if _cache is None:
        if not CONTENT_JSON.exists():
            raise FileNotFoundError(
                f"Report content file not found: {CONTENT_JSON}. "
                "Expected report_content/drmp_report_content.json next to the backend app."
            )
        with open(CONTENT_JSON, encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache


def get_common() -> Dict[str, Any]:
    """Part 1 content: title + Chapter 1 (1.1-1.4), identical for every report."""
    return load_content()["common"]


def get_city_record(app_name: str) -> Optional[Dict[str, Any]]:
    """
    Match a city's editorial record by this app's own city name.
    Case-insensitive; cities with app_name=null (GIS data not yet in the app)
    never match, by construction.
    """
    if not app_name:
        return None
    content = load_content()
    target = app_name.strip().lower()
    for c in content["cities"]:
        an = c.get("app_name")
        if an and an.strip().lower() == target:
            return c
    return None


def is_declared_profile_missing(app_name: str) -> bool:
    """True for the 6 app cities the content package explicitly documents as
    'profile not yet available' (Dahi, Harsud, Kasrawad, Khandwa, Punasa,
    Shahganj)."""
    if not app_name:
        return False
    content = load_content()
    declared = content.get("app_cities_without_profile", [])
    return any(app_name.strip().lower() == d.strip().lower() for d in declared)


def photo_path(city_record: Optional[Dict[str, Any]]) -> Optional[Path]:
    """Absolute path to a city's profile photo, or None if it has none / the
    file is missing on disk (never raises — a missing photo just means the
    report skips the image, it must not crash generation)."""
    if not city_record or not city_record.get("photo"):
        return None
    p = CONTENT_DIR / city_record["photo"]
    return p if p.exists() else None


def compute_line(city_record: Dict[str, Any]) -> str:
    """
    The "Computed at 108 lpcd: ..." sentence (DRMP report, §2.2).

    The arithmetic is evaluated from computed_line.formula in the JSON
    itself (sewage_mld / stp_capacity_pct / wtp_lpcd), not copied as literal
    numbers — only this city's own profile.population / wtp_mld / stp_mld
    feed it. Matches drmp_report_content.json's `template` wording and its
    "(below today's estimate)" / missing-field conditionals, verified
    against expected_computed for all 21 cities that have one.
    """
    content = load_content()
    cl = content["computed_line"]
    formula = cl["formula"]

    population = city_record["profile"]["population"]
    wtp_mld = city_record.get("wtp_mld")
    stp_mld = city_record.get("stp_mld")

    ctx: Dict[str, Any] = {"population": population, "wtp_mld": wtp_mld, "stp_mld": stp_mld}
    sewage_mld = eval(formula["sewage_mld"], {"__builtins__": {}}, ctx)  # noqa: S307 - trusted local JSON, not user input
    ctx["sewage_mld"] = sewage_mld

    stp_capacity_pct = None
    if stp_mld is not None and sewage_mld:
        stp_capacity_pct = eval(formula["stp_capacity_pct"], {"__builtins__": {}}, ctx)  # noqa: S307

    wtp_lpcd = None
    if wtp_mld is not None:
        wtp_lpcd = eval(formula["wtp_lpcd"], {"__builtins__": {}}, ctx)  # noqa: S307

    below_clause = " (below today’s estimate)" if (stp_capacity_pct is not None and stp_capacity_pct < 100) else ""

    if stp_capacity_pct is not None:
        line = (
            f"Computed at 108 lpcd: sewage ≈ {sewage_mld:.2f} MLD; "
            f"STP capacity is {stp_capacity_pct:.0f}% of this{below_clause}."
        )
    else:
        line = f"Computed at 108 lpcd: sewage ≈ {sewage_mld:.2f} MLD."

    if wtp_lpcd is not None:
        line += f" WTP capacity ≈ {wtp_lpcd:.0f} litres per person per day."

    return line


# ─────────────────────────────────────────────────────────────────────────────
# T6 / T7 — key findings & recommendations (DRMP Auto-Report Specification
# v1.0/1.1, §5). Only the bullets whose inputs genuinely exist in this app
# are generated; the rest of each template (built-up-area change, land-use
# change, % unsewered, 2050 design-year population/sewage, "waterbodies
# proposed") requires data this app does not compute anywhere, and is
# skipped rather than fabricated.
# ─────────────────────────────────────────────────────────────────────────────

def key_findings(city_record: Dict[str, Any], stp_summary: Optional[Dict[str, Any]]) -> List[str]:
    """T6 bullets this app can actually back with real numbers."""
    bullets: List[str] = []

    sewerage_status = ((city_record.get("sewerage") or {}).get("status") or "").lower()
    if "complete" not in sewerage_status:
        bullets.append(
            "Existing drains convey untreated wastewater directly or indirectly to the Narmada River."
        )

    if stp_summary and stp_summary.get("available") and stp_summary.get("count"):
        bullets.append(
            f"The proposed {stp_summary['count']}-STP system will provide "
            f"{stp_summary['total_capacity_mld']:.2f} MLD of treatment capacity for the areas it covers."
        )

    return bullets


def recommendations(stp_summary: Optional[Dict[str, Any]]) -> List[str]:
    """T7 bullets — the standing recommendations plus the one STP-count bullet
    that needs live data."""
    bullets: List[str] = ["Expand sewerage coverage to currently unserved areas."]

    if stp_summary and stp_summary.get("available") and stp_summary.get("count"):
        bullets.append(
            f"Construct and operationalise all {stp_summary['count']} proposed decentralised STPs."
        )

    bullets += [
        "Intercept sewage from major drains before discharge into the Narmada River.",
        "Integrate flood risk into future infrastructure planning.",
        "Strengthen groundwater monitoring and protect urban water bodies.",
        "Establish a long-term monitoring framework for water quality, wastewater treatment "
        "performance, and river health.",
    ]
    return bullets
