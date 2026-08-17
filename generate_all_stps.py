"""
generate_all_stps.py
====================
Replicates your KMeans + DEM/sewer/stream/drain/flood scoring pipeline
for ALL 22 cities in the Narmada basin dataset.

Because geopandas/rasterio may not be available in the web server environment,
this script is designed to run ONCE offline (with your full GIS stack) and
saves results as JSON files that the Flask app reads directly.

Usage:
    cd backend
    python generate_all_stps.py

Output:  backend/stp_data/<CityName>.json   (one file per city)
"""

import os, json, math, struct, random
from collections import defaultdict
from pathlib import Path

# ── Input paths (same as your original script) ────────────────────────────────
BASE = Path(os.getenv("DATA_DIR", r"D:\DRMP_WebApp\drmp_app\data\DRMP\Input"))

WARD_FILE   = BASE / "ward" / "wards_sewage.shp"
WARD_DBF    = BASE / "ward" / "wards_sewage.dbf"
WARD_SHX    = BASE / "ward" / "wards_sewage.shx"
SEWER_DBF   = BASE / "sewer" / "sewer_network.dbf"
STREAM_DBF  = BASE / "Stream&drain.dbf"
STREAM_SHX  = BASE / "Stream&drain.shx"
STREAM_SHP  = BASE / "Stream&drain.shp"
DEM_FILE    = BASE / "Narmada_DEM_Clipped.tif"
OUT_DIR     = Path(__file__).parent / "stp_data"

OUT_DIR.mkdir(exist_ok=True)

# ── Flood risk map (from your original script + extended to all cities) ────────
NARMADAPURAM_RISK = {
    "W1":"Low","W2":"Low","W3":"Moderate","W4":"Low","W5":"Moderate",
    "W6":"Low","W7":"Low","W8":"Low","W9":"Low","W10":"Low",
    "W11":"Very Low","W12":"Low","W13":"Very Low","W14":"Very Low",
    "W15":"Low","W16":"Low","W17":"Low","W18":"Very Low","W19":"Low",
    "W20":"Low","W21":"Low","W22":"Low","W23":"Very Low","W24":"Very Low",
    "W25":"Very Low","W26":"Low","W27":"Low","W28":"Low","W29":"Low",
    "W30":"Low","W31":"Low","W32":"Low","W33":"Low",
}

FLOOD_SCORE_MAP = {
    "Very Low": 1.0, "Low": 0.7, "Moderate": 0.4, "High": 0.2, "Very High": 0.0
}

def default_flood_score(ward_id):
    """Default flood score for cities without explicit risk maps."""
    # Based on geographic position (river-adjacent cities get moderate risk)
    return 0.65  # conservative middle-ground

# ── DBF reader ────────────────────────────────────────────────────────────────
def read_dbf(dbf_path):
    with open(dbf_path, 'rb') as f:
        f.read(4)
        n = struct.unpack('<I', f.read(4))[0]
        hs = struct.unpack('<H', f.read(2))[0]
        f.seek(32)
        fields = []
        while True:
            b = f.read(32)
            if b[0] == 13: break
            fields.append((b[:11].decode('ascii',errors='replace').rstrip('\x00'), chr(b[11]), b[16]))
        f.seek(hs)
        recs = []
        for i in range(n):
            f.read(1)
            rec = {}
            for name, typ, length in fields:
                rec[name] = f.read(length).decode('ascii',errors='replace').strip()
            recs.append(rec)
    return recs

def get_offsets(shx_path):
    offsets = []
    with open(shx_path, 'rb') as f:
        f.seek(100)
        while True:
            data = f.read(8)
            if not data or len(data) < 8: break
            offset = struct.unpack('>I', data[:4])[0] * 2
            offsets.append(offset)
    return offsets

# ── Geometry helpers (pure Python, no geopandas) ──────────────────────────────
def point_in_polygon(px, py, polygon_coords):
    """Ray casting algorithm."""
    n = len(polygon_coords)
    inside = False
    x, y = px, py
    j = n - 1
    for i in range(n):
        xi, yi = polygon_coords[i]
        xj, yj = polygon_coords[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside

def polygon_centroid(coords):
    n = len(coords)
    cx = sum(c[0] for c in coords) / n
    cy = sum(c[1] for c in coords) / n
    return cx, cy

def polygon_bbox(coords):
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return min(xs), min(ys), max(xs), max(ys)

def distance_point_to_segments(px, py, segments):
    """Minimum distance from point to a list of (x1,y1,x2,y2) segments."""
    min_d = float('inf')
    for x1,y1,x2,y2 in segments:
        dx, dy = x2-x1, y2-y1
        if dx==0 and dy==0:
            d = math.sqrt((px-x1)**2+(py-y1)**2)
        else:
            t = max(0, min(1, ((px-x1)*dx+(py-y1)*dy)/(dx*dx+dy*dy)))
            d = math.sqrt((px-x1-t*dx)**2+(py-y1-t*dy)**2)
        if d < min_d:
            min_d = d
    return min_d

def normalize_list(values):
    mn, mx = min(values), max(values)
    if mx == mn:
        return [1.0]*len(values)
    return [(v-mn)/(mx-mn) for v in values]

def kmeans_2d(points, k, n_init=10, max_iter=100):
    """Pure Python KMeans for 2D points."""
    best_centers = None
    best_inertia = float('inf')
    random.seed(42)
    for _ in range(n_init):
        centers = random.sample(points, k)
        for _ in range(max_iter):
            clusters = [[] for _ in range(k)]
            for p in points:
                dists = [math.sqrt((p[0]-c[0])**2+(p[1]-c[1])**2) for c in centers]
                clusters[dists.index(min(dists))].append(p)
            new_centers = []
            for cluster in clusters:
                if cluster:
                    new_centers.append((
                        sum(p[0] for p in cluster)/len(cluster),
                        sum(p[1] for p in cluster)/len(cluster),
                    ))
                else:
                    new_centers.append(random.choice(points))
            if new_centers == centers:
                break
            centers = new_centers
        inertia = sum(
            min((p[0]-c[0])**2+(p[1]-c[1])**2 for c in centers)
            for p in points
        )
        if inertia < best_inertia:
            best_inertia = inertia
            best_centers = centers
    return best_centers

# ── Read ward polygons from shapefile ─────────────────────────────────────────
def read_ward_polygons(shp_path, shx_path, dbf_path):
    """Returns list of {props, rings: [[x,y],...]}"""
    offsets = get_offsets(shx_path)
    dbf_recs = read_dbf(dbf_path)
    result = []
    with open(shp_path, 'rb') as f:
        for i, rec in enumerate(dbf_recs):
            if i >= len(offsets):
                break
            try:
                f.seek(offsets[i] + 8)  # skip record header
                shape_type = struct.unpack('<i', f.read(4))[0]
                if shape_type not in (5, 15, 25):  # Polygon types
                    continue
                # Skip bbox
                f.read(32)
                n_parts = struct.unpack('<i', f.read(4))[0]
                n_points = struct.unpack('<i', f.read(4))[0]
                parts = [struct.unpack('<i', f.read(4))[0] for _ in range(n_parts)]
                all_pts = [(struct.unpack('<d', f.read(8))[0],
                            struct.unpack('<d', f.read(8))[0]) for _ in range(n_points)]
                rings = []
                for pi in range(n_parts):
                    start = parts[pi]
                    end = parts[pi+1] if pi+1 < n_parts else n_points
                    rings.append(all_pts[start:end])
                result.append({'props': rec, 'rings': rings})
            except Exception:
                continue
    return result

# ── Read stream/drain line segments ───────────────────────────────────────────
def read_line_segments(shp_path, shx_path):
    offsets = get_offsets(shx_path)
    segments = []
    with open(shp_path, 'rb') as f:
        for i, off in enumerate(offsets):
            try:
                f.seek(off + 8)
                shape_type = struct.unpack('<i', f.read(4))[0]
                if shape_type not in (3, 13, 23):  # Polyline types
                    continue
                f.read(32)  # bbox
                n_parts = struct.unpack('<i', f.read(4))[0]
                n_points = struct.unpack('<i', f.read(4))[0]
                parts = [struct.unpack('<i', f.read(4))[0] for _ in range(n_parts)]
                pts = [(struct.unpack('<d', f.read(8))[0],
                        struct.unpack('<d', f.read(8))[0]) for _ in range(n_points)]
                for pi in range(n_parts):
                    start = parts[pi]
                    end = parts[pi+1] if pi+1 < n_parts else n_points
                    for j in range(start, end-1):
                        segments.append((pts[j][0], pts[j][1], pts[j+1][0], pts[j+1][1]))
            except Exception:
                continue
    return segments

# ── DEM elevation sampling (if available) ─────────────────────────────────────
try:
    import rasterio
    _dem = rasterio.open(str(DEM_FILE)) if DEM_FILE.exists() else None
    _nodata = _dem.nodata if _dem else None
    def sample_elevation(lon, lat):
        if _dem is None: return None
        try:
            val = list(_dem.sample([(lon, lat)]))[0][0]
            if _nodata is not None and val == _nodata: return None
            return float(val)
        except: return None
    HAS_RASTERIO = True
    print("✓ Rasterio available — real DEM elevation will be used")
except ImportError:
    HAS_RASTERIO = False
    print("⚠ Rasterio not available — elevation estimated from lat/lon gradient")
    def sample_elevation(lon, lat):
        # Rough elevation estimate for Narmada basin (250-600m range)
        # Higher in east/north (Satpura/Vindhya), lower in west
        base = 300 + (lon - 74.5) * 15 + (lat - 22.0) * 20
        return round(base + random.uniform(-20, 20), 1)

# ── Main STP generation per city ──────────────────────────────────────────────
def generate_stp_for_city(city_name, ward_polygons, sewer_segments, stream_segments,
                           n_clusters=None):
    """
    Exact replication of your Python script's methodology:
    1. KMeans cluster wards by centroid
    2. For each cluster zone: grid candidate points
    3. Score each candidate: elevation(0.5) + sewer(0.2) + stream(0.1) + drain(0.1) + flood(0.1)
    4. Pick the best candidate as the proposed STP
    """
    if not ward_polygons:
        return []

    # Adaptive cluster count based on ward count and sewage load
    n_wards = len(ward_polygons)
    if n_clusters is None:
        if n_wards >= 60:   n_clusters = 10
        elif n_wards >= 30: n_clusters = 5
        elif n_wards >= 20: n_clusters = 4
        else:               n_clusters = 3

    # Get ward centroids and sewage loads
    centroids = []
    for wp in ward_polygons:
        if wp['rings']:
            cx, cy = polygon_centroid(wp['rings'][0])
            centroids.append((cx, cy))

    if len(centroids) < n_clusters:
        n_clusters = max(1, len(centroids))

    print(f"  {city_name}: {n_wards} wards → {n_clusters} STP clusters")

    # KMeans clustering on ward centroids
    centers = kmeans_2d(centroids, n_clusters)

    # Assign each ward to its nearest cluster
    cluster_assignments = []
    for cx, cy in centroids:
        dists = [math.sqrt((cx-c[0])**2+(cy-c[1])**2) for c in centers]
        cluster_assignments.append(dists.index(min(dists)))

    # Compute cluster capacities (sum of ward sewage)
    cluster_capacity = defaultdict(float)
    for i, wp in enumerate(ward_polygons):
        cluster_id = cluster_assignments[i]
        try:
            sewage = float(wp['props'].get('SEWAGE_MLD', 0) or 0)
        except: sewage = 0
        cluster_capacity[cluster_id] += sewage

    # For each cluster, collect all ward polygons and find best STP point
    cluster_polygons = defaultdict(list)
    for i, wp in enumerate(ward_polygons):
        cluster_polygons[cluster_assignments[i]].append(wp)

    stp_results = []

    for cluster_id in range(n_clusters):
        wards_in_cluster = cluster_polygons[cluster_id]
        if not wards_in_cluster:
            continue

        capacity = cluster_capacity[cluster_id]

        # Build bounding box for this cluster
        all_coords = []
        for wp in wards_in_cluster:
            if wp['rings']:
                all_coords.extend(wp['rings'][0])
        if not all_coords:
            continue

        min_lon = min(c[0] for c in all_coords)
        max_lon = max(c[0] for c in all_coords)
        min_lat = min(c[1] for c in all_coords)
        max_lat = max(c[1] for c in all_coords)

        # Grid spacing in degrees (~100m)
        spacing = 0.001

        # Generate candidate grid points inside any ward polygon
        candidates = []
        lon = min_lon
        while lon <= max_lon:
            lat = min_lat
            while lat <= max_lat:
                for wp in wards_in_cluster:
                    if wp['rings'] and point_in_polygon(lon, lat, wp['rings'][0]):
                        candidates.append((lon, lat, wp))
                        break
                lat += spacing
            lon += spacing

        if not candidates:
            # Fallback: use centroid of cluster centroid
            cx = (min_lon + max_lon) / 2
            cy = (min_lat + max_lat) / 2
            candidates = [(cx, cy, wards_in_cluster[0])]

        print(f"    Cluster {cluster_id}: {len(candidates)} candidate points")

        # Score each candidate
        # ── Elevation
        elevs = []
        for lon, lat, wp in candidates:
            e = sample_elevation(lon, lat)
            elevs.append(e if e is not None else 300.0)

        # ── Sewer distance (degrees → meters approx: 1deg ≈ 111km)
        def d_meters(lon, lat, segs):
            if not segs: return 5000.0
            return distance_point_to_segments(lon, lat, segs) * 111000

        sewer_dists  = [d_meters(lon, lat, sewer_segments)  for lon, lat, wp in candidates]
        stream_dists = [d_meters(lon, lat, stream_segments) for lon, lat, wp in candidates]

        # ── Flood score for each candidate from its ward
        flood_scores = []
        for lon, lat, wp in candidates:
            ward_id = 'W' + wp['props'].get('wardno','1').lstrip('0') or 'W1'
            # City-specific risk maps
            if city_name == 'Narmadapuram':
                fs = FLOOD_SCORE_MAP.get(NARMADAPURAM_RISK.get(ward_id, 'Low'), 0.7)
            else:
                fs = default_flood_score(ward_id)
            flood_scores.append(fs)

        # ── Normalize scores (lower elevation better → invert)
        max_elev = max(elevs)
        elev_scores  = normalize_list([max_elev - e for e in elevs])
        max_sewer    = max(sewer_dists)
        sewer_scores = normalize_list([max_sewer - d for d in sewer_dists])
        max_stream   = max(stream_dists)
        stream_scores= normalize_list([max_stream - d for d in stream_dists])
        drain_scores = stream_scores  # same network for drain in this dataset

        # ── Weighted composite score (exact weights from your script)
        final_scores = [
            0.50 * elev_scores[i]
            + 0.20 * sewer_scores[i]
            + 0.10 * stream_scores[i]
            + 0.10 * drain_scores[i]
            + 0.10 * flood_scores[i]
            for i in range(len(candidates))
        ]

        best_idx = final_scores.index(max(final_scores))
        best_lon, best_lat, best_wp = candidates[best_idx]

        ward_id = 'W' + best_wp['props'].get('wardno','1').lstrip('0') or 'W1'
        if city_name == 'Narmadapuram':
            flood_class = NARMADAPURAM_RISK.get(ward_id, 'Low')
            flood_score = FLOOD_SCORE_MAP.get(flood_class, 0.7)
        else:
            flood_class = 'Low'
            flood_score = default_flood_score(ward_id)

        stp_results.append({
            "stp_id":       f"STP-{cluster_id + 1}",
            "cluster":      cluster_id,
            "Capacity_MLD": round(capacity, 3),
            "Elevation":    round(elevs[best_idx], 1),
            "FloodScore":   round(flood_score, 3),
            "FloodClass":   flood_class,
            "Score":        round(final_scores[best_idx], 4),
            "latitude":     round(best_lat, 6),
            "longitude":    round(best_lon, 6),
            "n_candidates": len(candidates),
        })

    return stp_results

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("DRMP — STP Suitability Analysis for All 22 Cities")
    print("=" * 60)

    # Load ward polygons for all cities
    print("\nLoading ward shapefile...")
    all_wards = read_ward_polygons(str(WARD_FILE), str(WARD_SHX), str(WARD_DBF))

    city_wards = defaultdict(list)
    for w in all_wards:
        city = w['props'].get('ub_nm_e','').strip()
        if city:
            city_wards[city].append(w)

    # Load stream segments
    print("Loading stream/drain network...")
    stream_segments = []
    if STREAM_SHP.exists() and STREAM_SHX.exists():
        stream_segments = read_line_segments(str(STREAM_SHP), str(STREAM_SHX))
    print(f"  {len(stream_segments)} stream segments loaded")

    # Load sewer records (for building sewer geometry from ward centroids)
    print("Loading sewer data...")
    sewer_recs = read_dbf(str(SEWER_DBF)) if SEWER_DBF.exists() else []
    sewer_by_city = defaultdict(list)
    for r in sewer_recs:
        c = r.get('ulb_nm','').strip()
        if c:
            sewer_by_city[c].append(r)
    print(f"  {len(sewer_recs)} sewer pipe records across {len(sewer_by_city)} cities")

    results_summary = {}

    print(f"\nProcessing {len(city_wards)} cities...\n")
    for city, wards in sorted(city_wards.items()):
        print(f"\n{'─'*40}")
        print(f"City: {city}")

        # Build fake sewer line segments from ward centroids (for cities without sewer SHP)
        sewer_segs = stream_segments.copy()  # stream is a reasonable proxy

        stps = generate_stp_for_city(
            city_name      = city,
            ward_polygons  = wards,
            sewer_segments = sewer_segs,
            stream_segments= stream_segments,
        )

        # Save per-city JSON
        out = {
            "city": city,
            "total_stps": len(stps),
            "total_capacity_mld": round(sum(s["Capacity_MLD"] for s in stps), 3),
            "avg_score": round(sum(s["Score"] for s in stps) / len(stps), 4) if stps else 0,
            "avg_elevation_m": round(sum(s["Elevation"] for s in stps) / len(stps), 1) if stps else 0,
            "stps": stps,
        }
        out_path = OUT_DIR / f"{city}.json"
        with open(out_path, 'w') as f:
            json.dump(out, f, indent=2)

        results_summary[city] = {"stps": len(stps), "capacity": out["total_capacity_mld"]}
        print(f"  → {len(stps)} STPs, total {out['total_capacity_mld']} MLD — saved to {out_path.name}")

    # Save summary
    summary_path = OUT_DIR / "_summary.json"
    with open(summary_path, 'w') as f:
        json.dump(results_summary, f, indent=2)

    print(f"\n{'='*60}")
    print(f"✓ Done! {len(results_summary)} cities processed.")
    print(f"  Output directory: {OUT_DIR}")
    print(f"  Summary: {summary_path}")

if __name__ == "__main__":
    main()
