"""
utils/gis_loader.py  — GeoPandas / Rasterio data loader with in-process cache.
Designed to be imported once; all functions are stateless beyond the cache.
"""
import logging
import warnings
from pathlib import Path
from functools import lru_cache
from typing import Optional, Dict, Any

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds
from shapely.geometry import mapping

warnings.filterwarnings("ignore")
logger = logging.getLogger("drmp.loader")

# Attribute columns that may carry the ULB name, in priority order.
_ULB_COLS = ("ub_nm_e", "ulb_nm", "ulbname", "ULB_NAME")


# ── Layer loader ──────────────────────────────────────────────────────────────

@lru_cache(maxsize=8)
def load_layer(path: str, ulb_filter: Optional[str] = None) -> gpd.GeoDataFrame:
    """
    Load shapefile, optionally filter by ULB name, and return WGS84 GeoDataFrame.
    Results are cached per (path, ulb_filter).

    The ULB filter is pushed down to the OGR driver so that non-matching
    features are never read into memory. This matters for sewer_network,
    which has 46,444 statewide features of which most cities need a few
    hundred at most.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Layer not found: {path}")

    # Build a driver-level attribute filter when possible.
    where = None
    if ulb_filter:
        try:
            fields = set(pyogrio.read_info(str(p))["fields"])
            col = next((c for c in _ULB_COLS if c in fields), None)
            if col:
                safe = str(ulb_filter).replace("'", "''")
                where = f'"{col}" = \'{safe}\''
        except Exception as exc:  # pragma: no cover - driver introspection
            logger.debug("Filter pushdown unavailable for %s: %s", p.name, exc)

    logger.info(
        "Loading layer: %s%s", p.name, f" [{ulb_filter}]" if ulb_filter else ""
    )
    gdf = gpd.read_file(str(p), engine="pyogrio", where=where)

    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)

    # Fallback for the case where the driver could not apply the filter.
    if ulb_filter and where is None:
        for col in _ULB_COLS:
            if col in gdf.columns:
                gdf = gdf[gdf[col] == ulb_filter]
                break

    gdf = gdf.dropna(subset=["geometry"])
    gdf = gdf[~gdf.geometry.is_empty]
    logger.info("Loaded %d features from %s", len(gdf), p.name)
    return gdf.reset_index(drop=True)


def clear_layer_cache() -> None:
    """Drop all cached GeoDataFrames. Useful for freeing memory."""
    load_layer.cache_clear()
    dem_summary.cache_clear()
    logger.info("Layer cache cleared")


def to_geojson(gdf: gpd.GeoDataFrame,
               simplify: float = 0.0,
               max_features: int = 10000) -> Dict[str, Any]:
    """Convert GeoDataFrame → GeoJSON dict, with optional simplification."""
    if len(gdf) > max_features:
        logger.warning("Truncating layer from %d to %d features", len(gdf), max_features)
        gdf = gdf.head(max_features)

    if simplify > 0:
        gdf = gdf.copy()
        gdf["geometry"] = gdf["geometry"].simplify(simplify, preserve_topology=True)

    # Drop NaN / non-serialisable values
    gdf = gdf.copy()
    for col in gdf.select_dtypes(include=["object"]).columns:
        gdf[col] = gdf[col].where(gdf[col].notna(), other=None)
    for col in gdf.select_dtypes(include=["float"]).columns:
        gdf[col] = gdf[col].where(gdf[col].notna(), other=None)

    return gdf.__geo_interface__


def layer_bounds(path: str, ulb_filter: Optional[str] = None) -> Dict[str, float]:
    """Return bounding box of a layer as {minx, miny, maxx, maxy}."""
    gdf = load_layer(path, ulb_filter)
    if gdf.empty:
        return {"minx": None, "miny": None, "maxx": None, "maxy": None}
    b = gdf.total_bounds
    return {"minx": b[0], "miny": b[1], "maxx": b[2], "maxy": b[3]}


# ── DEM utilities ─────────────────────────────────────────────────────────────

def open_dem(path: str) -> rasterio.DatasetReader:
    """Open DEM raster (not cached — caller manages lifecycle)."""
    if not Path(path).exists():
        raise FileNotFoundError(f"DEM not found: {path}")
    return rasterio.open(path)


@lru_cache(maxsize=1)
def dem_summary(path: str, step: int = 10) -> Dict[str, Any]:
    """
    Read every `step`-th pixel from the DEM and return summary statistics.

    Reads into float32 rather than float64 and computes statistics in place,
    which avoids three full-size intermediate copies. Cached because the
    result never changes for a given raster.
    """
    with open_dem(path) as src:
        out_shape = (
            1,
            max(1, src.height // step),
            max(1, src.width  // step),
        )
        data = src.read(
            1,
            out_shape=out_shape,
            resampling=Resampling.average,
            out_dtype="float32",
        )

        nodata = src.nodata
        if nodata is not None:
            data[data == nodata] = np.nan

        bounds = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        has_valid = bool(np.isfinite(data).any())

        result = {
            "crs": str(src.crs),
            "epsg": src.crs.to_epsg(),
            "width": src.width,
            "height": src.height,
            "resolution_x": src.res[0],
            "resolution_y": src.res[1],
            "nodata": float(nodata) if nodata is not None else None,
            "bounds_wgs84": {
                "west":  bounds[0], "south": bounds[1],
                "east":  bounds[2], "north": bounds[3],
            },
            "elevation": {
                "min":    float(np.nanmin(data))    if has_valid else None,
                "max":    float(np.nanmax(data))    if has_valid else None,
                "mean":   float(np.nanmean(data))   if has_valid else None,
                "std":    float(np.nanstd(data))    if has_valid else None,
                "median": float(np.nanmedian(data)) if has_valid else None,
            },
        }

        del data
        return result


def dem_tile(path: str, west: float, south: float,
             east: float, north: float,
             out_width: int = 256, out_height: int = 256) -> np.ndarray:
    """
    Read a spatial tile from the DEM and return as 2D numpy array.
    Returns float32 array with NaN for nodata.
    """
    from rasterio.windows import from_bounds
    with open_dem(path) as src:
        win = from_bounds(west, south, east, north, src.transform)
        data = src.read(
            1, window=win,
            out_shape=(out_height, out_width),
            resampling=Resampling.bilinear,
            out_dtype="float32",
        )
        if src.nodata is not None:
            data[data == src.nodata] = np.nan
        return data


def compute_slope(dem_arr: np.ndarray, res_x: float = 30.0, res_y: float = 30.0) -> np.ndarray:
    """Compute slope in degrees from a 2D elevation array."""
    # Gradient in x and y
    gy, gx = np.gradient(np.where(np.isnan(dem_arr), 0, dem_arr), res_y, res_x)
    slope = np.degrees(np.arctan(np.sqrt(gx**2 + gy**2)))
    slope[np.isnan(dem_arr)] = np.nan
    return slope


def compute_hillshade(dem_arr: np.ndarray,
                      azimuth: float = 315.0,
                      altitude: float = 45.0,
                      res: float = 30.0) -> np.ndarray:
    """Compute hillshade from a 2D elevation array."""
    az_rad = np.radians(360 - azimuth + 90)
    alt_rad = np.radians(altitude)
    gy, gx = np.gradient(np.where(np.isnan(dem_arr), 0, dem_arr), res, res)
    slope_r = np.arctan(np.sqrt(gx**2 + gy**2))
    aspect_r = np.pi / 2.0 - np.arctan2(-gy, gx)
    hs = (np.cos(alt_rad) * np.cos(slope_r) +
          np.sin(alt_rad) * np.sin(slope_r) * np.cos(az_rad - aspect_r))
    hs = np.clip(hs * 255, 0, 255).astype(np.uint8)
    hs[np.isnan(dem_arr)] = 0
    return hs


# ── Geometry helpers ──────────────────────────────────────────────────────────

def safe_area_km2(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Add area_km2 column using UTM projection.

    Returns an empty frame with the column present when there are no
    features. An empty GeoDataFrame has all-NaN total_bounds, which makes
    estimate_utm_crs() raise inside pyproj.
    """
    gdf = gdf.copy()
    if gdf.empty:
        gdf["area_km2"] = pd.Series(dtype="float64")
        return gdf

    if gdf.crs and gdf.crs.to_epsg() == 4326:
        utm = gdf.estimate_utm_crs()
        gdf["area_km2"] = gdf.to_crs(utm).geometry.area / 1e6
    else:
        gdf["area_km2"] = gdf.geometry.area / 1e6
    return gdf


def safe_length_km(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Add length_km column using UTM projection.

    Returns an empty frame with the column present when there are no
    features. See safe_area_km2 for why.
    """
    gdf = gdf.copy()
    if gdf.empty:
        gdf["length_km"] = pd.Series(dtype="float64")
        return gdf

    if gdf.crs and gdf.crs.to_epsg() == 4326:
        utm = gdf.estimate_utm_crs()
        gdf["length_km"] = gdf.to_crs(utm).geometry.length / 1000
    else:
        gdf["length_km"] = gdf.geometry.length / 1000
    return gdf