"""
utils/gis_analysis.py  — Spatial analysis functions for DRMP.
Reuses & extends the core pipeline from the original core.py.
"""
import logging
import warnings
from typing import Dict, Any, List, Optional, Tuple

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point, mapping
from shapely.ops import unary_union

warnings.filterwarnings("ignore")
logger = logging.getLogger("drmp.analysis")


# ── Normalisation ─────────────────────────────────────────────────────────────

def normalize(series: pd.Series) -> pd.Series:
    s = series.replace([np.inf, -np.inf], np.nan).fillna(series.median())
    rng = s.max() - s.min()
    return (s - s.min()) / rng if rng != 0 else pd.Series(np.ones(len(s)), index=s.index)


# ── Risk classification ───────────────────────────────────────────────────────

FLOOD_SCORE_MAP = {"Very Low": 1.0, "Low": 0.7, "Moderate": 0.4, "High": 0.2, "Very High": 0.0}

def classify_risk(elevation: Optional[float],
                  slope: Optional[float],
                  stream_dist: Optional[float],
                  drain_density: Optional[float]) -> str:
    """
    Multi-criteria risk classification.
    Returns 'High', 'Medium', or 'Low'.
    """
    score = 0
    factors = 0

    if elevation is not None:
        factors += 1
        if elevation < 300:    score += 2
        elif elevation < 350:  score += 1

    if slope is not None:
        factors += 1
        if slope > 15:   score += 2
        elif slope > 5:  score += 1

    if stream_dist is not None:
        factors += 1
        if stream_dist < 200:    score += 2
        elif stream_dist < 500:  score += 1

    if drain_density is not None:
        factors += 1
        if drain_density > 0.7:   score += 2
        elif drain_density > 0.3: score += 1

    if factors == 0:
        return "Low"
    ratio = score / (factors * 2)
    if ratio >= 0.6:  return "High"
    if ratio >= 0.3:  return "Medium"
    return "Low"


def enrich_wards_risk(wards: gpd.GeoDataFrame,
                      streams: Optional[gpd.GeoDataFrame] = None,
                      drains: Optional[gpd.GeoDataFrame] = None) -> gpd.GeoDataFrame:
    """
    Compute risk classification for each ward based on GIS parameters.
    """
    gdf = wards.copy()

    # Stream proximity
    if streams is not None and len(streams) > 0:
        stream_union = streams.to_crs(gdf.crs if gdf.crs else 4326).union_all()
        gdf["stream_dist_m"] = (
            gdf.to_crs(32644).geometry.centroid
               .distance(streams.to_crs(32644).union_all())
        )
    else:
        gdf["stream_dist_m"] = None

    # Drain density (drains per km²)
    if drains is not None and len(drains) > 0:
        ward_utm = gdf.to_crs(32644)
        drain_utm = drains.to_crs(32644)
        gdf["drain_density"] = 0.0
        for idx, ward_row in ward_utm.iterrows():
            try:
                clip = drain_utm.clip(ward_row.geometry)
                area_km2 = ward_row.geometry.area / 1e6
                gdf.at[idx, "drain_density"] = (len(clip) / area_km2) if area_km2 > 0 else 0
            except Exception:
                pass
        # Normalize drain density
        mx = gdf["drain_density"].max()
        gdf["drain_density_norm"] = gdf["drain_density"] / mx if mx > 0 else 0
    else:
        gdf["drain_density_norm"] = None

    # Elevation from columns if available
    elev_col = next((c for c in ("elevation", "Elevation", "elev", "ELEVATION") if c in gdf.columns), None)

    gdf["risk_class"] = gdf.apply(lambda row: classify_risk(
        elevation=float(row[elev_col]) if elev_col and pd.notna(row[elev_col]) else None,
        slope=None,
        stream_dist=float(row["stream_dist_m"]) if "stream_dist_m" in row.index and pd.notna(row["stream_dist_m"]) else None,
        drain_density=float(row["drain_density_norm"]) if "drain_density_norm" in row.index and pd.notna(row["drain_density_norm"]) else None,
    ), axis=1)

    return gdf


# ── Buffer analysis ───────────────────────────────────────────────────────────

def buffer_analysis(gdf: gpd.GeoDataFrame,
                    distance_m: float,
                    clip_to: Optional[gpd.GeoDataFrame] = None) -> gpd.GeoDataFrame:
    """Create buffer around features, optionally clip to boundary."""
    utm = gdf.estimate_utm_crs()
    utm_gdf = gdf.to_crs(utm)
    buffered = utm_gdf.copy()
    buffered["geometry"] = utm_gdf.geometry.buffer(distance_m)
    buffered["buffer_m"] = distance_m
    result = buffered.to_crs(4326)

    if clip_to is not None:
        boundary = clip_to.to_crs(4326).union_all()
        result = result.copy()
        result["geometry"] = result.geometry.intersection(boundary)
        result = result[~result.geometry.is_empty]

    return result


# ── Intersection analysis ─────────────────────────────────────────────────────

def intersection_analysis(layer_a: gpd.GeoDataFrame,
                           layer_b: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Spatial intersection of two layers."""
    a = layer_a.to_crs(4326)
    b = layer_b.to_crs(4326)
    return gpd.overlay(a, b, how="intersection", keep_geom_type=False)


# ── Nearest feature ───────────────────────────────────────────────────────────

def nearest_feature(source: gpd.GeoDataFrame,
                    target: gpd.GeoDataFrame,
                    k: int = 1) -> gpd.GeoDataFrame:
    """Find nearest features in target for each row in source."""
    src_utm = source.to_crs(32644)
    tgt_utm = target.to_crs(32644)
    result = src_utm.sjoin_nearest(tgt_utm, how="left",
                                   distance_col="nearest_dist_m",
                                   max_distance=None)
    result = result.to_crs(4326)
    return result


# ── Density analysis ──────────────────────────────────────────────────────────

def density_by_ward(point_layer: gpd.GeoDataFrame,
                    wards: gpd.GeoDataFrame,
                    feature_name: str = "features") -> gpd.GeoDataFrame:
    """Count point features per ward and compute density (per km²)."""
    pts  = point_layer.to_crs(4326)
    wds  = wards.to_crs(4326).copy()
    joined = gpd.sjoin(pts, wds[["geometry", "wardno", "wardname"]], how="left", predicate="within")
    counts = joined.groupby("wardno").size().rename("count")
    wds = wds.join(counts, on="wardno")
    wds["count"] = wds["count"].fillna(0).astype(int)
    wds_utm = wds.to_crs(32644)
    wds["area_km2"] = wds_utm.geometry.area / 1e6
    wds[f"{feature_name}_density"] = wds["count"] / wds["area_km2"].replace(0, np.nan)
    return wds


# ── Statistics ────────────────────────────────────────────────────────────────

def ward_statistics(wards: gpd.GeoDataFrame) -> List[Dict[str, Any]]:
    """Compute per-ward statistics from attribute data."""
    records = []
    wards_utm = wards.to_crs(32644)
    for _, row in wards.iterrows():
        rec: Dict[str, Any] = {}
        for col in ("wardno", "wardname", "ub_nm_e", "ulbname",
                    "total_popu", "no_of_hous", "population",
                    "SEWAGE_MLD", "SEWAGE_LPD", "POP_2035", "SEWAGE_203",
                    "area", "perimeter", "risk_class",
                    "male_popul", "female_pop"):
            if col in wards.columns:
                v = row[col]
                rec[col] = None if pd.isna(v) else v
        # Geometry area
        idx = row.name
        try:
            rec["area_km2"] = round(wards_utm.at[idx, "geometry"].area / 1e6, 4)
        except Exception:
            pass
        records.append(rec)
    return records


def summary_stats(wards: gpd.GeoDataFrame,
                  streams: Optional[gpd.GeoDataFrame] = None,
                  drains: Optional[gpd.GeoDataFrame] = None,
                  sewer: Optional[gpd.GeoDataFrame] = None) -> Dict[str, Any]:
    """Generate dashboard summary statistics."""
    stats: Dict[str, Any] = {}

    # Wards
    stats["total_wards"] = int(len(wards))
    if "total_popu" in wards.columns:
        stats["total_population"] = int(wards["total_popu"].sum())
    if "SEWAGE_MLD" in wards.columns:
        stats["total_sewage_mld"] = round(float(wards["SEWAGE_MLD"].sum()), 2)
    if "POP_2035" in wards.columns:
        stats["projected_population_2035"] = int(wards["POP_2035"].sum())
    if "SEWAGE_203" in wards.columns:
        stats["projected_sewage_2035_mld"] = round(float(wards["SEWAGE_203"].sum()), 2)

    # Area
    try:
        utm = wards.estimate_utm_crs()
        stats["total_area_km2"] = round(float(wards.to_crs(utm).geometry.area.sum() / 1e6), 2)
    except Exception:
        pass

    # Streams
    if streams is not None:
        stats["total_streams"] = int(len(streams))
        if "River_Name" in streams.columns:
            stats["unique_rivers"] = int(streams["River_Name"].nunique())
        if "River_Orde" in streams.columns:
            stats["stream_orders"] = sorted(streams["River_Orde"].dropna().unique().tolist())
        try:
            stream_utm = streams.to_crs(32644)
            stats["total_stream_length_km"] = round(float(stream_utm.geometry.length.sum() / 1000), 2)
        except Exception:
            pass

    # Drains
    if drains is not None:
        stats["total_drains"] = int(len(drains))
        try:
            drain_utm = drains.to_crs(32644)
            stats["total_drain_length_km"] = round(float(drain_utm.geometry.length.sum() / 1000), 2)
        except Exception:
            pass

    # Sewer
    if sewer is not None:
        stats["total_sewer_pipes"] = int(len(sewer))
        try:
            sewer_utm = sewer.to_crs(32644)
            stats["total_sewer_length_km"] = round(float(sewer_utm.geometry.length.sum() / 1000), 2)
        except Exception:
            pass

    return stats
