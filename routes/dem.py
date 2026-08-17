"""
routes/dem.py  — DEM visualization endpoints (elevation, hillshade, slope, color relief).
"""
import io
import base64
import logging
import numpy as np
from flask import Blueprint, jsonify, request, Response

from config import PATHS, DEM_SAMPLE_STEP
from utils.gis_loader import dem_summary, dem_tile, compute_slope, compute_hillshade, open_dem

logger = logging.getLogger("drmp.dem")
dem_bp = Blueprint("dem", __name__)


@dem_bp.route("/info")
def get_dem_info():
    """DEM metadata and statistics."""
    try:
        stats = dem_summary(str(PATHS["dem"]), step=DEM_SAMPLE_STEP)
        return jsonify({"status": "ok", "data": stats})
    except Exception as e:
        logger.exception("DEM info error")
        return jsonify({"error": str(e)}), 500


@dem_bp.route("/tile")
def get_dem_tile():
    """
    Return DEM tile as JSON array for a bbox.
    Query params: west, south, east, north, width, height, mode
    mode: elevation | slope | hillshade | relief
    """
    try:
        west  = float(request.args.get("west",   77.5))
        south = float(request.args.get("south",  22.5))
        east  = float(request.args.get("east",   78.0))
        north = float(request.args.get("north",  23.0))
        width  = int(request.args.get("width",  128))
        height = int(request.args.get("height", 128))
        mode   = request.args.get("mode", "elevation")

        arr = dem_tile(str(PATHS["dem"]), west, south, east, north, width, height)

        if mode == "slope":
            arr = compute_slope(arr)
        elif mode == "hillshade":
            arr = compute_hillshade(arr).astype(float)
        elif mode == "relief":
            # Normalise to 0-255 for colour relief
            mn, mx = np.nanmin(arr), np.nanmax(arr)
            arr = ((arr - mn) / (mx - mn + 1e-9) * 255) if mx > mn else np.zeros_like(arr)

        # Replace NaN with -9999 sentinel for JSON
        arr_clean = np.where(np.isnan(arr), -9999, arr).round(2)
        return jsonify({
            "status": "ok",
            "mode": mode,
            "bbox": [west, south, east, north],
            "shape": [height, width],
            "data": arr_clean.tolist(),
        })
    except Exception as e:
        logger.exception("DEM tile error")
        return jsonify({"error": str(e)}), 500


@dem_bp.route("/profile")
def get_dem_profile():
    """
    Elevation profile along a line (GeoJSON LineString).
    Body: { "coordinates": [[lon, lat], ...], "n_points": 100 }
    """
    body = request.get_json(force=True) or {}
    coords = body.get("coordinates", [])
    n_points = int(body.get("n_points", 100))

    if len(coords) < 2:
        return jsonify({"error": "At least 2 coordinates required"}), 400

    try:
        from shapely.geometry import LineString
        import rasterio
        line = LineString(coords)
        distances = np.linspace(0, line.length, n_points)
        pts = [line.interpolate(d) for d in distances]

        with open_dem(str(PATHS["dem"])) as src:
            sample_coords = [(p.x, p.y) for p in pts]
            elevations = [v[0] for v in src.sample(sample_coords)]

        nodata = None
        with open_dem(str(PATHS["dem"])) as src:
            nodata = src.nodata

        profile = []
        total_dist = 0
        for i, (pt, elev, d) in enumerate(zip(pts, elevations, distances)):
            e = None if (nodata and elev == nodata) else float(elev)
            profile.append({
                "lon": round(pt.x, 6),
                "lat": round(pt.y, 6),
                "elevation_m": e,
                "distance_m": round(float(line.length * 111000 * d / line.length), 1) if i > 0 else 0,
            })

        return jsonify({
            "status": "ok",
            "n_points": len(profile),
            "profile": profile,
        })
    except Exception as e:
        logger.exception("DEM profile error")
        return jsonify({"error": str(e)}), 500


@dem_bp.route("/sample")
def get_dem_sample():
    """Sample elevation at a point. Query: lat, lon"""
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
        with open_dem(str(PATHS["dem"])) as src:
            vals = list(src.sample([(lon, lat)]))
            elev = float(vals[0][0]) if vals else None
            if src.nodata and elev == src.nodata:
                elev = None
        return jsonify({
            "status": "ok",
            "lat": lat, "lon": lon,
            "elevation_m": elev,
        })
    except KeyError:
        return jsonify({"error": "lat and lon required"}), 400
    except Exception as e:
        logger.exception("DEM sample error")
        return jsonify({"error": str(e)}), 500
