"""
routes/cities.py — City discovery & bounds API. Powers the city dropdown
and map auto-fit on the frontend.
"""
import logging
from flask import Blueprint, jsonify, request

from services.city_service import list_cities, get_city_bounds, validate_city
from config import DEFAULT_CITY

logger = logging.getLogger("drmp.cities")
cities_bp = Blueprint("cities", __name__)


@cities_bp.route("/")
def get_cities():
    """
    List every city available in the dataset, with a flag for whether
    Proposed STP analysis output exists for it.
    """
    try:
        return jsonify({
            "status": "ok",
            "default_city": DEFAULT_CITY,
            "cities": list_cities(),
        })
    except Exception as e:
        logger.exception("City list error")
        return jsonify({"error": str(e)}), 500


@cities_bp.route("/<city>/bounds")
def city_bounds(city):
    """Tight bounding box for a city — used to lock/fit the map."""
    try:
        city = validate_city(city)
        bounds = get_city_bounds(city)
        return jsonify({"status": "ok", "city": city, "bounds": bounds})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.exception("City bounds error")
        return jsonify({"error": str(e)}), 500
