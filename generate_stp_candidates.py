"""
generate_stp_candidates.py — pre-computes, per city, the STP siting result
for EVERY possible N (number of STPs a user might request), using the exact
placement methodology from the reference code (STP_Code.docx):

    KMeans(n_clusters=N) over ward centroids -> N zones (dissolved wards)
    -> for each zone, build a candidate grid and score it -> keep ONLY the
       single best-scoring candidate in that zone.

This is the SAME selection rule as the original per-cluster pipeline
(run_stp.py / Updated_Code.docx): one winner per geographically-partitioned
zone. It guarantees the N sites are spread across the whole city by
construction, because each zone is a disjoint region of the city and each
zone contributes exactly one site.

REPLACES the previous version of this script, which built ONE flat grid
over the whole city, sorted every candidate by score, and greedily kept the
top N subject only to a 300m minimum-spacing rule. That rule did not
guarantee city-wide spread: when the best sewer/stream/elevation corridor
was concentrated in one part of a city, most or all of the top-N highest
scores could legitimately sit within a few hundred metres of each other
(300m apart is still "one corner of the city"), which is exactly the
clustering behaviour that was reported. The KMeans-zonal method does not
have this failure mode, because placement is bounded per zone rather than
by raw score rank.

Nothing about the scoring formula, weights, or per-point score components
(elevation, flood, sewer, stream, drain, wind) has changed. The invented
MIN_SPACING_M rule from the previous version has been removed entirely —
it is not part of the reference methodology and is no longer needed, since
spacing is now a structural consequence of one-pick-per-zone rather than a
post-hoc filter.

Because the zonal picks are precomputed for every N up to a city's ward
count (matching the reference's own cap: n_clusters is limited to n_wards,
"resolve_n_clusters" -> min(n, n_wards)), the live "/suggest" endpoint keeps
doing exactly what it did before: a cached JSON read + slice, no live GIS
geoprocessing per request. Only this offline generation step changed.

Output: stp_data/candidates/<City>_candidates.json, one per city:
    {
      "city": "<ULB name>",
      "max_n": <int>,              # = number of wards for this city;
                                    #   the largest N the zonal method can site
      "weights": {...},
      "proposals_by_n": {
        "1": [ {...one record...} ],
        "2": [ {...}, {...} ],
        ...
        "<max_n>": [ ... <max_n> records ... ]
      }
    }

Each record has the same fields the previous version produced (rank,
Elevation, FloodScore, FloodClass, SewerScore, StreamScore, DrainScore,
WindScore, Score, latitude, longitude, ward_name, ward_no, area_name,
city), plus Capacity_MLD and cluster now correctly populated per zone
(previously always null, because the flat-grid approach had no zone to
compute a capacity from).

Run this offline, exactly as before, whenever ward/sewer/stream/drain/DEM/
wind inputs change, and commit the resulting stp_data/candidates/*.json
files.
"""
import os, math, json, time, warnings
import geopandas as gpd, pandas as pd, numpy as np, rasterio
from sklearn.cluster import KMeans
warnings.filterwarnings("ignore")

t0 = time.time()
D = "/home/claude/app/DRMP_WebApp/drmp_app/data/DRMP/Input"

WARD_FILE  = f"{D}/ward/wards_sewage.shp"
SEWER_FILE = f"{D}/sewer/sewer_network.shp"
DRAIN_FILE = f"{D}/Stream&drain.shp"
DEM_FILE   = "/home/claude/dem/Narmada_DEM_Clipped_cog.tif"
WIND_FILE  = "/mnt/user-data/uploads/1787564570628_ULB_Wind_Statistics.csv"
OUT_DIR    = "/home/claude/stp_update/out/candidates"
os.makedirs(OUT_DIR, exist_ok=True)

WARD_ULB_FIELD, SEWER_ULB_FIELD, WARD_NO_FIELD = "ub_nm_e", "ulb_nm", "wardno"
WIND_ULB_FIELD, WIND_DIR_FIELD = "ulbname", "Prevailing_Direction"
SEWAGE_FIELD = "SEWAGE_MLD"

# ── IDENTICAL to the reference (STP_Code.docx) -- weights not touched ────────
WEIGHTS = {"elev":0.40,"flood":0.20,"sewer":0.10,"stream":0.10,"drain":0.10,"wind":0.10}
assert abs(sum(WEIGHTS.values())-1.0) < 1e-9

GRID_SPACING_M, MAX_CANDIDATES_PER_ZONE = 100, 20000

RISK_MAPS = {
 "Narmadapuram": {f"W{i}":r for i,r in enumerate(
    ["Low","Low","Moderate","Low","Moderate","Low","Low","Low","Low","Low",
     "Very Low","Low","Very Low","Very Low","Low","Low","Low","Very Low","Low",
     "Low","Low","Low","Very Low","Very Low","Very Low","Low","Low","Low","Low",
     "Low","Low","Low","Low"], start=1)}
}
FLOOD_SCORE_MAP = {"Very Low":1.0,"Low":0.7,"Moderate":0.4,"High":0.2,"Very High":0.0}
VALID_WIND_DIRS = {"N","S","E","W","NE","NW","SE","SW"}

def normalize(s):
    s = s.replace([np.inf,-np.inf], np.nan); s = s.fillna(s.median())
    rng = s.max()-s.min()
    return pd.Series(np.ones(len(s)), index=s.index) if (rng==0 or pd.isna(rng)) else (s-s.min())/rng

def inv_dist(d): return normalize(d.max()-d)

def wind_score(cand, zone_geom, direction):
    c = zone_geom.centroid; dx = cand.geometry.x-c.x; dy = cand.geometry.y-c.y
    if direction=="NE": return (normalize(-dx)+normalize(-dy))/2
    if direction=="SW": return (normalize(dx)+normalize(dy))/2
    if direction=="NW": return (normalize(-dx)+normalize(dy))/2
    if direction=="SE": return (normalize(dx)+normalize(-dy))/2
    if direction=="N":  return normalize(-dy)
    if direction=="S":  return normalize(dy)
    if direction=="E":  return normalize(dx)
    if direction=="W":  return normalize(-dx)
    return pd.Series(np.zeros(len(cand)), index=cand.index)

def make_grid(zone_geom, crs, spacing, max_pts):
    minx,miny,maxx,maxy = zone_geom.bounds; w,h = maxx-minx, maxy-miny
    est = (w/spacing)*(h/spacing)
    if est > max_pts: spacing = math.sqrt((w*h)/max_pts)
    xs = np.arange(minx, maxx+spacing, spacing); ys = np.arange(miny, maxy+spacing, spacing)
    xx,yy = np.meshgrid(xs,ys)
    pts = gpd.GeoSeries(gpd.points_from_xy(xx.ravel(), yy.ravel()), crs=crs)
    pts = pts[pts.within(zone_geom)]
    return gpd.GeoDataFrame(geometry=pts.reset_index(drop=True), crs=crs)

print("Loading shared inputs...")
wards = gpd.read_file(WARD_FILE, engine="pyogrio")
sewer = gpd.read_file(SEWER_FILE, engine="pyogrio")
drain = gpd.read_file(DRAIN_FILE, engine="pyogrio")

stream_parts = []
try:
    riv = gpd.read_file(f"{D}/Final_Narmada/Merged_Layers_02_07.shp", engine="pyogrio")
    stream_parts.append(riv[["geometry"]])
except Exception: pass
for path in ["/home/claude/build/khandwa/streams.geojson",
             "/home/claude/build/jabalpur/streams.geojson",
             "/home/claude/stp_update/streams/Amarkantak_Streams/A_S.shp"]:
    try:
        g = gpd.read_file(path, engine="pyogrio"); stream_parts.append(g[["geometry"]])
    except Exception: pass
stream = pd.concat([p.to_crs(4326) for p in stream_parts], ignore_index=True)
stream = gpd.GeoDataFrame(stream, geometry="geometry", crs=4326)

dem = rasterio.open(DEM_FILE)
wind_df = pd.read_csv(WIND_FILE)
if SEWAGE_FIELD not in wards.columns: wards[SEWAGE_FIELD] = 0.0

ulb_list = sorted(wards[WARD_ULB_FIELD].dropna().unique().tolist())
print(f"Found {len(ulb_list)} ULBs\n")

summary = []

for ulb in ulb_list:
    try:
        w = wards[wards[WARD_ULB_FIELD]==ulb].copy()
        w = w[w.geometry.notna() & ~w.geometry.is_empty]
        if len(w)==0: raise ValueError("no wards")
        try: utm_crs = w.to_crs(4326).estimate_utm_crs()
        except Exception: utm_crs = "EPSG:32644"
        w = w.to_crs(utm_crs)
        w[SEWAGE_FIELD] = pd.to_numeric(w[SEWAGE_FIELD], errors="coerce").fillna(0.0)

        rmap = RISK_MAPS.get(ulb)
        w["Ward_ID"] = "W" + w[WARD_NO_FIELD].astype(str).str.strip()
        if rmap:
            w["FloodScore"] = w["Ward_ID"].map(rmap).map(FLOOD_SCORE_MAP)
            w["FloodClass"] = w["Ward_ID"].map(rmap)
            w["FloodScore"]=w["FloodScore"].fillna(0.0); w["FloodClass"]=w["FloodClass"].fillna("Unclassified")
        else:
            w["FloodScore"]=0.0; w["FloodClass"]="Unclassified"

        boundary_geom = w.dissolve().geometry.iloc[0]

        sewer_union = None
        s = sewer[sewer[SEWER_ULB_FIELD]==ulb] if SEWER_ULB_FIELD in sewer.columns else sewer.iloc[0:0]
        if len(s)>0: sewer_union = s.to_crs(utm_crs).union_all()

        st = stream.to_crs(utm_crs); st = st[st.intersects(boundary_geom.buffer(2000))]
        stream_union = st.union_all() if len(st)>0 else None

        dr = drain.to_crs(utm_crs); dr = dr[dr.intersects(boundary_geom.buffer(2000))]
        drain_union = dr.union_all() if len(dr)>0 else None

        prev_wind = None
        wr = wind_df[wind_df[WIND_ULB_FIELD].astype(str).str.strip()==ulb]
        if len(wr)>0:
            d = str(wr.iloc[0][WIND_DIR_FIELD]).strip().upper()
            if d in VALID_WIND_DIRS: prev_wind = d

        w["centroid"] = w.geometry.centroid
        w["x"] = w.centroid.x
        w["y"] = w.centroid.y

        n_wards = len(w)
        # Same cap the reference applies: n_clusters can never exceed the
        # number of wards available to cluster (resolve_n_clusters ->
        # int(max(1, min(n, n_wards)))).
        max_n = max(1, n_wards)

        proposals_by_n = {}

        for n_clusters in range(1, max_n + 1):
            km = KMeans(n_clusters=n_clusters, random_state=42, n_init=20)
            w["cluster"] = km.fit_predict(w[["x", "y"]])

            cap = w.groupby("cluster")[SEWAGE_FIELD].sum()
            zones = w.drop(columns=["centroid"]).dissolve(by="cluster").reset_index()
            zones["Capacity_MLD"] = zones["cluster"].map(cap)

            zone_best = []
            for _, zrow in zones.iterrows():
                cid, zgeom = zrow["cluster"], zrow.geometry

                cand = make_grid(zgeom, utm_crs, GRID_SPACING_M, MAX_CANDIDATES_PER_ZONE)
                if len(cand) == 0:
                    continue

                cd = cand.to_crs(dem.crs)
                vals = np.array([v[0] for v in dem.sample([(g.x, g.y) for g in cd.geometry])], dtype=float)
                cand["elev"] = vals
                if dem.nodata is not None:
                    cand.loc[cand["elev"] == dem.nodata, "elev"] = np.nan
                cand = cand[cand["elev"].notna()].copy()
                if len(cand) == 0:
                    continue
                cand["elev_score"] = inv_dist(cand["elev"])

                fj = gpd.sjoin(cand, w[["FloodScore", "FloodClass", "geometry"]], how="left", predicate="within")
                fj = fj[~fj.index.duplicated(keep="first")]
                cand["flood_score"] = fj["FloodScore"].reindex(cand.index).fillna(0.0).values
                cand["flood_class"] = fj["FloodClass"].reindex(cand.index).fillna("Unclassified").values

                cand["sewer_score"]  = inv_dist(cand.distance(sewer_union))  if sewer_union  is not None else 0.0
                cand["stream_score"] = inv_dist(cand.distance(stream_union)) if stream_union is not None else 0.0
                cand["drain_score"]  = inv_dist(cand.distance(drain_union))  if drain_union  is not None else 0.0
                cand["wind_score"]   = wind_score(cand, zgeom, prev_wind) if prev_wind else 0.0

                W = WEIGHTS
                cand["score"] = (W["elev"]*cand["elev_score"] + W["flood"]*cand["flood_score"]
                                + W["sewer"]*cand["sewer_score"] + W["stream"]*cand["stream_score"]
                                + W["drain"]*cand["drain_score"] + W["wind"]*cand["wind_score"])
                cand = cand.dropna(subset=["score"])
                if len(cand) == 0:
                    continue

                best = cand.loc[cand["score"].idxmax()]
                zone_best.append({
                    "cluster": int(cid),
                    "Capacity_MLD": float(zrow["Capacity_MLD"]),
                    "elev": float(best["elev"]),
                    "flood_score": float(best["flood_score"]),
                    "flood_class": str(best["flood_class"]),
                    "sewer_score": float(best["sewer_score"]),
                    "stream_score": float(best["stream_score"]),
                    "drain_score": float(best["drain_score"]),
                    "wind_score": float(best["wind_score"]),
                    "score": float(best["score"]),
                    "geometry": best.geometry,
                })

            if len(zone_best) == 0:
                continue

            # Best-to-worst by Score, purely for the "STP 1 = best" display
            # label the feature already used -- this does not change WHICH
            # points were picked (one per zone), only their rank number.
            zone_best.sort(key=lambda r: -r["score"])

            pts_wgs = gpd.GeoSeries([r["geometry"] for r in zone_best], crs=utm_crs).to_crs(4326)
            records = []
            for rank, r in enumerate(zone_best, start=1):
                pt = pts_wgs.iloc[rank - 1]
                wj = w.copy(); wj["_d"] = wj.geometry.distance(r["geometry"])
                nearest = wj.loc[wj["_d"].idxmin()]
                ward_name = str(nearest.get("ward_name") or nearest.get("WARD_NAME") or nearest.get("wardname") or "").strip()
                records.append({
                    "rank": rank,
                    "cluster": r["cluster"],
                    "Capacity_MLD": round(r["Capacity_MLD"], 2),
                    "Elevation": round(r["elev"], 1),
                    "FloodScore": round(r["flood_score"], 3),
                    "FloodClass": r["flood_class"],
                    "SewerScore": round(r["sewer_score"], 3),
                    "StreamScore": round(r["stream_score"], 3),
                    "DrainScore": round(r["drain_score"], 3),
                    "WindScore": round(r["wind_score"], 3),
                    "Score": round(r["score"], 4),
                    "latitude": round(float(pt.y), 6),
                    "longitude": round(float(pt.x), 6),
                    "ward_name": ward_name,
                    "ward_no": str(nearest.get(WARD_NO_FIELD) or "").strip(),
                    "area_name": f"{ward_name}, {ulb}" if ward_name else ulb,
                    "city": ulb,
                })
            proposals_by_n[str(n_clusters)] = records

        if not proposals_by_n:
            raise ValueError("no STPs generated for any N")

        out_max_n = max(int(k) for k in proposals_by_n)
        out = {"city": ulb, "max_n": out_max_n, "weights": WEIGHTS, "proposals_by_n": proposals_by_n}
        json.dump(out, open(f"{OUT_DIR}/{ulb}_candidates.json", "w"), indent=2)
        total_picks = sum(len(v) for v in proposals_by_n.values())
        print(f"{ulb:16} wards={n_wards:3}  max_n={out_max_n:3}  -> {total_picks} zone-best sites written across N=1..{out_max_n}")
        summary.append((ulb, out_max_n))

    except Exception as e:
        print(f"{ulb:16} SKIPPED ({e})")
        summary.append((ulb, 0))

dem.close()
print(f"\nDone in {time.time()-t0:.0f}s")
print("\nMax N per city (this is what /suggest validates 'count' against):")
for ulb, n in sorted(summary, key=lambda x: -x[1]):
    print(f"  {ulb:16} {n}")
