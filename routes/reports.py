"""
routes/reports.py — PDF, CSV, GeoJSON report generation. Multi-city aware.

The DRMP report (PDF + DOCX) is the only part that changed in this file —
CSV and GeoJSON exports below are untouched. It now builds the full
two-part DRMP report (Part 1: common Chapter 1, identical for every city;
Part 2: the selected city's profile, water supply & sewerage, future
plans, STP analysis and key findings/recommendations), sourced from
report_content/drmp_report_content.json for the editorial text and from
the app's existing ward-statistics / STP-suitability pipeline for every
number in §2.4. See services/report_content.py for the content-matching
and computed-line logic.
"""
import io
import json
import csv
import logging
from datetime import datetime
from pathlib import Path
from flask import Blueprint, jsonify, request, Response, send_file

from config import PATHS, DEFAULT_CITY
from utils.gis_loader import load_layer, to_geojson
from utils.gis_analysis import ward_statistics
from services.city_service import validate_city
from services.stp_service import get_stp_table, get_stp_summary, load_stp_data
from services import report_content

logger = logging.getLogger("drmp.reports")
reports_bp = Blueprint("reports", __name__)


def _get_city(default=DEFAULT_CITY):
    return request.args.get("city", request.args.get("ulb", default)) or default


def _get_draft_flag() -> bool:
    """Internal-draft toggle (Reports page checkbox): only when this is on,
    and only when a city record actually has data_checks, does §2.3 print a
    Data checks block. Never shown in a non-draft (final) report."""
    v = (request.args.get("draft") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


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


# ═════════════════════════════════════════════════════════════════════════════
# DRMP report — PDF + DOCX. Part 1 (common) + Part 2 (selected city only).
# ═════════════════════════════════════════════════════════════════════════════

def _fmt_int(v):
    if v in (None, "", "—"):
        return "—"
    try:
        return f"{int(round(float(v))):,}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_mld(v, decimals=2):
    if v in (None, "", "—"):
        return "—"
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_coord(v, decimals=6):
    """Format a latitude/longitude value to a fixed precision (6 decimal
    places ~= 11cm, plenty for siting purposes). Without this, a raw float
    coming out of a GIS computation can carry binary floating-point noise
    (e.g. 79.91000000000001) straight into the report table."""
    if v in (None, "", "—"):
        return "—"
    try:
        return f"{float(v):.{decimals}f}"
    except (TypeError, ValueError):
        return str(v)


def _ward_sort_key(rec):
    try:
        return (0, int(rec.get("wardno")))
    except (TypeError, ValueError):
        return (1, str(rec.get("wardno", "")))


def _report_filename(city: str, ext: str) -> str:
    safe_city = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in city)
    return f"DRMP_{safe_city}_{datetime.now().strftime('%Y%m%d')}.{ext}"


def _gather_report_data(city: str, draft: bool):
    """
    Everything a DRMP report (PDF or DOCX) needs, assembled once so both
    renderers stay in sync. Raises nothing for a city with no editorial
    profile — that is a normal, expected case (see
    app_cities_without_profile), not an error.
    """
    common = report_content.get_common()
    city_record = report_content.get_city_record(city)
    profile_missing = city_record is None

    computed_line = report_content.compute_line(city_record) if city_record else None

    # Ward-wise 2011 / 2035 population & sewage — exactly what the app
    # already computes (ward_statistics has no 2025/2050 columns; none are
    # invented here).
    wards_gdf = load_layer(str(PATHS["wards_sewage"]), city)
    ward_records = sorted(ward_statistics(wards_gdf), key=_ward_sort_key)

    stp_summary = get_stp_summary(city)
    stp_table = get_stp_table(city)
    stp_raw = load_stp_data(city)
    stp_weights = (stp_raw or {}).get("weights", {})

    data_checks = []
    if draft and city_record and city_record.get("data_checks"):
        data_checks = city_record["data_checks"]

    findings = []
    recs = []
    if city_record:
        findings = report_content.key_findings(city_record, stp_summary)
        recs = report_content.recommendations(stp_summary)

    return {
        "common": common,
        "city": city,
        "city_record": city_record,
        "profile_missing": profile_missing,
        "computed_line": computed_line,
        "ward_records": ward_records,
        "stp_summary": stp_summary,
        "stp_table": stp_table,
        "stp_weights": stp_weights,
        "data_checks": data_checks,
        "findings": findings,
        "recommendations": recs,
        "generated_str": f"Generated: {datetime.now().strftime('%d %B %Y, %H:%M')}",
    }


# ── PDF ──────────────────────────────────────────────────────────────────────

def _make_header_footer(city_label, generated_str):
    def _draw(canvas, doc):
        canvas.saveState()
        width, height = doc.pagesize
        canvas.setFont("Times-Bold", 10)
        canvas.drawString(doc.leftMargin, height - 0.55 * 72, f"DRMP — {city_label}")
        canvas.setFont("Times-Roman", 8.5)
        canvas.drawRightString(width - doc.rightMargin, height - 0.55 * 72, generated_str)
        canvas.setLineWidth(0.6)
        canvas.setStrokeColorRGB(0.2, 0.2, 0.2)
        canvas.line(doc.leftMargin, height - 0.62 * 72, width - doc.rightMargin, height - 0.62 * 72)
        canvas.setLineWidth(0.4)
        canvas.setStrokeColorRGB(0.6, 0.6, 0.6)
        canvas.line(doc.leftMargin, 0.55 * 72, width - doc.rightMargin, 0.55 * 72)
        canvas.setFont("Times-Roman", 8)
        canvas.drawString(doc.leftMargin, 0.38 * 72, generated_str)
        canvas.drawCentredString(width / 2.0, 0.38 * 72, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()
    return _draw


import re as _re

_KM2_RE = _re.compile("km²")


def _pdf_text(s):
    """
    Make a content string safe to pass into a ReportLab Paragraph.

    ReportLab's Paragraph text is parsed as a small XML dialect, so a raw
    '&' (e.g. "10-year O&M") breaks parsing — this escapes &, < and >.
    Separately, the base 14 Times fonts ReportLab ships with have no glyph
    for ₹ (Indian Rupee) or ² (superscript two), so those are swapped for
    ASCII-safe equivalents here. This only affects the PDF: the DOCX
    builder uses python-docx, whose runs take plain Unicode text directly
    (no XML-escaping needed) and Word's own Times New Roman renders ₹ and
    ² normally, so the DOCX keeps the real characters.
    """
    if s is None:
        return "—"
    s = str(s)
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("₹", "Rs. ")
    s = _KM2_RE.sub("sq km", s)
    s = s.replace("²", "")
    return s


def _build_pdf(data: dict) -> bytes:
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image,
        ListFlowable, ListItem, PageBreak,
    )
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_LEFT

    HEADING_COLOR = colors.HexColor("#0F4761")

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("DRMPTitle", parent=styles["Title"], fontName="Times-Bold",
                                  fontSize=22, leading=26, textColor=HEADING_COLOR, spaceAfter=10)
    h1_style = ParagraphStyle("DRMPH1", parent=styles["Heading1"], fontName="Times-Bold",
                               fontSize=16, leading=19, textColor=HEADING_COLOR,
                               spaceBefore=4, spaceAfter=8)
    h2_style = ParagraphStyle("DRMPH2", parent=styles["Heading2"], fontName="Times-Bold",
                               fontSize=13.5, leading=16, textColor=HEADING_COLOR,
                               spaceBefore=12, spaceAfter=6)
    h3_style = ParagraphStyle("DRMPH3", parent=styles["Heading3"], fontName="Times-Bold",
                               fontSize=11.5, leading=14, textColor=HEADING_COLOR,
                               spaceBefore=8, spaceAfter=5)
    normal_style = ParagraphStyle("DRMPNormal", parent=styles["Normal"], fontName="Times-Roman",
                                   fontSize=10.5, leading=14, spaceAfter=6)
    subtitle_style = ParagraphStyle("DRMPSubtitle", parent=normal_style, fontName="Times-Bold",
                                     fontSize=10.5, textColor=colors.HexColor("#444444"))
    bullet_style = ParagraphStyle("DRMPBullet", parent=normal_style, spaceAfter=3)
    caption_style = ParagraphStyle("DRMPCaption", parent=normal_style, fontName="Times-Italic",
                                    fontSize=9, textColor=colors.HexColor("#555555"), spaceAfter=0)
    cell_style = ParagraphStyle("DRMPCell", parent=styles["Normal"], fontName="Times-Roman",
                                 fontSize=8, leading=10)
    cell_bold_style = ParagraphStyle("DRMPCellBold", parent=cell_style, fontName="Times-Bold")
    header_cell_style = ParagraphStyle("DRMPHeaderCell", parent=cell_style, fontName="Times-Bold",
                                        textColor=colors.white)
    label_cell_style = ParagraphStyle("DRMPLabelCell", parent=cell_style, fontName="Times-Bold")

    def cell(text, bold=False):
        style = cell_bold_style if bold else cell_style
        return Paragraph(_pdf_text(text) if text not in (None, "") else "—", style)

    elements = []

    # ── Part 1 (common) ──────────────────────────────────────────────────
    common = data["common"]
    elements.append(Paragraph(common["title"], title_style))
    elements.append(Spacer(1, 0.1 * inch))
    elements.append(Paragraph(common["heading"], h1_style))
    for sub in common["subsections"]:
        elements.append(Paragraph(sub["heading"], h3_style))
        for para in sub.get("paragraphs", []):
            elements.append(Paragraph(_pdf_text(para), normal_style))
        if sub.get("bullets"):
            items = [ListItem(Paragraph(_pdf_text(b), bullet_style), leftIndent=14) for b in sub["bullets"]]
            elements.append(ListFlowable(items, bulletType="bullet", start="•", leftIndent=14))

    elements.append(PageBreak())

    # ── Part 2 (selected city) ───────────────────────────────────────────
    city_record = data["city_record"]
    display_name = city_record["name"] if city_record else data["city"]

    elements.append(Paragraph(f"2. {display_name}", h1_style))

    if city_record:
        elements.append(Paragraph(_pdf_text(city_record["subtitle"]), subtitle_style))
        elements.append(Paragraph(_pdf_text(city_record["intro"]), normal_style))

        # 2.1 City profile — profile table (left) + photo & caption (right)
        elements.append(Paragraph("2.1 City profile", h2_style))
        profile = city_record["profile"]
        area_ha = profile.get("area_ha")
        area_text = f"{_fmt_int(area_ha)} ha" if area_ha is not None else "—"
        if area_ha is not None:
            area_text += f" ({area_ha / 100:,.2f} km²)"
        profile_rows = [
            ["ULB", city_record["name"]],
            ["District", f"{city_record['district']} district"],
            ["Area", area_text],
            ["Wards", _fmt_int(profile.get("wards"))],
            ["Population", _fmt_int(profile.get("population"))],
            ["Distance from the Narmada", profile.get("distance_from_narmada") or "—"],
            ["Distance from Bhopal", profile.get("distance_from_bhopal") or "—"],
            [profile.get("last_row_label", "Known for"), profile.get("last_row_value") or "—"],
        ]
        profile_table = Table(
            [[cell(k, bold=True), cell(v)] for k, v in profile_rows],
            colWidths=[1.7 * inch, 2.4 * inch],
        )
        profile_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d1d5db")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0f4ff")),
            ("PADDING", (0, 0), (-1, -1), 4),
        ]))

        photo_cell_content = []
        photo = report_content.photo_path(city_record)
        if photo:
            try:
                from PIL import Image as PILImage
                with PILImage.open(photo) as im:
                    w, h = im.size
                target_w = 2.3 * inch
                target_h = target_w * h / w
                max_h = 2.4 * inch
                if target_h > max_h:
                    target_h = max_h
                    target_w = target_h * w / h
                photo_cell_content.append(Image(str(photo), width=target_w, height=target_h))
                if city_record.get("photo_caption"):
                    photo_cell_content.append(Spacer(1, 0.05 * inch))
                    photo_cell_content.append(Paragraph(_pdf_text(city_record["photo_caption"]), caption_style))
            except Exception:
                logger.exception("Could not embed city photo for %s", data["city"])
                photo_cell_content = [Paragraph("(photo unavailable)", caption_style)]
        else:
            photo_cell_content = [Paragraph("(no photo available)", caption_style)]

        layout_table = Table(
            [[profile_table, photo_cell_content]],
            colWidths=[4.3 * inch, 2.6 * inch],
        )
        layout_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (1, 0), (1, 0), 10),
        ]))
        elements.append(layout_table)
        elements.append(Spacer(1, 0.15 * inch))

        # 2.2 Water supply and sewerage
        elements.append(Paragraph("2.2 Water supply and sewerage", h2_style))
        ws = city_record.get("water_supply", {})
        sw = city_record.get("sewerage", {})
        field_order = [
            ("status", "Status"), ("cost", "Cost"), ("funding", "Funding"),
            ("water_source", "Water source"), ("treatment_plant", "Treatment plant"),
            ("storage_pumping", "Storage / pumping"), ("network", "Network"),
            ("house_connections", "House connections"),
        ]
        ws_rows = [[cell("", bold=True), cell("Water supply", bold=True), cell("Sewerage", bold=True)]]
        for key, label in field_order:
            ws_rows.append([cell(label, bold=True), cell(ws.get(key) or "—"), cell(sw.get(key) or "—")])
        ws_table = Table(ws_rows, colWidths=[1.5 * inch, 2.75 * inch, 2.75 * inch], repeatRows=1)
        ws_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), HEADING_COLOR),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4ff")]),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d1d5db")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("PADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(ws_table)
        elements.append(Spacer(1, 0.08 * inch))
        if data["computed_line"]:
            elements.append(Paragraph(_pdf_text(data["computed_line"]), normal_style))

        # 2.3 Future plans listed by the ULB
        elements.append(Paragraph("2.3 Future plans listed by the ULB", h2_style))
        future_plans = city_record.get("future_plans") or []
        if future_plans:
            items = [ListItem(Paragraph(_pdf_text(p), bullet_style), leftIndent=14) for p in future_plans]
            elements.append(ListFlowable(items, bulletType="bullet", start="•", leftIndent=14))
        else:
            elements.append(Paragraph("None listed.", normal_style))

        if data["data_checks"]:
            elements.append(Paragraph("Data checks", h3_style))
            items = [ListItem(Paragraph(_pdf_text(d), bullet_style), leftIndent=14) for d in data["data_checks"]]
            elements.append(ListFlowable(items, bulletType="bullet", start="•", leftIndent=14))

    else:
        elements.append(Paragraph("City profile not yet available.", normal_style))

    # 2.4 STP analysis — starts on a new page, same for every city (profile or not)
    elements.append(PageBreak())
    elements.append(Paragraph("2.4 STP analysis", h1_style))

    elements.append(Paragraph("Ward-wise population and sewage", h2_style))
    cols = ["wardno", "wardname", "total_popu", "SEWAGE_MLD", "POP_2035", "SEWAGE_203"]
    headers = ["Ward No.", "Ward Name", "Population (2011)", "Sewage (2011, MLD)", "Population (2035)", "Sewage (2035, MLD)"]
    decimal_cols = {"SEWAGE_MLD", "SEWAGE_203"}
    int_cols = {"total_popu", "POP_2035"}

    ward_rows = [[Paragraph(h, header_cell_style) for h in headers]]
    for rec in data["ward_records"]:
        row = []
        for c in cols:
            v = rec.get(c, "—")
            if c in decimal_cols:
                v = _fmt_mld(v)
            elif c in int_cols:
                v = _fmt_int(v)
            row.append(cell(v))
        ward_rows.append(row)

    def _sum(colname):
        total, has_value = 0.0, False
        for rec in data["ward_records"]:
            v = rec.get(colname)
            if v in (None, "", "—"):
                continue
            try:
                total += float(v); has_value = True
            except (TypeError, ValueError):
                pass
        return total if has_value else None

    totals = {c: _sum(c) for c in ["total_popu", "SEWAGE_MLD", "POP_2035", "SEWAGE_203"]}
    ward_rows.append([
        cell("TOTAL", bold=True), cell("", bold=True),
        cell(_fmt_int(totals["total_popu"]), bold=True),
        cell(_fmt_mld(totals["SEWAGE_MLD"]), bold=True),
        cell(_fmt_int(totals["POP_2035"]), bold=True),
        cell(_fmt_mld(totals["SEWAGE_203"]), bold=True),
    ])
    ward_table = Table(ward_rows, colWidths=[0.65 * inch, 1.95 * inch, 1.2 * inch, 1.3 * inch, 1.2 * inch, 1.3 * inch],
                        repeatRows=1)
    ward_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEADING_COLOR),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f0f4ff")]),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#dbe6ff")),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d1d5db")),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, HEADING_COLOR),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("PADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(ward_table)
    elements.append(Spacer(1, 0.25 * inch))

    if data["stp_summary"].get("available"):
        elements.append(Paragraph("Proposed STP locations", h2_style))
        elements.append(Paragraph(
            f"Total proposed STPs: {data['stp_summary']['count']}  &nbsp;&middot;&nbsp;  "
            f"Total capacity: {_fmt_mld(data['stp_summary'].get('total_capacity_mld'))} MLD  &nbsp;&middot;&nbsp;  "
            f"Average suitability score: {data['stp_summary'].get('avg_score', '—')}",
            normal_style,
        ))
        elements.append(Spacer(1, 0.1 * inch))
        stp_headers = ["STP ID", "Capacity (MLD)", "Elevation (m)", "Flood Score", "Suitability Score", "Latitude", "Longitude"]
        stp_rows = [[Paragraph(h, header_cell_style) for h in stp_headers]]
        for r in data["stp_table"]:
            stp_rows.append([
                cell(r.get("stp_id", "—")), cell(_fmt_mld(r.get("Capacity_MLD"))),
                cell(_fmt_mld(r.get("Elevation"))), cell(_fmt_mld(r.get("FloodScore"))),
                cell(_fmt_mld(r.get("Score"))), cell(_fmt_coord(r.get("latitude"))), cell(_fmt_coord(r.get("longitude"))),
            ])
        stp_table_fl = Table(stp_rows, colWidths=[0.9 * inch, 1.2 * inch, 1.2 * inch, 1.1 * inch, 1.3 * inch, 1.1 * inch, 1.1 * inch],
                              repeatRows=1)
        stp_table_fl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16a34a")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0fdf4")]),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d1d5db")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("PADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(stp_table_fl)
        elements.append(Spacer(1, 0.2 * inch))

        weights = data["stp_weights"]
        if weights:
            elements.append(Paragraph("Site-suitability scoring criteria", h2_style))
            weight_labels = {
                "elev": "Elevation", "flood": "Flood risk", "sewer": "Proximity to sewers",
                "stream": "Proximity to streams", "drain": "Proximity to drains", "wind": "Wind direction",
            }
            wrows = [[cell("Criterion", bold=True), cell("Weight", bold=True)]]
            for key, label in weight_labels.items():
                if key in weights:
                    wrows.append([cell(label), cell(f"{weights[key] * 100:.0f}%")])
            wtable = Table(wrows, colWidths=[2.5 * inch, 1.2 * inch])
            wtable.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f4ff")),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#d1d5db")),
                ("PADDING", (0, 0), (-1, -1), 4),
            ]))
            elements.append(wtable)
    else:
        elements.append(Paragraph("Proposed STP locations", h2_style))
        elements.append(Paragraph("No STP suitability analysis has been run for this city yet.", normal_style))

    # 2.5 Key findings and recommendations — only for cities with a profile
    if city_record and (data["findings"] or data["recommendations"]):
        elements.append(Spacer(1, 0.2 * inch))
        elements.append(Paragraph("2.5 Key findings and recommendations", h1_style))
        if data["findings"]:
            elements.append(Paragraph("Key findings", h3_style))
            items = [ListItem(Paragraph(_pdf_text(f), bullet_style), leftIndent=14) for f in data["findings"]]
            elements.append(ListFlowable(items, bulletType="bullet", start="•", leftIndent=14))
        if data["recommendations"]:
            elements.append(Paragraph("Recommendations", h3_style))
            items = [ListItem(Paragraph(_pdf_text(r), bullet_style), leftIndent=14) for r in data["recommendations"]]
            elements.append(ListFlowable(items, bulletType="bullet", start="•", leftIndent=14))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=1 * inch, rightMargin=1 * inch,
        topMargin=1.1 * inch, bottomMargin=0.9 * inch,
    )
    header_footer = _make_header_footer(display_name, data["generated_str"])
    doc.build(elements, onFirstPage=header_footer, onLaterPages=header_footer)
    buf.seek(0)
    return buf.getvalue()


@reports_bp.route("/pdf/ward-report")
def export_ward_pdf():
    """Generate the full DRMP report (Part 1 common + Part 2 selected city) as a PDF."""
    city = _get_city()
    draft = _get_draft_flag()
    try:
        from reportlab.platypus import SimpleDocTemplate  # noqa: F401 - import check below gives a clean 501
    except ImportError:
        return jsonify({"error": "ReportLab not installed. Install with: pip install reportlab"}), 501

    try:
        city = validate_city(city)
        data = _gather_report_data(city, draft)
        pdf_bytes = _build_pdf(data)
        return send_file(
            io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=True,
            download_name=_report_filename(city, "pdf"),
        )
    except Exception as e:
        logger.exception("DRMP PDF report error")
        return jsonify({"error": str(e)}), 500


# ── DOCX ─────────────────────────────────────────────────────────────────────

def _build_docx(data: dict) -> bytes:
    import docx
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    HEADING_RGB = RGBColor(0x0F, 0x47, 0x61)

    doc = docx.Document()

    section = doc.sections[0]
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    section.left_margin = Inches(1)
    section.right_margin = Inches(1)
    section.top_margin = Inches(1)
    section.bottom_margin = Inches(0.9)

    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(11)

    def set_heading_style(style_name, size):
        try:
            style = doc.styles[style_name]
            style.font.name = "Times New Roman"
            style.font.size = Pt(size)
            style.font.color.rgb = HEADING_RGB
            style.font.bold = True
        except KeyError:
            pass

    set_heading_style("Heading 1", 18)
    set_heading_style("Heading 2", 14)
    set_heading_style("Heading 3", 12)

    def add_page_number_field(paragraph):
        run = paragraph.add_run()
        fld_begin = OxmlElement("w:fldChar"); fld_begin.set(qn("w:fldCharType"), "begin")
        instr = OxmlElement("w:instrText"); instr.set(qn("xml:space"), "preserve"); instr.text = "PAGE"
        fld_end = OxmlElement("w:fldChar"); fld_end.set(qn("w:fldCharType"), "end")
        run._r.append(fld_begin); run._r.append(instr); run._r.append(fld_end)

    header_p = section.header.paragraphs[0]
    header_p.text = f"DRMP — {data['city_record']['name'] if data['city_record'] else data['city']}"
    header_p.runs[0].font.name = "Times New Roman"
    header_p.runs[0].font.bold = True
    header_p.runs[0].font.size = Pt(10)

    footer_p = section.footer.paragraphs[0]
    footer_run = footer_p.add_run(f"{data['generated_str']}   ·   Page ")
    footer_run.font.name = "Times New Roman"
    footer_run.font.size = Pt(8)
    add_page_number_field(footer_p)

    def para(text, style=None, bold=False, italic=False, size=None, color=None):
        p = doc.add_paragraph(style=style)
        run = p.add_run(text)
        run.font.name = "Times New Roman"
        if bold: run.font.bold = True
        if italic: run.font.italic = True
        if size: run.font.size = Pt(size)
        if color: run.font.color.rgb = color
        return p

    def bullets(items):
        for it in items:
            p = doc.add_paragraph(style="List Bullet")
            run = p.add_run(it)
            run.font.name = "Times New Roman"

    def kv_table(rows, col_widths, header_row=None, bold_first_col=False, header_bg="0F4761"):
        n_cols = len(rows[0])
        t = doc.add_table(rows=0, cols=n_cols)
        t.alignment = WD_TABLE_ALIGNMENT.LEFT
        t.style = "Table Grid"
        if header_row:
            hr = t.add_row()
            for i, text in enumerate(header_row):
                hr.cells[i].text = ""
                rp = hr.cells[i].paragraphs[0].add_run(text)
                rp.font.name = "Times New Roman"; rp.font.bold = True; rp.font.size = Pt(9)
                rp.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                _shade_cell(hr.cells[i], header_bg)
        for row in rows:
            r = t.add_row()
            for i, text in enumerate(row):
                r.cells[i].text = ""
                rp = r.cells[i].paragraphs[0].add_run(str(text) if text not in (None, "") else "—")
                rp.font.name = "Times New Roman"; rp.font.size = Pt(9)
                if bold_first_col and i == 0:
                    rp.font.bold = True
        for i, w in enumerate(col_widths):
            for row in t.rows:
                row.cells[i].width = Inches(w)
        return t

    def _shade_cell(cell, hex_color):
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto"); shd.set(qn("w:fill"), hex_color)
        cell._tc.get_or_add_tcPr().append(shd)

    # ── Part 1 (common) ──────────────────────────────────────────────────
    common = data["common"]
    title_p = doc.add_paragraph()
    title_run = title_p.add_run(common["title"])
    title_run.font.name = "Times New Roman"; title_run.font.bold = True
    title_run.font.size = Pt(26); title_run.font.color.rgb = HEADING_RGB

    para(common["heading"], style="Heading 1")
    for sub in common["subsections"]:
        para(sub["heading"], style="Heading 3")
        for p in sub.get("paragraphs", []):
            para(p)
        if sub.get("bullets"):
            bullets(sub["bullets"])

    doc.add_page_break()

    # ── Part 2 (selected city) ───────────────────────────────────────────
    city_record = data["city_record"]
    display_name = city_record["name"] if city_record else data["city"]
    para(f"2. {display_name}", style="Heading 1")

    if city_record:
        para(city_record["subtitle"], bold=True, color=RGBColor(0x44, 0x44, 0x44))
        para(city_record["intro"])

        para("2.1 City profile", style="Heading 2")
        profile = city_record["profile"]
        area_ha = profile.get("area_ha")
        area_text = f"{_fmt_int(area_ha)} ha" if area_ha is not None else "—"
        if area_ha is not None:
            area_text += f" ({area_ha / 100:,.2f} km²)"
        profile_rows = [
            ("ULB", city_record["name"]),
            ("District", f"{city_record['district']} district"),
            ("Area", area_text),
            ("Wards", _fmt_int(profile.get("wards"))),
            ("Population", _fmt_int(profile.get("population"))),
            ("Distance from the Narmada", profile.get("distance_from_narmada") or "—"),
            ("Distance from Bhopal", profile.get("distance_from_bhopal") or "—"),
            (profile.get("last_row_label", "Known for"), profile.get("last_row_value") or "—"),
        ]

        # Side-by-side layout: profile table on the left, photo + caption on
        # the right — a borderless 1x2 outer table with the real profile
        # table nested in its left cell, matching the PDF's layout.
        outer = doc.add_table(rows=1, cols=2)
        outer.autofit = False
        left_cell, right_cell = outer.cell(0, 0), outer.cell(0, 1)
        left_cell.width = Inches(4.3)
        right_cell.width = Inches(2.6)

        nested = left_cell.add_table(rows=0, cols=2)
        nested.style = "Table Grid"
        for k, v in profile_rows:
            r = nested.add_row()
            r.cells[0].text = ""
            kr = r.cells[0].paragraphs[0].add_run(k)
            kr.font.name = "Times New Roman"; kr.font.bold = True; kr.font.size = Pt(9)
            r.cells[1].text = ""
            vr = r.cells[1].paragraphs[0].add_run(str(v) if v not in (None, "") else "—")
            vr.font.name = "Times New Roman"; vr.font.size = Pt(9)
        for row in nested.rows:
            row.cells[0].width = Inches(1.9)
            row.cells[1].width = Inches(2.3)
        # Drop the empty paragraph add_table() leaves at the top of the cell
        left_cell.paragraphs[0]._element.getparent().remove(left_cell.paragraphs[0]._element)

        photo = report_content.photo_path(city_record)
        right_cell.paragraphs[0].text = ""
        if photo:
            try:
                run = right_cell.paragraphs[0].add_run()
                run.add_picture(str(photo), width=Inches(2.3))
                if city_record.get("photo_caption"):
                    cap_p = right_cell.add_paragraph()
                    cap_run = cap_p.add_run(city_record["photo_caption"])
                    cap_run.font.name = "Times New Roman"; cap_run.font.italic = True
                    cap_run.font.size = Pt(9); cap_run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
            except Exception:
                logger.exception("Could not embed city photo (DOCX) for %s", data["city"])
                right_cell.paragraphs[0].add_run("(photo unavailable)")
        else:
            right_cell.paragraphs[0].add_run("(no photo available)")

        doc.add_paragraph()

        para("2.2 Water supply and sewerage", style="Heading 2")
        ws = city_record.get("water_supply", {})
        sw = city_record.get("sewerage", {})
        field_order = [
            ("status", "Status"), ("cost", "Cost"), ("funding", "Funding"),
            ("water_source", "Water source"), ("treatment_plant", "Treatment plant"),
            ("storage_pumping", "Storage / pumping"), ("network", "Network"),
            ("house_connections", "House connections"),
        ]
        rows = [(label, ws.get(key) or "—", sw.get(key) or "—") for key, label in field_order]
        kv_table(rows, col_widths=[1.5, 2.5, 2.5], header_row=["", "Water supply", "Sewerage"], bold_first_col=True)

        if data["computed_line"]:
            para(data["computed_line"])

        para("2.3 Future plans listed by the ULB", style="Heading 2")
        future_plans = city_record.get("future_plans") or []
        if future_plans:
            bullets(future_plans)
        else:
            para("None listed.")

        if data["data_checks"]:
            para("Data checks", style="Heading 3")
            bullets(data["data_checks"])
    else:
        para("City profile not yet available.")

    doc.add_page_break()
    para("2.4 STP analysis", style="Heading 1")

    para("Ward-wise population and sewage", style="Heading 2")
    headers = ["Ward No.", "Ward Name", "Population (2011)", "Sewage (2011, MLD)", "Population (2035)", "Sewage (2035, MLD)"]
    cols = ["wardno", "wardname", "total_popu", "SEWAGE_MLD", "POP_2035", "SEWAGE_203"]
    decimal_cols = {"SEWAGE_MLD", "SEWAGE_203"}
    int_cols = {"total_popu", "POP_2035"}
    rows = []
    for rec in data["ward_records"]:
        row = []
        for c in cols:
            v = rec.get(c, "—")
            if c in decimal_cols: v = _fmt_mld(v)
            elif c in int_cols: v = _fmt_int(v)
            row.append(v)
        rows.append(row)

    def _sum(colname):
        total, has_value = 0.0, False
        for rec in data["ward_records"]:
            v = rec.get(colname)
            if v in (None, "", "—"): continue
            try:
                total += float(v); has_value = True
            except (TypeError, ValueError):
                pass
        return total if has_value else None

    totals = {c: _sum(c) for c in ["total_popu", "SEWAGE_MLD", "POP_2035", "SEWAGE_203"]}
    rows.append([
        "TOTAL", "", _fmt_int(totals["total_popu"]), _fmt_mld(totals["SEWAGE_MLD"]),
        _fmt_int(totals["POP_2035"]), _fmt_mld(totals["SEWAGE_203"]),
    ])
    ward_table = kv_table(rows, col_widths=[0.7, 1.7, 1.1, 1.2, 1.1, 1.2], header_row=headers)
    # Bold the TOTAL row
    for cell_ in ward_table.rows[-1].cells:
        for p in cell_.paragraphs:
            for r in p.runs:
                r.font.bold = True

    if data["stp_summary"].get("available"):
        para("Proposed STP locations", style="Heading 2")
        para(
            f"Total proposed STPs: {data['stp_summary']['count']}  ·  "
            f"Total capacity: {_fmt_mld(data['stp_summary'].get('total_capacity_mld'))} MLD  ·  "
            f"Average suitability score: {data['stp_summary'].get('avg_score', '—')}"
        )
        stp_headers = ["STP ID", "Capacity (MLD)", "Elevation (m)", "Flood Score", "Suitability Score", "Latitude", "Longitude"]
        stp_rows = [
            [r.get("stp_id", "—"), _fmt_mld(r.get("Capacity_MLD")), _fmt_mld(r.get("Elevation")),
             _fmt_mld(r.get("FloodScore")), _fmt_mld(r.get("Score")), _fmt_coord(r.get("latitude")), _fmt_coord(r.get("longitude"))]
            for r in data["stp_table"]
        ]
        kv_table(stp_rows, col_widths=[0.9, 1.1, 1.1, 1.0, 1.2, 1.0, 1.0], header_row=stp_headers, header_bg="16A34A")

        weights = data["stp_weights"]
        if weights:
            para("Site-suitability scoring criteria", style="Heading 2")
            weight_labels = {
                "elev": "Elevation", "flood": "Flood risk", "sewer": "Proximity to sewers",
                "stream": "Proximity to streams", "drain": "Proximity to drains", "wind": "Wind direction",
            }
            wrows = [(label, f"{weights[key] * 100:.0f}%") for key, label in weight_labels.items() if key in weights]
            kv_table(wrows, col_widths=[2.5, 1.2], header_row=["Criterion", "Weight"])
    else:
        para("Proposed STP locations", style="Heading 2")
        para("No STP suitability analysis has been run for this city yet.")

    if city_record and (data["findings"] or data["recommendations"]):
        para("2.5 Key findings and recommendations", style="Heading 1")
        if data["findings"]:
            para("Key findings", style="Heading 3")
            bullets(data["findings"])
        if data["recommendations"]:
            para("Recommendations", style="Heading 3")
            bullets(data["recommendations"])

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.getvalue()


@reports_bp.route("/docx/drmp-report")
def export_drmp_docx():
    """Generate the full DRMP report (Part 1 common + Part 2 selected city) as a DOCX."""
    city = _get_city()
    draft = _get_draft_flag()
    try:
        import docx  # noqa: F401
    except ImportError:
        return jsonify({"error": "python-docx not installed. Install with: pip install python-docx"}), 501

    try:
        city = validate_city(city)
        data = _gather_report_data(city, draft)
        docx_bytes = _build_docx(data)
        return send_file(
            io.BytesIO(docx_bytes),
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True, download_name=_report_filename(city, "docx"),
        )
    except Exception as e:
        logger.exception("DRMP DOCX report error")
        return jsonify({"error": str(e)}), 500


def _now():
    return datetime.now().strftime("%Y%m%d_%H%M")
