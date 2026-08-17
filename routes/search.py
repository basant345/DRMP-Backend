"""
routes/search.py — Search endpoints, multi-city aware.
"""
import logging
from flask import Blueprint, jsonify, request

from config import PATHS, DEFAULT_CITY
from utils.gis_loader import load_layer, to_geojson
from services.city_service import validate_city

logger = logging.getLogger("drmp.search")
search_bp = Blueprint("search", __name__)


def _get_city(default=DEFAULT_CITY):
    return request.args.get("city", request.args.get("ulb", default)) or default


@search_bp.route("/ward")
def search_ward():
    """Search wards by name or number, scoped to a city."""
    q    = request.args.get("q", "").strip()
    city = _get_city()
    if not q:
        return jsonify({"error": "Query parameter 'q' required"}), 400
    try:
        city = validate_city(city)
        gdf = load_layer(str(PATHS["wards_sewage"]), city)
        mask = (
            gdf["wardname"].astype(str).str.lower().str.contains(q.lower(), na=False)
            | gdf["wardno"].astype(str).str.lower().str.contains(q.lower(), na=False)
        )
        result = gdf[mask]
        return jsonify({
            "status": "ok", "count": len(result), "query": q, "city": city,
            "data": to_geojson(result) if len(result) > 0 else None,
            "items": result[["wardno", "wardname"]].to_dict(orient="records") if len(result) > 0 else [],
        })
    except Exception as e:
        logger.exception("Ward search error")
        return jsonify({"error": str(e)}), 500


@search_bp.route("/stream")
def search_stream():
    """Search streams by river name."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "Query parameter 'q' required"}), 400
    try:
        gdf = load_layer(str(PATHS["stream_drain"]))
        mask = gdf["River_Name"].astype(str).str.lower().str.contains(q.lower(), na=False)
        result = gdf[mask]
        items = result[["River_Name"]].drop_duplicates().to_dict(orient="records") if "River_Name" in result.columns else []
        return jsonify({
            "status": "ok", "count": len(result), "query": q,
            "data": to_geojson(result) if len(result) > 0 else None,
            "items": items,
        })
    except Exception as e:
        logger.exception("Stream search error")
        return jsonify({"error": str(e)}), 500


@search_bp.route("/drain")
def search_drain():
    """Search drains by name or flow type."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "Query parameter 'q' required"}), 400
    try:
        gdf = load_layer(str(PATHS["stream_drain"]))
        if "Flow_Type" in gdf.columns:
            mask = (
                gdf["River_Name"].astype(str).str.lower().str.contains(q.lower(), na=False)
                | gdf["Flow_Type"].astype(str).str.lower().str.contains(q.lower(), na=False)
            )
        else:
            mask = gdf["River_Name"].astype(str).str.lower().str.contains(q.lower(), na=False)
        result = gdf[mask]
        return jsonify({
            "status": "ok", "count": len(result), "query": q,
            "data": to_geojson(result) if len(result) > 0 else None,
        })
    except Exception as e:
        logger.exception("Drain search error")
        return jsonify({"error": str(e)}), 500


@search_bp.route("/autocomplete")
def autocomplete():
    """Autocomplete suggestions for wards and rivers, scoped to a city."""
    q    = request.args.get("q", "").strip().lower()
    typ  = request.args.get("type", "all")
    city = _get_city()
    suggestions = []

    try:
        city = validate_city(city)
        if typ in ("ward", "all"):
            wards = load_layer(str(PATHS["wards_sewage"]), city)
            for _, row in wards.iterrows():
                name = str(row.get("wardname", ""))
                if q in name.lower():
                    suggestions.append({
                        "type": "ward",
                        "label": f"Ward {row.get('wardno')} – {name}",
                        "value": str(row.get("wardno", "")),
                    })

        if typ in ("stream", "all"):
            streams = load_layer(str(PATHS["stream_drain"]))
            names = streams["River_Name"].dropna().unique() if "River_Name" in streams.columns else []
            for name in names:
                if q in str(name).lower():
                    suggestions.append({"type": "stream", "label": str(name), "value": str(name)})

        return jsonify({"status": "ok", "query": q, "city": city, "suggestions": suggestions[:20]})
    except Exception as e:
        logger.exception("Autocomplete error")
        return jsonify({"error": str(e)}), 500
