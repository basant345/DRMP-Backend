"""
routes/analysis.py — GIS analysis endpoints, multi-city aware.
Risk classification has been replaced by Proposed STP analysis (see routes/stp.py);
this module retains buffer / intersect / nearest / density / dashboard summary,
all parameterised by `city`.
"""
import logging
from flask import Blueprint, jsonify, request

from config import PATHS, DEFAULT_CITY
from utils.gis_loader import load_layer, to_geojson
from utils.gis_analysis import (
    buffer_analysis, intersection_analysis, nearest_feature,
    density_by_ward, ward_statistics,
)
from services.city_service import validate_city
from services.stp_service import get_stp_summary

logger = logging.getLogger("drmp.analysis")
analysis_bp = Blueprint("analysis", __name__)


def _get_city(default=DEFAULT_CITY):
    return request.args.get("city", default) or default


@analysis_bp.route("/summary")
def get_summary():
    """Dashboard summary statistics for a city, including STP analysis status."""
    city = _get_city()
    try:
        city = validate_city(city)
        wards   = load_layer(str(PATHS["wards_sewage"]), city)
        sewer   = load_layer(str(PATHS["sewer_network"]), city)

        stats = {}
        stats["city"] = city
        stats["total_wards"] = int(len(wards))
        if "total_popu" in wards.columns:
            stats["total_population"] = int(wards["total_popu"].sum())
        if "SEWAGE_MLD" in wards.columns:
            stats["total_sewage_mld"] = round(float(wards["SEWAGE_MLD"].sum()), 2)
        if "POP_2035" in wards.columns:
            stats["projected_population_2035"] = int(wards["POP_2035"].sum())
        if "SEWAGE_203" in wards.columns:
            stats["projected_sewage_2035_mld"] = round(float(wards["SEWAGE_203"].sum()), 2)
        try:
            utm = wards.estimate_utm_crs()
            stats["total_area_km2"] = round(float(wards.to_crs(utm).geometry.area.sum() / 1e6), 2)
        except Exception:
            pass
        try:
            sewer_utm = sewer.to_crs(32644)
            stats["total_sewer_length_km"] = round(float(sewer_utm.geometry.length.sum() / 1000), 2)
            stats["total_sewer_pipes"] = int(len(sewer))
        except Exception:
            pass

        # Fold in STP summary so the dashboard shows it without a second call
        stp_summary = get_stp_summary(city)
        stats["stp_analysis"] = stp_summary

        return jsonify({"status": "ok", "data": stats})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.exception("Summary error")
        return jsonify({"error": str(e)}), 500


@analysis_bp.route("/ward-stats")
def get_ward_stats():
    """Per-ward statistics table for a city."""
    city = _get_city()
    try:
        city = validate_city(city)
        wards = load_layer(str(PATHS["wards_sewage"]), city)
        records = ward_statistics(wards)
        return jsonify({"status": "ok", "city": city, "count": len(records), "data": records})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.exception("Ward stats error")
        return jsonify({"error": str(e)}), 500


@analysis_bp.route("/buffer", methods=["POST"])
def post_buffer():
    """Buffer analysis. Body: { layer, distance_m, city }"""
    body = request.get_json(force=True)
    layer_key  = body.get("layer", "streams")
    distance_m = float(body.get("distance_m", 200))
    city = body.get("city", DEFAULT_CITY)

    key_map = {
        "wards":   ("wards_sewage", city),
        "streams": ("stream_drain", None),
        "drains":  ("stream_drain", None),
        "sewer":   ("sewer_network", city),
        "narmada": ("narmada_river", None),
    }
    if layer_key not in key_map:
        return jsonify({"error": f"Unknown layer: {layer_key}"}), 400

    try:
        city = validate_city(city)
        path_key, city_filter = key_map[layer_key]
        gdf = load_layer(str(PATHS[path_key]), city_filter)
        buffered = buffer_analysis(gdf, distance_m)
        return jsonify({
            "status": "ok", "layer": layer_key, "distance_m": distance_m, "city": city,
            "data": to_geojson(buffered, simplify=0.0001),
        })
    except Exception as e:
        logger.exception("Buffer error")
        return jsonify({"error": str(e)}), 500


@analysis_bp.route("/intersect", methods=["POST"])
def post_intersect():
    """Intersection of two layers. Body: { layer_a, layer_b, city }"""
    body = request.get_json(force=True)
    city = body.get("city", DEFAULT_CITY)

    def _load(key):
        needs_city = key in ("wards", "sewer")
        c = city if needs_city else None
        pk = {"wards": "wards_sewage", "streams": "stream_drain",
              "drains": "stream_drain", "sewer": "sewer_network",
              "narmada": "narmada_river"}.get(key, key)
        return load_layer(str(PATHS[pk]), c)

    try:
        city = validate_city(city)
        a = _load(body.get("layer_a", "wards"))
        b = _load(body.get("layer_b", "streams"))
        result = intersection_analysis(a, b)
        return jsonify({
            "status": "ok", "city": city, "count": len(result),
            "data": to_geojson(result, simplify=0.0001, max_features=2000),
        })
    except Exception as e:
        logger.exception("Intersect error")
        return jsonify({"error": str(e)}), 500


@analysis_bp.route("/nearest", methods=["POST"])
def post_nearest():
    """Nearest feature analysis. Body: { source, target, city }"""
    body = request.get_json(force=True)
    city = body.get("city", DEFAULT_CITY)

    def _load(key):
        pk = {"wards": "wards_sewage", "streams": "stream_drain",
              "sewer": "sewer_network"}.get(key, "stream_drain")
        c = city if key in ("wards", "sewer") else None
        return load_layer(str(PATHS[pk]), c)

    try:
        city = validate_city(city)
        src = _load(body.get("source", "wards"))
        tgt = _load(body.get("target", "streams"))
        result = nearest_feature(src, tgt)
        return jsonify({
            "status": "ok", "city": city, "count": len(result),
            "data": to_geojson(result, simplify=0.0001),
        })
    except Exception as e:
        logger.exception("Nearest error")
        return jsonify({"error": str(e)}), 500


@analysis_bp.route("/density")
def get_density():
    """Sewer pipe density by ward, for a city."""
    city = _get_city()
    try:
        city = validate_city(city)
        wards = load_layer(str(PATHS["wards_sewage"]), city)
        sewer = load_layer(str(PATHS["sewer_network"]), city)
        sewer_pts = sewer.copy()
        sewer_pts["geometry"] = sewer.geometry.centroid
        result = density_by_ward(sewer_pts, wards, feature_name="sewer")
        return jsonify({"status": "ok", "city": city, "data": to_geojson(result, simplify=0.0001)})
    except Exception as e:
        logger.exception("Density error")
        return jsonify({"error": str(e)}), 500


@analysis_bp.route("/elevation-stats")
def get_elevation_stats():
    """DEM elevation summary (raster is city-agnostic — full district coverage)."""
    try:
        from utils.gis_loader import dem_summary
        stats = dem_summary(str(PATHS["dem"]))
        return jsonify({"status": "ok", "data": stats})
    except Exception as e:
        logger.exception("Elevation stats error")
        return jsonify({"error": str(e)}), 500
