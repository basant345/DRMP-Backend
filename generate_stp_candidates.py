"""
generate_stp_candidates.py — pre-computes a ranked, well-spaced pool of
candidate STP sites per city, using the EXACT SAME scoring formula, weights
and helper functions as the existing per-cluster STP siting pipeline
(run_stp.py / Updated_Code.docx). Nothing about the scoring changes here.

This is what makes the new "Suggest N STPs" feature possible without doing
live KMeans + grid + DEM geoprocessing on every button click on a 512MB
Render free instance -- the ranked pool is computed once, offline, and the
live endpoint just validates + slices top N from it.

Output: stp_data/<City>_candidates.json  (one per city)
"""
import os, math, json, time, warnings
import geopandas as gpd, pandas as pd, numpy as np, rasterio
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

# ── IDENTICAL to run_stp.py -- same weights, not touched ─────────────────────
WEIGHTS = {"elev":0.40,"flood":0.20,"sewer":0.10,"stream":0.10,"drain":0.10,"wind":0.10}
assert abs(sum(WEIGHTS.values())-1.0) < 1e-9

GRID_SPACING_M, MAX_CANDIDATES_PER_ZONE = 100, 20000
# NEW for this feature only: minimum spacing between two suggested sites, so
# "top N" are N genuinely different locations rather than N points 50m apart
# from each other. This is a selection rule, not a change to the scoring
# formula or weights -- disclosed explicitly to the user.
MIN_SPACING_M = 300

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
if "SEWAGE_MLD" not in wards.columns: wards["SEWAGE_MLD"] = 0.0

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

        # ONE grid over the WHOLE city boundary (not per-cluster) -- this is
        # the one structural difference from run_stp.py, needed because we
        # want a ranked pool across the whole city, not one-best-per-zone.
        cand = make_grid(boundary_geom, utm_crs, GRID_SPACING_M, MAX_CANDIDATES_PER_ZONE)
        if len(cand)==0: raise ValueError("empty grid")

        cd = cand.to_crs(dem.crs)
        vals = np.array([v[0] for v in dem.sample([(g.x,g.y) for g in cd.geometry])], dtype=float)
        cand["elev"] = vals
        if dem.nodata is not None: cand.loc[cand["elev"]==dem.nodata,"elev"] = np.nan
        cand = cand[cand["elev"].notna()].copy()
        if len(cand)==0: raise ValueError("no valid elevation")
        cand["elev_score"] = inv_dist(cand["elev"])

        fj = gpd.sjoin(cand, w[["FloodScore","FloodClass","geometry"]], how="left", predicate="within")
        fj = fj[~fj.index.duplicated(keep="first")]
        cand["flood_score"] = fj["FloodScore"].reindex(cand.index).fillna(0.0).values
        cand["flood_class"] = fj["FloodClass"].reindex(cand.index).fillna("Unclassified").values

        cand["sewer_score"]  = inv_dist(cand.distance(sewer_union))  if sewer_union  is not None else 0.0
        cand["stream_score"] = inv_dist(cand.distance(stream_union)) if stream_union is not None else 0.0
        cand["drain_score"]  = inv_dist(cand.distance(drain_union))  if drain_union  is not None else 0.0
        cand["wind_score"]   = wind_score(cand, boundary_geom, prev_wind) if prev_wind else 0.0

        W = WEIGHTS
        cand["score"] = (W["elev"]*cand["elev_score"] + W["flood"]*cand["flood_score"]
                        + W["sewer"]*cand["sewer_score"] + W["stream"]*cand["stream_score"]
                        + W["drain"]*cand["drain_score"] + W["wind"]*cand["wind_score"])
        cand = cand.dropna(subset=["score"]).sort_values("score", ascending=False).reset_index(drop=True)

        # Greedy non-maximum suppression: walk best-to-worst, keep a point
        # only if it's >= MIN_SPACING_M from every already-kept point.
        kept_idx, kept_geoms = [], []
        for idx, row in cand.iterrows():
            g = row.geometry
            if all(g.distance(kg) >= MIN_SPACING_M for kg in kept_geoms):
                kept_idx.append(idx); kept_geoms.append(g)
        ranked = cand.loc[kept_idx].reset_index(drop=True)

        w_wgs = w.to_crs(4326)
        pts_wgs = gpd.GeoSeries(ranked.geometry, crs=utm_crs).to_crs(4326)

        records = []
        for rank, (i, row) in enumerate(ranked.iterrows(), start=1):
            pt = pts_wgs.iloc[i]
            wj = w.copy(); wj["_d"] = wj.geometry.distance(row.geometry)
            nearest = wj.loc[wj["_d"].idxmin()]
            ward_name = str(nearest.get("ward_name") or nearest.get("WARD_NAME") or nearest.get("wardname") or "").strip()
            records.append({
                "rank": rank,
                "Elevation": round(float(row["elev"]),1),
                "FloodScore": round(float(row["flood_score"]),3),
                "FloodClass": str(row["flood_class"]),
                "SewerScore": round(float(row["sewer_score"]),3),
                "StreamScore": round(float(row["stream_score"]),3),
                "DrainScore": round(float(row["drain_score"]),3),
                "WindScore": round(float(row["wind_score"]),3),
                "Score": round(float(row["score"]),4),
                "latitude": round(float(pt.y),6),
                "longitude": round(float(pt.x),6),
                "ward_name": ward_name,
                "ward_no": str(nearest.get(WARD_NO_FIELD) or "").strip(),
                "area_name": f"{ward_name}, {ulb}" if ward_name else ulb,
                "city": ulb,
            })

        out = {"city": ulb, "available_candidates": len(records),
               "min_spacing_m": MIN_SPACING_M, "weights": WEIGHTS, "candidates": records}
        json.dump(out, open(f"{OUT_DIR}/{ulb}_candidates.json","w"), indent=2)
        print(f"{ulb:16} {len(cand):5} scored -> {len(records):4} spaced candidates (>= {MIN_SPACING_M}m apart)")
        summary.append((ulb, len(records)))

    except Exception as e:
        print(f"{ulb:16} SKIPPED ({e})")
        summary.append((ulb, 0))

dem.close()
print(f"\nDone in {time.time()-t0:.0f}s")
print("\nAvailable-candidate ceiling per city (this is what request #10's validation checks against):")
for ulb, n in sorted(summary, key=lambda x: -x[1]):
    print(f"  {ulb:16} {n}")
