"""A study area the analyst brings along (Prof. Shi 2026-11-13 #12).

"Provide a button for the user to upload their own study area polygon in
Shapefile." The file is read here, reprojected to WGS84 and dissolved into one
polygon. Every complaint about the file is raised as ``AreaUploadError`` with a
sentence an analyst can act on.
"""
from __future__ import annotations

import io
import json
import logging
import tempfile
import zipfile
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

MAX_BYTES = 20 * 1024 * 1024
_TIGER_COUNTY = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "tigerWMS_Current/MapServer/82/query"
)


class AreaUploadError(ValueError):
    """The uploaded file cannot be used as a study area."""


def _safe_extract(content: bytes, dest: Path) -> list[Path]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise AreaUploadError("That is not a valid .zip file. Zip the shapefile's .shp, .shx and "
                              ".dbf files (and the .prj if you have it) together and upload the .zip.")
    total = 0
    out: list[Path] = []
    with zf:
        for info in zf.infolist():
            name = info.filename
            if info.is_dir() or name.startswith("__MACOSX") or Path(name).name.startswith("."):
                continue
            total += info.file_size
            if total > MAX_BYTES * 4:
                raise AreaUploadError("The zip expands to far more than 20 MB. Upload only the "
                                      "study-area layer.")
            target = dest / Path(name).name          # flatten: never trust paths inside the zip
            target.write_bytes(zf.read(info))
            out.append(target)
    return out


def read_polygon(filename: str, content: bytes):
    """(GeoDataFrame of ONE dissolved polygon in EPSG:4326, [notes]) from a zipped
    shapefile or a GeoJSON file."""
    import geopandas as gpd
    from shapely import make_valid

    if not content:
        raise AreaUploadError("The file is empty.")
    if len(content) > MAX_BYTES:
        raise AreaUploadError(f"The file is {len(content) / 1e6:.0f} MB; the limit is 20 MB. "
                              "Simplify the polygon or upload a smaller extract.")
    name = (filename or "").lower()
    notes: list[str] = []
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d)
        has_prj = True
        if name.endswith(".zip"):
            files = _safe_extract(content, dest)
            shp = [f for f in files if f.suffix.lower() == ".shp"]
            gj = [f for f in files if f.suffix.lower() in (".geojson", ".json")]
            if not shp and gj:
                path = gj[0]
            elif not shp:
                raise AreaUploadError("There is no .shp file in that zip. A shapefile needs at least "
                                      "the .shp, .shx and .dbf files — zip them together.")
            else:
                path = shp[0]
                have = {f.suffix.lower() for f in files if f.stem == path.stem}
                for ext in (".shx", ".dbf"):
                    if ext not in have:
                        notes.append(f"The zip has no {ext} file; it was read without it.")
                has_prj = ".prj" in have
        elif name.endswith((".geojson", ".json")):
            path = dest / "area.geojson"
            path.write_bytes(content)
        elif name.endswith(".shp"):
            raise AreaUploadError("Upload the shapefile as a .zip containing the .shp, .shx and "
                                  ".dbf files together (a lone .shp file is not enough).")
        else:
            raise AreaUploadError("Upload a zipped shapefile (.zip) or a GeoJSON file (.geojson).")
        try:
            gdf = gpd.read_file(path)
        except Exception as exc:
            logger.info("study-area read failed: %r", exc)
            raise AreaUploadError("That file could not be read as a shapefile or GeoJSON. Check "
                                  "that it is not corrupted and that the zip holds the .shp, .shx "
                                  "and .dbf files.")
    if gdf is None or len(gdf) == 0 or gdf.geometry.isna().all():
        raise AreaUploadError("The file has no features in it — there is no polygon to use.")
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    kinds = set(gdf.geom_type)
    polys = gdf[gdf.geom_type.isin(["Polygon", "MultiPolygon"])]
    if polys.empty:
        what = ", ".join(sorted(k.lower() for k in kinds)) or "no"
        raise AreaUploadError(f"The file contains {what} features, not polygons. A study area "
                              "must be a polygon (an outline you can fill in).")
    if len(polys) < len(gdf):
        notes.append(f"{len(gdf) - len(polys)} point/line feature(s) in the file were ignored.")
    if polys.crs is None:
        minx, miny, maxx, maxy = polys.total_bounds
        if not (-180 <= minx and maxx <= 180 and -90 <= miny and maxy <= 90):
            raise AreaUploadError("The file has no coordinate system (.prj) and its coordinates are "
                                  "not longitude/latitude, so it cannot be placed on the map. Add "
                                  "the .prj file to the zip.")
        polys = polys.set_crs("EPSG:4326")
        notes.append("The file has no coordinate system (.prj); it was assumed to be WGS84 "
                     "(longitude/latitude)." if not has_prj else
                     "The file did not say which coordinate system it uses; WGS84 was assumed.")
    polys = polys.to_crs("EPSG:4326")
    geom = polys.geometry.apply(make_valid)
    merged = geom.union_all() if hasattr(geom, "union_all") else geom.unary_union
    # make_valid can leave stray lines or points beside the polygon; keep the polygons only
    if merged.geom_type == "GeometryCollection":
        from shapely.geometry import MultiPolygon

        polys_only = [p for g in merged.geoms if g.geom_type in ("Polygon", "MultiPolygon")
                      for p in (g.geoms if g.geom_type == "MultiPolygon" else [g])]
        if not polys_only:
            raise AreaUploadError("The polygon in the file is empty or collapsed to a line.")
        merged = polys_only[0] if len(polys_only) == 1 else MultiPolygon(polys_only)
    if merged.is_empty or merged.area == 0:
        raise AreaUploadError("The polygon in the file is empty (zero area).")
    if len(polys) > 1:
        notes.append(f"{len(polys)} polygons were combined into one study area.")
    out = gpd.GeoDataFrame({"name": [Path(filename or "area").stem]}, geometry=[merged],
                           crs="EPSG:4326")
    return out, notes


def counties_for_polygon(gdf) -> list[dict]:
    """Counties the polygon touches, largest share first:
    [{state_fips, county_fips, name, share}], share being the part of the polygon
    inside the county (0-1)."""
    import geopandas as gpd

    minx, miny, maxx, maxy = [float(v) for v in gdf.total_bounds]
    params = {
        "geometry": f"{minx},{miny},{maxx},{maxy}", "geometryType": "esriGeometryEnvelope",
        "inSR": "4326", "spatialRel": "esriSpatialRelIntersects", "outSR": "4326",
        "outFields": "GEOID,STATE,COUNTY,NAME", "returnGeometry": "true", "f": "geojson",
    }
    r = requests.get(_TIGER_COUNTY, params=params, timeout=60)
    r.raise_for_status()
    counties = gpd.read_file(r.text)
    if counties.empty:
        return []
    return county_shares(gdf, counties)


def county_shares(gdf, counties) -> list[dict]:
    poly = gdf.to_crs("EPSG:5070").geometry.iloc[0]
    total = poly.area or 1.0
    out = []
    for _, row in counties.to_crs("EPSG:5070").iterrows():
        share = row.geometry.intersection(poly).area / total
        if share > 0:
            out.append({"state_fips": str(row["STATE"]), "county_fips": str(row["COUNTY"]),
                        "name": str(row["NAME"]), "share": round(float(share), 4)})
    return sorted(out, key=lambda c: -c["share"])


def simplify_for_storage(gdf, max_bytes: int = 400_000) -> dict:
    """The polygon as a GeoJSON geometry dict small enough to keep with the project."""
    geom = gdf.geometry.iloc[0]
    tol = 0.00002
    while True:
        s = geom.simplify(tol, preserve_topology=True)
        text = json.dumps(s.__geo_interface__, separators=(",", ":"))
        if len(text) <= max_bytes or tol > 0.05:
            return json.loads(text)
        tol *= 2.5
