"""
routes/layers.py — REST endpoints for GIS layer retrieval, multi-city aware.
Every endpoint now takes `city` (query param or path) instead of assuming
a single hardcoded ULB.
"""
import logging
from flask import Blueprint, jsonify, request

from config import PATHS, DEFAULT_CITY, SIMPLIFY_TOLERANCE, SEWER_SIMPLIFY
from utils.gis_loader import load_layer, to_geojson, layer_bounds, safe_area_km2, safe_length_km
from services.city_service import validate_city, get_city_bounds

logger = logging.getLogger("drmp.layers")
layers_bp = Blueprint("layers", __name__)


def _get_city(default=DEFAULT_CITY):
    return request.args.get("city", default) or default


@layers_bp.route("/wards")
def get_wards():
    """Ward polygons for a city, with sewage/population attributes."""
    city = _get_city()
    simplify = float(request.args.get("simplify", SIMPLIFY_TOLERANCE))
    try:
        city = validate_city(city)
        gdf = load_layer(str(PATHS["wards_sewage"]), city)
        gdf = safe_area_km2(gdf)
        return jsonify({
            "status": "ok",
            "count": len(gdf),
            "city": city,
            "data": to_geojson(gdf, simplify=simplify),
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.exception("Error loading wards")
        return jsonify({"error": str(e)}), 500


@layers_bp.route("/wards/<wardno>")
def get_ward_detail(wardno):
    """Single ward detail by ward number, scoped to a city."""
    city = _get_city()
    try:
        city = validate_city(city)
        gdf = load_layer(str(PATHS["wards_sewage"]), city)
        ward = gdf[gdf["wardno"].astype(str) == str(wardno)]
        if ward.empty:
            return jsonify({"error": f"Ward {wardno} not found in {city}"}), 404
        ward = safe_area_km2(ward)
        return jsonify({
            "status": "ok",
            "data": to_geojson(ward),
            "properties": ward.drop(columns=["geometry"]).iloc[0].to_dict(),
        })
    except Exception as e:
        logger.exception("Error loading ward %s", wardno)
        return jsonify({"error": str(e)}), 500


@layers_bp.route("/streams")
def get_streams():
    """
    Stream & drain network, clipped to the selected city's ward extent
    so neighbouring cities' streams don't bleed into the map.
    """
    city = _get_city()
    simplify = float(request.args.get("simplify", SIMPLIFY_TOLERANCE))
    layer = request.args.get("layer", "stream_drain")
    path_key = "narmada_river" if layer == "narmada" else "stream_drain"
    try:
        city = validate_city(city)
        gdf = load_layer(str(PATHS[path_key]))
        gdf = _clip_to_city(gdf, city)
        gdf = safe_length_km(gdf)
        return jsonify({
            "status": "ok",
            "count": len(gdf),
            "city": city,
            "data": to_geojson(gdf, simplify=simplify),
        })
    except Exception as e:
        logger.exception("Error loading streams")
        return jsonify({"error": str(e)}), 500


@layers_bp.route("/drains")
def get_drains():
    """Drain lines for a city (filtered by Flow_Type where available)."""
    city = _get_city()
    simplify = float(request.args.get("simplify", SIMPLIFY_TOLERANCE))
    try:
        city = validate_city(city)
        gdf = load_layer(str(PATHS["stream_drain"]))
        if "Flow_Type" in gdf.columns:
            drain = gdf[gdf["Flow_Type"].str.lower().str.contains("drain", na=False)]
            if drain.empty:
                drain = gdf
        else:
            drain = gdf
        drain = _clip_to_city(drain, city)
        drain = safe_length_km(drain)
        return jsonify({
            "status": "ok",
            "count": len(drain),
            "city": city,
            "data": to_geojson(drain, simplify=simplify),
        })
    except Exception as e:
        logger.exception("Error loading drains")
        return jsonify({"error": str(e)}), 500


@layers_bp.route("/sewer")
def get_sewer():
    """Sewer network for a city."""
    city = _get_city()
    simplify = float(request.args.get("simplify", SEWER_SIMPLIFY))
    max_feat = int(request.args.get("max_features", 5000))
    try:
        city = validate_city(city)
        gdf = load_layer(str(PATHS["sewer_network"]), city)
        gdf = safe_length_km(gdf)
        return jsonify({
            "status": "ok",
            "count": len(gdf),
            "city": city,
            "data": to_geojson(gdf, simplify=simplify, max_features=max_feat),
        })
    except Exception as e:
        logger.exception("Error loading sewer")
        return jsonify({"error": str(e)}), 500


@layers_bp.route("/ulb-boundary")
def get_ulb_boundary():
    """
    Dissolved ward boundary for ONE city only — used to mask/fit the map
    so neighbouring ULBs are never shown.
    """
    city = _get_city()
    try:
        city = validate_city(city)
        gdf = load_layer(str(PATHS["wards_sewage"]), city)
        dissolved = gdf.dissolve()
        return jsonify({
            "status": "ok",
            "city": city,
            "data": to_geojson(dissolved, simplify=0.00005),
        })
    except Exception as e:
        logger.exception("Error loading ULB boundary")
        return jsonify({"error": str(e)}), 500


@layers_bp.route("/bounds")
def get_bounds():
    """Tight bounding box for a city — map auto-fit uses this."""
    city = _get_city()
    try:
        city = validate_city(city)
        bounds = get_city_bounds(city)
        return jsonify({"status": "ok", "city": city, "bounds": bounds})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@layers_bp.route("/available")
def get_available_layers():
    """List all available layer types with metadata (city-agnostic)."""
    from pathlib import Path
    layers = []
    for key, path in PATHS.items():
        p = Path(str(path))
        layers.append({
            "key": key,
            "name": p.stem,
            "exists": p.exists(),
            "type": "raster" if str(path).endswith(".tif") else "vector",
        })
    return jsonify({"status": "ok", "layers": layers})


# ── Internal helper ────────────────────────────────────────────────────────────

def _clip_to_city(gdf, city):
    """Clip a city-agnostic layer (streams, drains) to the city's ward extent."""
    try:
        from utils.gis_loader import load_layer as _load
        wards = _load(str(PATHS["wards_sewage"]), city)
        if wards.empty:
            return gdf
        boundary = wards.union_all() if hasattr(wards, "union_all") else wards.unary_union
        # Add small buffer so streams just outside ward polygons (e.g. river banks) still show
        boundary_buffered = boundary.buffer(0.01)
        clipped = gdf[gdf.geometry.intersects(boundary_buffered)]
        return clipped
    except Exception as e:
        logger.warning("Clip to city failed (%s) — returning unclipped layer", e)
        return gdf
