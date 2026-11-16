"""The project's database as one saved package (Prof. Shi 2026-11-13, step 5).

"When the user clicks the Confirm button, ask the user, 'Download and save the
database?' If the answer is yes, download the data from online sources, put them
together with the user-uploaded local data, save the entire package (the database
for the current project) to a separate place."

The package is a zip holding one GeoPackage (every layer the project fetched or
the user supplied, the project area, an uploaded study-area outline when there is
one, and a ``package_metadata`` table: source, provider, vintage, fetch time,
user-provided flag), a metadata CSV and a short README. The latest copy is also
kept on the server beside the project. A fingerprint of what went into it is
remembered, so the question is only asked again when the database has changed.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PROVIDER_OSM = "OpenStreetMap contributors (via the Overpass API)"
PROVIDER_CENSUS = "US Census Bureau TIGERweb"


def _hcas():
    from backend import hcas

    return hcas


def package_dir(pid: str) -> Path:
    d = _hcas()._user_data_root() / pid / "database"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _iso(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError):
        return ""


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower() or "project"


# ---------------------------------------------------------------- what is in the database
def _layer_frames(pid: str):
    """(layer name, manifest info, GeoDataFrame) for every layer on disk for the project."""
    h = _hcas()
    import geopandas as gpd

    for layer, info in (h._read_manifest(pid).get("layers") or {}).items():
        gdf = None
        try:
            if info.get("kind") == "shared":
                hit = h._shared_layer(layer, info.get("bbox") or []) if info.get("bbox") else None
                gdf = hit[0] if hit else None
            else:
                if info.get("kind") == "user":
                    src = h._user_layer_path(pid, layer)
                elif info.get("bbox_key"):
                    src = h._project_cache_dir(pid) / f"{layer}.{info['bbox_key']}.geojson"
                else:
                    src = None
                if src is not None and src.exists():
                    gdf = gpd.read_file(src)
        except Exception as exc:
            logger.warning("database package: layer %s unreadable: %r", layer, exc)
        if gdf is not None:
            yield layer, info, gdf


def user_data_entries(pid: str) -> list[dict]:
    """The analyst's own per-factor data stored with the project (step 3)."""
    try:
        from backend import hcas_userdata

        return hcas_userdata.list_entries(pid)
    except ImportError:
        return []


def fingerprint(pid: str, db: Path | None = None) -> str:
    """A short hash of everything that goes into the package."""
    h = _hcas()
    project = h.get_project(pid, db=db)
    meta = project["meta"]
    layers = {
        k: [v.get("kind"), v.get("at"), v.get("feature_count"), v.get("bbox_key")]
        for k, v in sorted((h._read_manifest(pid).get("layers") or {}).items())
    }
    user = [[e.get("id"), e.get("factor"), e.get("filename"), e.get("sha")] for e in user_data_entries(pid)]
    payload = {
        "layers": layers,
        "area": [meta.get("state_fips"), meta.get("county_fips"), meta.get("areal_unit"),
                 meta.get("area_geoids"), meta.get("area_polygon")],
        "user": user,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def status(pid: str, db: Path | None = None) -> dict:
    """Whether the analyst should be asked 'Download and save the database?'."""
    h = _hcas()
    meta = h.get_project(pid, db=db)["meta"]
    fp = fingerprint(pid, db)
    saved = meta.get("db_saved_fingerprint")
    return {"fingerprint": fp, "saved_fingerprint": saved, "saved_at": meta.get("db_saved_at"),
            "ask": saved != fp, "has_copy": latest_path(pid) is not None}


def latest_path(pid: str) -> Path | None:
    p = package_dir(pid) / "latest.zip"
    return p if p.exists() else None


# ---------------------------------------------------------------- building it
def _metadata_rows(pid: str, project: dict, layers: list, user: list) -> list[dict]:
    h = _hcas()
    meta = project["meta"]
    rows: list[dict] = []
    step3 = next((s for s in project["steps"] if s["step"] == 3), None)
    s3_time = _iso(step3.get("drafted_at")) if step3 else ""
    for layer, info, gdf in layers:
        user_provided = info.get("kind") == "user"
        label, source, _geom = h._layer_meta(layer)
        rows.append({
            "layer": layer, "kind": "map layer", "source": label,
            "provider": f"User-provided: {info.get('original_filename') or layer}" if user_provided else (
                PROVIDER_OSM if "OpenStreetMap" in source else source),
            "vintage": "as supplied by the user" if user_provided else "OpenStreetMap data at the time it was fetched",
            "fetched_at": _iso(info.get("at")), "user_provided": int(user_provided),
            "feature_count": int(len(gdf)), "crs": str(gdf.crs),
            "original_filename": info.get("original_filename") or "",
            "note": "replaces the automatically fetched layer" if user_provided else "",
        })
    rows.append({"layer": "project_area", "kind": "study area", "source": "Project area (union of the selected Census units)",
                 "provider": PROVIDER_CENSUS, "vintage": "2020 Census boundaries", "fetched_at": s3_time,
                 "user_provided": 0, "feature_count": 1, "crs": "EPSG:5070", "original_filename": "", "note": ""})
    if meta.get("area_polygon"):
        rows.append({"layer": "uploaded_study_area", "kind": "study area", "source": "Uploaded study-area outline",
                     "provider": f"User-provided: {meta.get('area_polygon_name') or 'study area'}",
                     "vintage": "as supplied by the user", "fetched_at": "", "user_provided": 1,
                     "feature_count": 1, "crs": "EPSG:4326", "original_filename": meta.get("area_polygon_name") or "",
                     "note": ""})
    step3_art = (step3.get("human_final") or step3.get("ai_draft") or {}) if step3 else {}
    for t in step3_art.get("tables") or []:
        rows.append({"layer": f"(table) {t.get('source')}", "kind": "community data table (not stored in the package)",
                     "source": t.get("name") or "", "provider": t.get("name") or "",
                     "vintage": t.get("note") or "", "fetched_at": s3_time, "user_provided": 0,
                     "feature_count": t.get("rows") or "", "crs": "", "original_filename": "", "note": t.get("level") or ""})
    for e in user:
        rows.append({"layer": e.get("layer") or e.get("filename"), "kind": f"user data for {e.get('factor')}",
                     "source": e.get("filename") or "", "provider": f"User-provided: {e.get('filename')}",
                     "vintage": "as supplied by the user", "fetched_at": _iso(e.get("at")), "user_provided": 1,
                     "feature_count": e.get("rows") or "", "crs": e.get("crs") or "", "original_filename": e.get("filename") or "",
                     "note": e.get("note") or ""})
    return rows


def _add_metadata_table(gpkg: Path, rows: list[dict]) -> None:
    cols = ["layer", "kind", "source", "provider", "vintage", "fetched_at", "user_provided",
            "feature_count", "crs", "original_filename", "note"]
    con = sqlite3.connect(gpkg)
    try:
        con.execute("DROP TABLE IF EXISTS package_metadata")
        con.execute("CREATE TABLE package_metadata (fid INTEGER PRIMARY KEY AUTOINCREMENT, "
                    + ", ".join(f'"{c}" TEXT' for c in cols) + ")")
        con.executemany(
            "INSERT INTO package_metadata (" + ", ".join(f'"{c}"' for c in cols) + ") VALUES ("
            + ", ".join("?" for _ in cols) + ")",
            [[str(r.get(c, "")) for c in cols] for r in rows])
        try:   # make it visible to GIS software as an attribute table
            con.execute("INSERT OR REPLACE INTO gpkg_contents (table_name, data_type, identifier, description, last_change) "
                        "VALUES ('package_metadata', 'attributes', 'package_metadata', "
                        "'Source, provider, vintage, fetch time and user-provided flag of every layer', "
                        "strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
        except sqlite3.Error:
            pass
        con.commit()
    finally:
        con.close()


def _add_attribute_table(gpkg: Path, name: str, df) -> None:
    """A plain table (the analyst's own GEOID table) as an attribute table of the GeoPackage."""
    name = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "user_table"
    cols = [re.sub(r"[^A-Za-z0-9_]+", "_", str(c)) or "col" for c in df.columns]
    con = sqlite3.connect(gpkg)
    try:
        con.execute(f'DROP TABLE IF EXISTS "{name}"')
        con.execute(f'CREATE TABLE "{name}" (fid INTEGER PRIMARY KEY AUTOINCREMENT, '
                    + ", ".join(f'"{c}" TEXT' for c in cols) + ")")
        con.executemany(f'INSERT INTO "{name}" (' + ", ".join(f'"{c}"' for c in cols) + ") VALUES ("
                        + ", ".join("?" for _ in cols) + ")",
                        [[str(v) for v in row] for row in df.itertuples(index=False, name=None)])
        try:
            con.execute("INSERT OR REPLACE INTO gpkg_contents (table_name, data_type, identifier, description, last_change) "
                        "VALUES (?, 'attributes', ?, 'User-provided table', strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                        (name, name))
        except sqlite3.Error:
            pass
        con.commit()
    finally:
        con.close()


def _readme(project: dict, rows: list[dict], fp: str) -> str:
    meta = project["meta"]
    lines = [
        f"HCAS project database: {meta.get('title') or 'Untitled assessment'}",
        f"Saved {_iso(time.time())} - package id {fp}",
        "",
        "WHAT IS IN HERE",
        "  database.gpkg    A GeoPackage (open it in QGIS or ArcGIS). One layer per data layer, plus",
        "                   'project_area' (the study area) and, when you uploaded one,",
        "                   'uploaded_study_area' (your own outline).",
        "                   The table 'package_metadata' lists, for every layer, where it came from:",
        "                   source, provider, vintage, fetch time and whether YOU supplied it.",
        "  metadata.csv     The same metadata table as a spreadsheet.",
        "",
        "LAYERS",
    ]
    for r in rows:
        lines.append(f"  - {r['layer']}: {r['source']} [{r['provider']}]"
                     + (f" - {r['feature_count']} features" if r.get("feature_count") != "" else ""))
    lines += ["", "NOTES",
              "  Map layers keep the coordinate system they were stored in (see the crs column).",
              "  Community data tables (CDC, County Health Rankings, EPA) are listed in the metadata",
              "  but are fetched live by HCAS; they are not copied into the GeoPackage.", ""]
    return "\n".join(lines)


def build_package(pid: str, db: Path | None = None) -> dict:
    """Assemble the package, keep the latest copy on the server and remember its
    fingerprint on the project. Returns a summary for the page."""
    h = _hcas()
    import geopandas as gpd

    project = h.get_project(pid, db=db)
    meta = project["meta"]
    layers = list(_layer_frames(pid))
    user = user_data_entries(pid)
    rows = _metadata_rows(pid, project, layers, user)
    fp = fingerprint(pid, db)
    title = meta.get("title") or "project"
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as d:
        gpkg = Path(d) / "database.gpkg"
        first = True

        def put(gdf, name):
            nonlocal first
            gdf = h._gpkg_safe(gdf)
            gdf.to_file(gpkg, layer=name, driver="GPKG", **({} if first else {"mode": "a"}))
            first = False

        # project area first, so the file always has at least one layer
        try:
            put(h._load_area(project, "project").to_crs("EPSG:5070"), "project_area")
        except Exception:
            spec = h._area_spec(project, "project")
            if h._is_sub_county(spec):
                raise
            geom = h._county_geometry(spec["state_fips"], spec["county_fips"])
            if geom is not None and len(geom):
                put(geom.to_crs("EPSG:5070"), "project_area")
        if meta.get("area_polygon"):
            outline = h._outline_gdf(project, "project")
            if outline is not None:
                put(outline, "uploaded_study_area")
        for layer, _info, gdf in layers:
            put(gdf, layer)
        attr_tables = []
        for e in user:
            gdf = None
            try:
                from backend import hcas_userdata

                gdf = hcas_userdata.as_frame(pid, e)
            except Exception as exc:
                logger.warning("database package: user data %s unreadable: %r", e.get("filename"), exc)
            if gdf is not None and isinstance(gdf, gpd.GeoDataFrame):
                put(gdf, e.get("layer") or _slug(e.get("filename") or "user"))
            elif gdf is not None:
                name = "user_" + _slug(Path(e.get("filename") or "table").stem).replace("-", "_")
                attr_tables.append((name, gdf))
                e["layer"] = name
        if first:
            raise RuntimeError("There is nothing to save yet - run the data step first.")
        for name, df in attr_tables:
            _add_attribute_table(gpkg, name, df)
        _add_metadata_table(gpkg, rows)
        csv_buf = io.StringIO()
        w = csv.DictWriter(csv_buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(gpkg, "database.gpkg")
            zf.writestr("metadata.csv", csv_buf.getvalue())
            zf.writestr("README.txt", _readme(project, rows, fp))
    data = buf.getvalue()
    out = package_dir(pid)
    (out / "latest.zip").write_bytes(data)
    now = time.time()
    summary = {"fingerprint": fp, "saved_at": now, "bytes": len(data), "layers": len(rows),
               "filename": f"{_slug(title)}-database.zip"}
    (out / "latest.json").write_text(json.dumps(summary))
    _remember(pid, fp, now, db)
    return summary


def _remember(pid: str, fp: str, at: float, db: Path | None) -> None:
    h = _hcas()
    with h._conn(db or h.DB_PATH) as conn:
        row = conn.execute("SELECT meta_json FROM projects WHERE id = ?", (pid,)).fetchone()
        meta = h._load_meta(row["meta_json"])
        meta["db_saved_fingerprint"] = fp
        meta["db_saved_at"] = at
        conn.execute("UPDATE projects SET meta_json = ? WHERE id = ?", (json.dumps(meta), pid))


def download_name(pid: str) -> str:
    p = package_dir(pid) / "latest.json"
    try:
        return json.loads(p.read_text()).get("filename") or f"{pid}-database.zip"
    except (OSError, ValueError):
        return f"{pid}-database.zip"
