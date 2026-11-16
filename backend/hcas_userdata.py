"""The analyst's own data, factor by factor (Prof. Shi 2026-11-13, step 3).

"Rather than directly starting the automatic process, list all confirmed factors and for
each factor allow the user to choose between uploading their own data or let AI search
and fetch data (provide data source information)."

Per confirmed factor the analyst picks "Fetch automatically" (default) or "Upload my own
data". Own data is one of

* a CSV with a Census GEOID column (tract or block group) and a value column: it gives
  the factor's metric directly, for the units of the project area (``apply_tables``);
* a zipped shapefile or GeoJSON of features: for a metric that is computed from exactly
  one map layer it replaces that layer (the same override the data panel always had); for
  any other metric it is stored, shown on the page, saved in the step-5 database and listed
  in the report as not used in the metric.

Everything is kept with the project, and the report cites it as
"User-provided: <file name>".
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

SOURCE_LINKS = {
    "svi": "https://www.atsdr.cdc.gov/place-health/php/svi/svi-data-documentation-download.html",
    "places": "https://www.cdc.gov/places/",
    "chr": "https://www.countyhealthrankings.org/health-data",
    "aqi": "https://www.epa.gov/outdoor-air-quality-data",
}
OSM_LINK = "https://www.openstreetmap.org/copyright"


class UserDataError(ValueError):
    """The uploaded file cannot be used; the message says why."""


def _hcas():
    from backend import hcas

    return hcas


def _dir(pid: str) -> Path:
    d = _hcas().LAYER_CACHE_DIR / pid / "userdata"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index(pid: str) -> dict:
    p = _dir(pid) / "index.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            pass
    return {"entries": [], "choices": {}}


def _save_index(pid: str, idx: dict) -> None:
    (_dir(pid) / "index.json").write_text(json.dumps(idx, indent=1))


def list_entries(pid: str) -> list[dict]:
    return list(_index(pid).get("entries") or [])


def get_choices(pid: str) -> dict:
    return dict(_index(pid).get("choices") or {})


def set_choice(pid: str, metric: str, choice: str) -> dict:
    if choice not in ("fetch", "upload"):
        raise UserDataError("The choice must be 'fetch' or 'upload'.")
    idx = _index(pid)
    idx.setdefault("choices", {})[metric] = choice
    _save_index(pid, idx)
    return idx["choices"]


# ---------------------------------------------------------------- what HCAS would use
def source_info(metric: str) -> dict:
    """Where HCAS gets this metric when it fetches automatically: name, provider,
    vintage and a link."""
    h = _hcas()
    from backend.assess import indicators as ind

    if metric == h.NEEDS_DATA:
        return {"name": "No connected data source measures this yet", "provider": "", "vintage": "",
                "link": "", "layers": []}
    layers = h._metric_layers(metric)
    if layers:
        names = [h._layer_meta(l)[0] for l in layers]
        return {"name": ", ".join(names), "provider": "OpenStreetMap contributors (Overpass API)",
                "vintage": "current OpenStreetMap data, fetched when you press Gather data",
                "link": OSM_LINK, "layers": layers}
    src = h.METRIC_SOURCES.get(metric)
    meta = ind.SOURCES.get(src) or {}
    name = meta.get("name") or src or "Public data table"
    m = re.search(r"\b(19|20)\d{2}(?:\s*[-–]\s*(?:19|20)?\d{2,4})?", name)
    return {"name": meta.get("short") or name, "provider": name,
            "vintage": m.group(0) if m else "latest release", "link": SOURCE_LINKS.get(src, ""),
            "level": meta.get("level") or "", "layers": []}


def plan(pid: str, db: Path | None = None) -> dict:
    """Every confirmed factor with the source HCAS would use and the analyst's choice."""
    h = _hcas()
    project = h.get_project(pid, db=db)
    row = next((s for s in project["steps"] if s["step"] == 2), None)
    if row is None or row["status"] != "confirmed":
        raise KeyError("step 2 is not confirmed")
    factors = (row.get("human_final") or row.get("ai_draft") or {}).get("factors") or []
    entries = list_entries(pid)
    choices = get_choices(pid)
    out = []
    for i, f in enumerate(factors):
        metric = f.get("metric") or ""
        key = metric if metric != h.NEEDS_DATA else f"{h.NEEDS_DATA}:{f.get('factor', i)}"
        mine = [e for e in entries if e.get("metric") == key]
        choice = choices.get(key) or ("upload" if mine else "fetch")
        out.append({"key": key, "factor": f.get("factor", ""), "metric": metric,
                    "metric_label": h.METRIC_LABELS.get(metric, metric.replace("_", " ").capitalize()),
                    "awaiting_data": metric == h.NEEDS_DATA, "source": source_info(metric),
                    "choice": choice, "uploads": mine,
                    "can_use_features": len(h._metric_layers(metric)) == 1 if metric != h.NEEDS_DATA else False})
    return {"factors": out}


# ---------------------------------------------------------------- reading uploads
def _read_features(filename: str, content: bytes):
    import geopandas as gpd

    from backend.gis import upload

    name = (filename or "").lower()
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d)
        if name.endswith(".zip"):
            files = upload._safe_extract(content, dest)
            pick = [f for f in files if f.suffix.lower() == ".shp"] or \
                   [f for f in files if f.suffix.lower() in (".geojson", ".json")]
            if not pick:
                raise UserDataError("There is no .shp or .geojson file in that zip.")
            path = pick[0]
        elif name.endswith((".geojson", ".json")):
            path = dest / "features.geojson"
            path.write_bytes(content)
        else:
            raise UserDataError("Upload a CSV with a GEOID column, a zipped shapefile or a GeoJSON file.")
        try:
            gdf = gpd.read_file(path)
        except Exception:
            raise UserDataError("That file could not be read as a shapefile or GeoJSON.")
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty] if len(gdf) else gdf
    if len(gdf) == 0:
        raise UserDataError("The file has no features in it.")
    if gdf.crs is None:
        minx, miny, maxx, maxy = gdf.total_bounds
        if not (-180 <= minx and maxx <= 180 and -90 <= miny and maxy <= 90):
            raise UserDataError("The file has no coordinate system (.prj) and is not in longitude/latitude.")
        gdf = gdf.set_crs("EPSG:4326")
    return gdf.to_crs("EPSG:4326")


def _read_table(content: bytes):
    import pandas as pd

    try:
        df = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False, encoding_errors="replace")
    except Exception:
        raise UserDataError("That CSV could not be read. Save it as a plain comma-separated file with a header row.")
    if df.empty or len(df.columns) < 2:
        raise UserDataError("The CSV needs a GEOID column and at least one value column, and some rows.")
    df.columns = [str(c).strip() for c in df.columns]
    geo = None
    for c in df.columns:                      # a column called GEOID / FIPS first
        if re.fullmatch(r"(?i)(geo_?id\d*|fips|tract|tract_?id|bg_?id|block_?group|census_?(tract|block_?group))", c):
            geo = c
            break
    if geo is None:
        for c in df.columns:
            s = df[c].str.strip()
            if len(s) and (s.str.fullmatch(r"\d{10,12}").mean() >= 0.9):
                geo = c
                break
    if geo is None:
        raise UserDataError("No Census GEOID column was found. Add a column named GEOID holding the 11-digit "
                            "tract or 12-digit block group code of each row.")
    ids = df[geo].str.strip()
    ids = ids.map(lambda v: v.zfill(11) if re.fullmatch(r"\d{10}", v) else v)
    if not ids.str.fullmatch(r"\d{11}|\d{12}").mean() >= 0.9:
        raise UserDataError(f"The GEOID column ({geo}) must hold 11-digit tract or 12-digit block group codes.")
    df[geo] = ids
    numeric = [c for c in df.columns if c != geo and pd.to_numeric(df[c], errors="coerce").notna().mean() >= 0.8]
    if not numeric:
        raise UserDataError("No numeric value column was found next to the GEOID column.")
    return df, geo, numeric


def add_upload(pid: str, metric: str, filename: str, content: bytes, factor: str = "",
               db: Path | None = None) -> dict:
    """Store an upload for one factor. Returns the entry (``needs_value_column`` is True for a
    CSV with several numeric columns until the analyst picks one)."""
    import geopandas as gpd

    h = _hcas()
    from backend.gis.upload import MAX_BYTES

    h.get_project(pid, db=db)
    if not content:
        raise UserDataError("The file is empty.")
    if len(content) > MAX_BYTES:
        raise UserDataError(f"The file is {len(content) / 1e6:.0f} MB; the limit is 20 MB.")
    eid = uuid.uuid4().hex[:10]
    entry = {"id": eid, "metric": metric, "factor": factor, "filename": filename, "at": time.time(),
             "sha": hashlib.sha256(content).hexdigest()[:16], "used": False, "note": ""}
    if filename.lower().endswith(".csv"):
        df, geo, numeric = _read_table(content)
        (_dir(pid) / f"{eid}.csv").write_bytes(content)
        entry.update(kind="table", file=f"{eid}.csv", rows=int(len(df)), geoid_col=geo, columns=list(df.columns),
                     numeric_columns=numeric, value_col=numeric[0] if len(numeric) == 1 else None,
                     needs_value_column=len(numeric) > 1, geoid_len=int(df[geo].str.len().mode().iloc[0]))
        entry["used"] = metric != h.NEEDS_DATA and entry["value_col"] is not None
        entry["note"] = ("Gives this metric its value for each unit of the project area." if entry["used"] else
                         "Choose which column holds the value." if entry["needs_value_column"] else
                         "Stored with the project; no metric reads it.")
    else:
        gdf = _read_features(filename, content)
        fname = f"{eid}.geojson"
        gdf.to_file(_dir(pid) / fname, driver="GeoJSON")
        layers = h._metric_layers(metric) if metric != h.NEEDS_DATA else []
        entry.update(kind="layer", file=fname, rows=int(len(gdf)), crs="EPSG:4326",
                     geom_types=sorted(set(gdf.geom_type)))
        if len(layers) == 1:
            layer = layers[0]
            gdf.to_file(h._user_layer_path(pid, layer), driver="GeoJSON")
            manifest = h._read_manifest(pid)
            manifest.setdefault("layers", {})[layer] = {
                "kind": "user", "feature_count": int(len(gdf)), "crs": "EPSG:4326", "at": time.time(),
                "original_filename": filename}
            h._write_manifest(pid, manifest)
            entry.update(used=True, layer=layer,
                         note=f"Replaces the fetched {h._layer_meta(layer)[0]} layer in this metric.")
        else:
            entry["layer"] = re.sub(r"[^a-z0-9]+", "_", Path(filename).stem.lower()).strip("_") or "user_layer"
            entry["note"] = ("This metric is computed from several layers, so the file is not used in it; it is "
                             "kept, shown, saved in the database and listed in the report."
                             if layers else "Kept with the project, shown, saved in the database and listed in the report.")
    idx = _index(pid)
    idx["entries"] = [e for e in idx["entries"] if not (e.get("metric") == metric and e.get("kind") == entry["kind"])]
    idx["entries"].append(entry)
    idx.setdefault("choices", {})[metric] = "upload"
    _save_index(pid, idx)
    return entry


def set_value_column(pid: str, entry_id: str, column: str) -> dict:
    idx = _index(pid)
    e = next((x for x in idx["entries"] if x["id"] == entry_id), None)
    if e is None or e.get("kind") != "table":
        raise KeyError(entry_id)
    if column not in (e.get("numeric_columns") or []):
        raise UserDataError(f"'{column}' is not a numeric column of this file.")
    e.update(value_col=column, needs_value_column=False,
             used=e.get("metric") != _hcas().NEEDS_DATA,
             note="Gives this metric its value for each unit of the project area.")
    _save_index(pid, idx)
    return e


def remove(pid: str, entry_id: str) -> None:
    h = _hcas()
    idx = _index(pid)
    e = next((x for x in idx["entries"] if x["id"] == entry_id), None)
    if e is None:
        raise KeyError(entry_id)
    (_dir(pid) / (e.get("file") or "x")).unlink(missing_ok=True)
    if e.get("kind") == "layer" and e.get("used") and e.get("layer"):
        h._user_layer_path(pid, e["layer"]).unlink(missing_ok=True)
        manifest = h._read_manifest(pid)
        manifest.get("layers", {}).pop(e["layer"], None)
        h._write_manifest(pid, manifest)
    idx["entries"] = [x for x in idx["entries"] if x["id"] != entry_id]
    if not any(x.get("metric") == e.get("metric") for x in idx["entries"]):
        idx.setdefault("choices", {})[e.get("metric")] = "fetch"
    _save_index(pid, idx)


def as_frame(pid: str, entry: dict):
    """The stored file as a (Geo)DataFrame, for the database package and the preview."""
    import geopandas as gpd
    import pandas as pd

    path = _dir(pid) / (entry.get("file") or "")
    if entry.get("kind") == "layer":
        return gpd.read_file(path)
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def preview_geojson(pid: str, entry_id: str) -> dict:
    e = next((x for x in list_entries(pid) if x["id"] == entry_id), None)
    if e is None or e.get("kind") != "layer":
        raise KeyError(entry_id)
    gdf = as_frame(pid, e)
    if len(gdf) > 3000:
        gdf = gdf.iloc[:3000]
    gdf = gdf[["geometry"]].copy()
    return json.loads(gdf.to_json())


# ---------------------------------------------------------------- using it
def apply_tables(project: dict, which: str, metrics_wanted: list) -> tuple[dict, dict]:
    """Metric values from the analyst's own GEOID tables, for the units of the area.

    Returns ({result key: value}, {metric: measure detail}). The value is the mean over
    the project-area units found in the file (every unit of the county for a whole-county
    project); the detail states how many matched, so a partial table is never passed off as a
    full one."""
    import pandas as pd

    h = _hcas()
    pid = project["id"]
    choices = get_choices(pid)
    spec = h._area_spec(project, which) or {}
    prefix = f"{spec.get('state_fips', '')}{spec.get('county_fips', '')}"
    selected = [str(g) for g in (spec.get("area_geoids") or [])]
    values, details = {}, {}
    for e in list_entries(pid):
        m = e.get("metric")
        if e.get("kind") != "table" or not e.get("value_col") or m not in metrics_wanted:
            continue
        if choices.get(m, "upload") != "upload":
            continue
        try:
            df = as_frame(pid, e)
        except Exception as exc:
            logger.warning("user table %s unreadable: %r", e.get("filename"), exc)
            continue
        ids = df[e["geoid_col"]].str.strip()
        vals = pd.to_numeric(df[e["value_col"]], errors="coerce")
        table = {i: v for i, v in zip(ids, vals) if v == v}
        n = int(e.get("geoid_len") or 11)
        if selected:
            keys = [g[:n] for g in selected]
            got = [table[k] for k in keys if k in table]
            total = len(keys)
        else:
            got = [v for i, v in table.items() if i.startswith(prefix)]
            total = len(got)
        key = (h.METRIC_RESULT_KEYS.get(m) or [m])[0]
        if not got:
            values[key] = None
            details[m] = {"source": f"User-provided: {e['filename']}", "level": "your table",
                          "not_measured": f"None of the {total or 'project-area'} units were found in {e['filename']}."}
            continue
        values[key] = round(sum(got) / len(got), 4)
        details[m] = {"source": f"User-provided: {e['filename']}",
                      "level": f"your table · {len(got)} of {total} units found" if selected else
                               f"your table · {len(got)} rows in the county"}
    return values, details


def report_lines(project: dict) -> list[str]:
    """The report section citing the analyst's own data."""
    entries = list_entries(project["id"])
    if not entries:
        return []
    out = ["\n## User-provided data",
           "_Data the analyst supplied instead of letting HCAS fetch it. Each is cited as "
           "\"User-provided: file name\"._\n"]
    for e in entries:
        what = f"{e.get('rows', '?')} " + ("rows" if e.get("kind") == "table" else "features")
        use = ("used in the metric" if e.get("used") else "**not used in the metric**")
        out.append(f"- User-provided: {e.get('filename')} — {e.get('factor') or e.get('metric')} ({what}; {use}). "
                   f"{e.get('note') or ''}".rstrip())
    return out
