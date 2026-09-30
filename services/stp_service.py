"""
services/stp_service.py
Reads pre-generated STP analysis JSON files (one per city) produced by
generate_all_stps.py. Falls back to shapefile if a legacy .shp exists.
"""
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Dict, Any, Optional, List

logger = logging.getLogger("drmp.stp")

# ── Path to generated JSON files ──────────────────────────────────────────────
STP_DATA_DIR = Path(__file__).parent.parent / "stp_data"


@lru_cache(maxsize=32)
def load_stp_data(city: str) -> Optional[Dict[str, Any]]:
    """Load the pre-generated STP JSON for a city. Returns None if not found."""
    json_path = STP_DATA_DIR / f"{city}.json"
    if json_path.exists():
        with open(json_path) as f:
            return json.load(f)
    logger.warning("No STP JSON found for city: %s (expected: %s)", city, json_path)
    return None


def get_stp_geojson(city: str) -> Dict[str, Any]:
    """Return STP points as a GeoJSON FeatureCollection."""
    data = load_stp_data(city)
    if not data or not data.get("stps"):
        return {"type": "FeatureCollection", "features": []}

    features = []
    for stp in data["stps"]:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [stp["longitude"], stp["latitude"]],
            },
            "properties": {
                "stp_id":       stp["stp_id"],
                "cluster":      stp["cluster"],
                "Capacity_MLD": stp["Capacity_MLD"],
                "Elevation":    stp["Elevation"],
                "FloodScore":   stp["FloodScore"],
                "FloodClass":   stp.get("FloodClass", "—"),
                "Score":        stp["Score"],
                "latitude":     stp["latitude"],
                "longitude":    stp["longitude"],
                "n_candidates": stp.get("n_candidates", 0),
            },
        })
    return {"type": "FeatureCollection", "features": features}


def get_stp_summary(city: str) -> Dict[str, Any]:
    """Summary statistics for a city's proposed STPs."""
    data = load_stp_data(city)
    if not data or not data.get("stps"):
        return {"city": city, "available": False, "count": 0, "total_capacity_mld": 0}

    stps = data["stps"]
    return {
        "city":               city,
        "available":          True,
        "count":              data["total_stps"],
        "total_capacity_mld": data["total_capacity_mld"],
        "avg_score":          data.get("avg_score"),
        "avg_elevation_m":    data.get("avg_elevation_m"),
        "min_score":          round(min(s["Score"] for s in stps), 4),
        "max_score":          round(max(s["Score"] for s in stps), 4),
    }


def get_stp_table(city: str) -> List[Dict[str, Any]]:
    """Tabular records for the analysis data table / CSV export."""
    data = load_stp_data(city)
    if not data or not data.get("stps"):
        return []
    return data["stps"]


def list_cities_with_stp() -> List[str]:
    """Return list of city names that have generated STP JSON files."""
    return [p.stem for p in STP_DATA_DIR.glob("*.json") if p.stem != "_summary"]


# ─────────────────────────────────────────────────────────────────────────────
# "Suggest N STPs" — user-entered-count feature.
#
# This does NOT touch the existing per-cluster STP methodology above:
# load_stp_data / get_stp_geojson / get_stp_summary / get_stp_table are
# unchanged.
#
# Placement methodology (matches the reference code, STP_Code.docx, exactly):
# for a requested count N, the siting pipeline runs KMeans(n_clusters=N) over
# the city's ward centroids, dissolves the wards into N zones, and keeps only
# the single best-scoring candidate site in each zone. One winner per
# geographically-partitioned zone guarantees the N sites are spread across
# the whole city by construction — the same guarantee the reference's
# original per-cluster pipeline relies on.
#
# generate_stp_candidates.py precomputes this offline for every N from 1 up
# to a city's ward count (the same cap the reference applies to n_clusters),
# using the SAME weights and scoring formula as the existing pipeline
# (elev/flood/sewer/stream/drain/wind) — nothing about the formula changed,
# only the selection rule that decides which candidates become the N sites.
# A previous version of this feature instead built one flat grid over the
# whole city and took the top N by score subject to a 300m minimum-spacing
# rule; that rule did not guarantee city-wide spread and is what was
# producing STPs clustered in one area. It has been removed.
#
# At request time this remains a cached JSON read + slice — no live
# geoprocessing — which keeps it fast and safe on a memory-constrained
# instance. Only the offline generation step changed.
# ─────────────────────────────────────────────────────────────────────────────

CANDIDATES_DIR = Path(__file__).parent.parent / "stp_data" / "candidates"


@lru_cache(maxsize=32)
def load_stp_candidates(city: str) -> Optional[Dict[str, Any]]:
    """Load the pre-generated per-N zonal siting results for a city."""
    json_path = CANDIDATES_DIR / f"{city}_candidates.json"
    if json_path.exists():
        with open(json_path) as f:
            return json.load(f)
    logger.warning("No STP candidate pool found for city: %s (expected: %s)", city, json_path)
    return None


def get_stp_candidate_count(city: str) -> int:
    """
    Largest number of STPs the zonal methodology can site for this city —
    bounded by its number of wards, exactly as the reference caps
    n_clusters at n_wards.
    """
    data = load_stp_candidates(city)
    if not data:
        return 0
    return int(data.get("max_n", 0))


def suggest_top_n_stps(city: str, n: int) -> Dict[str, Any]:
    """
    Return the N zonal-best candidate sites for a city as a result dict:

        {"status": "ok", "city", "requested", "available", "data": <GeoJSON>}
        {"status": "error", "reason": "no_data" | "invalid_count" | "count_too_high",
         "message": <human-readable>, "available": <int>}

    Placement, weights and scoring are entirely inherited from
    generate_stp_candidates.py — this function only validates and looks up
    the precomputed result for the requested N.

    Each returned feature is labelled "STP 1", "STP 2", ... in suitability
    rank order (best first), rather than a numeric candidate id, so the map
    marker/popup reads the same way as the original per-cluster STPs did.
    """
    data = load_stp_candidates(city)
    if not data or not data.get("proposals_by_n"):
        return {
            "status": "error", "reason": "no_data", "available": 0,
            "message": f"No suitable STP locations are available for {city}.",
        }

    available = int(data.get("max_n", 0))

    if not isinstance(n, int) or n <= 0:
        return {
            "status": "error", "reason": "invalid_count", "available": available,
            "message": "Number of STPs must be a positive whole number.",
        }

    records = data["proposals_by_n"].get(str(n))
    if records is None:
        return {
            "status": "error", "reason": "count_too_high", "available": available,
            "message": (
                f"Only {available} suitable location{'s' if available != 1 else ''} "
                f"available for {city}. Please enter a number up to {available}."
            ),
        }

    features = []
    for c in records:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [c["longitude"], c["latitude"]],
            },
            "properties": {
                "stp_id":       f"STP {c['rank']}",
                "rank":         c["rank"],
                "cluster":      c.get("cluster"),
                "Capacity_MLD": c.get("Capacity_MLD"),
                "Elevation":    c["Elevation"],
                "FloodScore":   c["FloodScore"],
                "FloodClass":   c.get("FloodClass", "—"),
                "SewerScore":   c.get("SewerScore"),
                "StreamScore":  c.get("StreamScore"),
                "DrainScore":   c.get("DrainScore"),
                "WindScore":    c.get("WindScore"),
                "Score":        c["Score"],
                "latitude":     c["latitude"],
                "longitude":    c["longitude"],
                "ward_name":    c.get("ward_name", ""),
                "ward_no":      c.get("ward_no", ""),
                "area_name":    c.get("area_name", city),
                "city":         c.get("city", city),
                "n_candidates": available,
            },
        })

    return {
        "status": "ok",
        "city": city,
        "requested": n,
        "available": available,
        "data": {"type": "FeatureCollection", "features": features},
    }

