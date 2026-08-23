"""
services/city_service.py — City discovery, bounds, and per-city metadata.
"""
import logging
from functools import lru_cache
from pathlib import Path
from typing import Dict, Any, List

from config import PATHS, CITY_REGISTRY, DEFAULT_CITY
from utils.gis_loader import load_layer

logger = logging.getLogger("drmp.city")

# generate_all_stps.py writes one JSON file per city here — this is the real,
# current source of STP data. CITY_REGISTRY's stp_shp/stp_csv paths are a
# legacy, largely-unpopulated convention from before that script existed and
# are kept only for the two cities that still define them.
STP_DATA_DIR = Path(__file__).parent.parent / "stp_data"


@lru_cache(maxsize=1)
def _cities_with_stp_json() -> set:
    """City names that have a non-empty pre-generated STP JSON file."""
    found = set()
    if not STP_DATA_DIR.exists():
        return found
    for f in STP_DATA_DIR.glob("*.json"):
        if f.stem == "_summary":
            continue
        found.add(f.stem)
    return found


@lru_cache(maxsize=1)
def discover_cities() -> List[str]:
    """
    Discover all ULB names present in the wards shapefile.
    This is the source of truth for "every city available in the dataset".
    """
    try:
        gdf = load_layer(str(PATHS["wards_sewage"]))
        col = next((c for c in ("ub_nm_e", "ulb_nm", "ulbname") if c in gdf.columns), None)
        if not col:
            return list(CITY_REGISTRY.keys())
        names = sorted(gdf[col].dropna().unique().tolist())
        return names
    except Exception as e:
        logger.warning("City discovery failed, falling back to registry: %s", e)
        return list(CITY_REGISTRY.keys())


def list_cities() -> List[Dict[str, Any]]:
    """
    Return city list with metadata: whether STP analysis output exists for it.
    """
    all_cities = discover_cities()
    stp_cities = _cities_with_stp_json()
    result = []
    for name in all_cities:
        reg = CITY_REGISTRY.get(name)
        # Prefer the JSON output from generate_all_stps.py — that is what
        # /api/stp/<city> actually serves. Fall back to the legacy shapefile
        # path for any city registered the old way but not yet in stp_data/.
        has_stp = name in stp_cities or bool(reg and reg["stp_shp"].exists())
        result.append({
            "name": name,
            "display_name": reg["display_name"] if reg else name,
            "has_stp_analysis": has_stp,
            "is_default": name == DEFAULT_CITY,
        })
    # Cities with STP analysis float to top
    result.sort(key=lambda c: (not c["has_stp_analysis"], c["name"]))
    return result


def get_city_bounds(city: str) -> Dict[str, float]:
    """
    Tight bounding box for a city's wards, with small padding —
    used to lock/fit the map to that city only.
    """
    gdf = load_layer(str(PATHS["wards_sewage"]), city)
    if gdf.empty:
        raise ValueError(f"No ward data found for city: {city}")
    minx, miny, maxx, maxy = gdf.total_bounds
    pad_x = (maxx - minx) * 0.08 or 0.01
    pad_y = (maxy - miny) * 0.08 or 0.01
    return {
        "minx": float(minx - pad_x), "miny": float(miny - pad_y),
        "maxx": float(maxx + pad_x), "maxy": float(maxy + pad_y),
        "center_lat": float((miny + maxy) / 2),
        "center_lon": float((minx + maxx) / 2),
    }


def validate_city(city: str) -> str:
    """Raise if city is unknown; otherwise return it unchanged."""
    cities = discover_cities()
    if city not in cities:
        raise ValueError(f"Unknown city: '{city}'. Available: {cities}")
    return city
