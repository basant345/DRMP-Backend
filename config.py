"""
config.py â€” Multi-city configuration for DRMP.
Cities are discovered dynamically from the ULB column in shapefiles,
but a CITY_REGISTRY gives per-city overrides (display name, STP output path, etc).
"""
import os
from pathlib import Path

# â”€â”€ Base data directory â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data" / "DRMP" / "Input"))

# â”€â”€ Shared layer paths (filtered by ULB at query time) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
PATHS = {
    "wards_sewage":  DATA_DIR / "ward" / "wards_sewage.shp",
    "mpward":        DATA_DIR / "ward" / "mpward.shp",
    "mpulb":         DATA_DIR / "ulbs" / "mpulb.shp",
    "sewer_network": DATA_DIR / "sewer" / "sewer_network.shp",
    "stream_drain":  DATA_DIR / "Stream&drain.shp",
    "narmada_river": DATA_DIR / "Final_Narmada" / "Merged_Layers_02_07.shp",
    "dem":           DATA_DIR / "Narmada_DEM_Clipped.tif",
}

# â”€â”€ Per-city STP output registry â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Each city's Python suitability-analysis script writes Proposed_STP_Locations.shp
# to its own output folder. Register each city's folder here.
# Add a new entry whenever you run the analysis for a new city.
STP_OUTPUT_DIR = Path(os.getenv("STP_OUTPUT_DIR", DATA_DIR / "stp_outputs"))

CITY_REGISTRY = {
    "Narmadapuram": {
        "display_name": "Narmadapuram",
        "stp_shp": STP_OUTPUT_DIR / "Narmadapuram" / "Proposed_STP_Locations.shp",
        "stp_csv": STP_OUTPUT_DIR / "Narmadapuram" / "STP_Summary.csv",
    },
    "Jabalpur": {
        "display_name": "Jabalpur",
        "stp_shp": DATA_DIR / "Jabalpur_DRMP" / "Proposed_STP_Locations.shp",
        "stp_csv": DATA_DIR / "Jabalpur_DRMP" / "STP_Summary.csv",
    },
    # Add further cities as their STP analysis output becomes available:
    # "Bhopal": {
    #     "display_name": "Bhopal",
    #     "stp_shp": STP_OUTPUT_DIR / "Bhopal" / "Proposed_STP_Locations.shp",
    #     "stp_csv": STP_OUTPUT_DIR / "Bhopal" / "STP_Summary.csv",
    # },
}

DEFAULT_CITY = os.getenv("DEFAULT_CITY", "Narmadapuram")
PRIMARY_EPSG = int(os.getenv("PRIMARY_EPSG", "32644"))   # UTM Zone 44N
WGS84 = 4326

# â”€â”€ STP suitability score weights (mirrors the Python model) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
STP_WEIGHTS = {
    "elevation": 0.50,
    "sewer":     0.20,
    "stream":    0.10,
    "drain":     0.10,
    "flood":     0.10,
}

# â”€â”€ Risk classification thresholds (kept for backward-compat / legacy views) â”€â”€
RISK_THRESHOLDS = {
    "elevation": {"low": 300, "medium": 350},
    "slope":     {"low": 5,   "medium": 15},
    "stream_proximity": {"high": 200, "medium": 500},
    "drain_density": {"low": 0.3, "high": 0.7},
}

# â”€â”€ DEM / simplification tuning â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
DEM_SAMPLE_STEP = int(os.getenv("DEM_SAMPLE_STEP", "10"))
SIMPLIFY_TOLERANCE = float(os.getenv("SIMPLIFY_TOLERANCE", "0.0001"))
SEWER_SIMPLIFY     = float(os.getenv("SEWER_SIMPLIFY",     "0.0005"))
