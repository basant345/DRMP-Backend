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
