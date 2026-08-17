"""
routes/reports.py — PDF, CSV, GeoJSON report generation. Multi-city aware.
"""
import io
import json
import csv
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request, Response, send_file

from config import PATHS, DEFAULT_CITY
from utils.gis_loader import load_layer, to_geojson
from utils.gis_analysis import ward_statistics
from services.city_service import validate_city
from services.stp_service import get_stp_table, get_stp_summary

logger = logging.getLogger("drmp.reports")
reports_bp = Blueprint("reports", __name__)


def _get_city(default=DEFAULT_CITY):
    return request.args.get("city", request.args.get("ulb", default)) or default


@reports_bp.route("/csv/wards")
def export_wards_csv():
    """Export ward statistics as CSV for a city."""
    city = _get_city()
    try:
        city = validate_city(city)
        wards   = load_layer(str(PATHS["wards_sewage"]), city)
        records = ward_statistics(wards)
        if not records:
            return jsonify({"error": "No data"}), 404
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)
        return Response(
            output.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="ward_stats_{city}_{_now()}.csv"'},
        )
    except Exception as e:
        logger.exception("CSV export error")
        return jsonify({"error": str(e)}), 500


@reports_bp.route("/csv/stp")
def export_stp_csv():
    """Export Proposed STP locations as CSV for a city."""
    city = _get_city()
    try:
        city = validate_city(city)
        records = get_stp_table(city)
        if not records:
            return jsonify({"error": f"No STP analysis available for {city}"}), 404
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=records[0].keys())
        writer.writeheader()
        writer.writerows(records)
        return Response(
            output.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="proposed_stp_{city}_{_now()}.csv"'},
        )
    except Exception as e:
        logger.exception("STP CSV export error")
        return jsonify({"error": str(e)}), 500


@reports_bp.route("/geojson/<layer_key>")
def export_geojson(layer_key: str):
    """Export any layer as GeoJSON file download, scoped to a city."""
    city = _get_city()
    city_filter_keys = {"wards_sewage", "sewer_network", "mpward"}
    key_map = {
        "wards":   "wards_sewage",
        "streams": "stream_drain",
        "drains":  "stream_drain",
        "sewer":   "sewer_network",
        "narmada": "narmada_river",
        "ulbs":    "mpulb",
    }

    if layer_key == "stp":
        try:
            city = validate_city(city)
            from services.stp_service import get_stp_geojson
            geojson = get_stp_geojson(city)
            out = json.dumps(geojson, ensure_ascii=False, indent=2)
            return Response(
                out, mimetype="application/geo+json",
                headers={"Content-Disposition": f'attachment; filename="proposed_stp_{city}_{_now()}.geojson"'},
            )
        except Exception as e:
            logger.exception("STP GeoJSON export error")
            return jsonify({"error": str(e)}), 500

    path_key = key_map.get(layer_key, layer_key)
    if path_key not in PATHS:
        return jsonify({"error": f"Unknown layer: {layer_key}"}), 400

    try:
        city = validate_city(city)
        c = city if path_key in city_filter_keys else None
        gdf = load_layer(str(PATHS[path_key]), c)
        geojson = to_geojson(gdf)
        out = json.dumps(geojson, ensure_ascii=False, indent=2)
        return Response(
            out, mimetype="application/geo+json",
            headers={"Content-Disposition": f'attachment; filename="{layer_key}_{city}_{_now()}.geojson"'},
        )
    except Exception as e:
        logger.exception("GeoJSON export error")
        return jsonify({"error": str(e)}), 500


@reports_bp.route("/pdf/ward-report")
def export_ward_pdf():
    """Generate a PDF ward + STP report using ReportLab, for a city."""
    city = _get_city()
    try:
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib import colors
        from reportlab.lib.units import cm

        city = validate_city(city)
        wards   = load_layer(str(PATHS["wards_sewage"]), city)
        records = ward_statistics(wards)
        stp_summary = get_stp_summary(city)
        stp_records = get_stp_table(city)

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                                leftMargin=1.5*cm, rightMargin=1.5*cm,
                                topMargin=2*cm, bottomMargin=2*cm)
        styles = getSampleStyleSheet()
        elements = []

        elements.append(Paragraph(f"District River Management Plan — {city}", styles["Title"]))
        elements.append(Paragraph(f"Generated: {datetime.now().strftime('%d %B %Y, %H:%M')}", styles["Normal"]))
        elements.append(Spacer(1, 0.5*cm))

        # Ward table
        elements.append(Paragraph("Ward-wise Data", styles["Heading2"]))
        cols = ["wardno", "wardname", "total_popu", "SEWAGE_MLD", "POP_2035", "SEWAGE_203", "area_km2"]
        headers = ["Ward No.", "Ward Name", "Population", "Sewage (MLD)", "Pop 2035", "Sewage 2035 (MLD)", "Area (km²)"]
        ward_data = [headers] + [[str(rec.get(c, "—") or "—") for c in cols] for rec in records[:60]]
        wt = Table(ward_data, colWidths=[2*cm, 5*cm, 3*cm, 3*cm, 3*cm, 3.5*cm, 3*cm])
        wt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a56db")),
            ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
            ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4ff")]),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d1d5db")),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("PADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(wt)
        elements.append(Spacer(1, 0.6*cm))

        # STP section
        if stp_summary.get("available"):
            elements.append(Paragraph("Proposed STP Locations (Suitability Analysis)", styles["Heading2"]))
            elements.append(Paragraph(
                f"Total proposed STPs: {stp_summary['count']}  ·  "
                f"Total capacity: {stp_summary.get('total_capacity_mld', '—')} MLD  ·  "
                f"Average suitability score: {stp_summary.get('avg_score', '—')}",
                styles["Normal"]
            ))
            elements.append(Spacer(1, 0.2*cm))
            stp_headers = ["STP ID", "Capacity (MLD)", "Elevation (m)", "Flood Score", "Suitability Score", "Latitude", "Longitude"]
            stp_data = [stp_headers] + [
                [str(r.get("stp_id","—")), str(r.get("Capacity_MLD","—")), str(r.get("Elevation","—")),
                 str(r.get("FloodScore","—")), str(r.get("Score","—")), str(r.get("latitude","—")), str(r.get("longitude","—"))]
                for r in stp_records
            ]
            st = Table(stp_data, colWidths=[2.5*cm, 3*cm, 3*cm, 3*cm, 3.5*cm, 3*cm, 3*cm])
            st.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16a34a")),
                ("TEXTCOLOR",  (0, 0), (-1, 0), colors.white),
                ("FONTNAME",   (0, 0), (-1, 0), "Helvetica-Bold"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0fdf4")]),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d1d5db")),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("PADDING", (0, 0), (-1, -1), 4),
            ]))
            elements.append(st)
        else:
            elements.append(Paragraph("Proposed STP Locations", styles["Heading2"]))
            elements.append(Paragraph("No STP suitability analysis has been run for this city yet.", styles["Normal"]))

        doc.build(elements)
        buf.seek(0)
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                         download_name=f"DRMP_Report_{city}_{_now()}.pdf")
    except ImportError:
        return jsonify({"error": "ReportLab not installed. Install with: pip install reportlab"}), 501
    except Exception as e:
        logger.exception("PDF report error")
        return jsonify({"error": str(e)}), 500


def _now():
    return datetime.now().strftime("%Y%m%d_%H%M")
