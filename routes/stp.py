"""
routes/stp.py — Proposed STP Location endpoints, driven by the offline
Python suitability-analysis script's output (KMeans clustering + DEM/sewer/
stream/drain/flood scoring). Replaces the old generic risk-classification view.
"""
import logging
from flask import Blueprint, jsonify, request

from services.city_service import validate_city
from services.stp_service import (
    get_stp_geojson, get_stp_summary, get_stp_table, suggest_top_n_stps,
)
from config import DEFAULT_CITY

logger = logging.getLogger("drmp.stp_routes")
stp_bp = Blueprint("stp", __name__)


@stp_bp.route("/<city>")
def get_stp_for_city(city):
    """
    Proposed STP locations for a city as GeoJSON, with full attributes:
    stp_id, cluster, Capacity_MLD, Elevation, FloodScore, Score, lat/lon.
    """
    try:
        city = validate_city(city)
        data = get_stp_geojson(city)
        return jsonify({
            "status": "ok",
            "city": city,
            "count": len(data.get("features", [])),
            "data": data,
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.exception("STP fetch error")
        return jsonify({"error": str(e)}), 500


@stp_bp.route("/<city>/summary")
def get_stp_summary_for_city(city):
    """Summary stats: count, total capacity, average elevation/score."""
    try:
        city = validate_city(city)
        return jsonify({"status": "ok", "data": get_stp_summary(city)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.exception("STP summary error")
        return jsonify({"error": str(e)}), 500


@stp_bp.route("/<city>/table")
def get_stp_table_for_city(city):
    """Tabular STP records for the analysis data table."""
    try:
        city = validate_city(city)
        return jsonify({"status": "ok", "city": city, "data": get_stp_table(city)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.exception("STP table error")
        return jsonify({"error": str(e)}), 500


@stp_bp.route("/<city>/suggest")
def suggest_stps_for_city(city):
    """
    User-driven "Suggest N STPs" feature.

    Query param: count (positive integer, required).
    Ranking and weights are entirely inherited from the existing suitability
    methodology (see services/stp_service.py) — this route only validates
    the requested count and returns the top N pre-ranked candidates for the
    selected city, restricted to that city's boundary by construction.
    """
    try:
        city = validate_city(city)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    raw_count = request.args.get("count", "").strip()
    if not raw_count:
        return jsonify({
            "status": "error", "reason": "invalid_count",
            "message": "Please enter the number of STPs to suggest.",
        }), 400

    try:
        count = int(raw_count)
    except ValueError:
        return jsonify({
            "status": "error", "reason": "invalid_count",
            "message": "Number of STPs must be a positive whole number.",
        }), 400

    try:
        result = suggest_top_n_stps(city, count)
    except Exception as e:
        logger.exception("STP suggest error")
        return jsonify({"error": str(e)}), 500

    if result["status"] == "error":
        return jsonify(result), 400

    return jsonify(result)

