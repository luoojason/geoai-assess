"""Community data beyond the built environment — socioeconomic conditions,
health-related social needs, pollution, health care, safety and food.

Prof. Shi and Prof. Wang, 2026-09-16 (#6, "Improve the quality of AI answers"):
step 2's notes listed factors the system "cannot yet measure — air/water quality
and pollution, noise, housing affordability and quality, income/employment and
socioeconomic equity, healthy-food availability …, healthcare capacity …,
personal and traffic safety, social cohesion, and broadband", and asking to
"add pollution data" or "demographic and socioeconomic factors" was refused.
"If the AI knows some important factors are not included, then why doesn't it
simply include them?"

Every source here is public and needs no key (the Census ACS API does, and this
install has none), verified live on 2026-09-16:

  svi     CDC/ATSDR Social Vulnerability Index 2022 — ACS 2018-2022 estimates by
          census tract, from CDC's public ArcGIS service.
  places  CDC PLACES — model-based estimates by census tract (BRFSS 2022-2023),
          incl. health-related social needs (food/housing insecurity, transport).
  chr     County Health Rankings & Roadmaps 2025 — county-level measures incl.
          fine particulate matter, drinking-water violations, providers, crashes.
  aqi     US EPA AirData — annual air-quality index summary per monitored county.

Tract sources are combined for a project area as a population-weighted average
of the tracts it covers (see `aggregate`); county sources describe the whole
county and are labelled so wherever they are shown. Nothing is ever filled in:
a value that cannot be computed is None, with the reason recorded.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import time
import zipfile
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "HCAS/1.0 (Healthy Community Assessment System; +https://github.com/luoojason)"}

SOURCES = {
    "svi": {
        "name": "CDC/ATSDR Social Vulnerability Index 2022 (American Community Survey 2018–2022)",
        "short": "CDC Social Vulnerability Index",
        "level": "census tract",
    },
    "places": {
        "name": "CDC PLACES (BRFSS model-based estimates, crude prevalence)",
        "short": "CDC PLACES",
        "level": "census tract",
    },
    "chr": {
        "name": "County Health Rankings & Roadmaps 2025 (University of Wisconsin Population Health Institute)",
        "short": "County Health Rankings",
        "level": "county",
    },
    "aqi": {
        "name": "US EPA AirData — annual air quality index by county",
        "short": "EPA AirData",
        "level": "county (counties with air monitors)",
    },
}

# metric -> definition. `field` is the source's column; `scale` converts the
# stored value to the unit in `unit`.
INDICATORS: dict[str, dict] = {
    # --- socioeconomic & demographic (SVI, tract) ---------------------------
    "poverty_rate": {"source": "svi", "field": "EP_POV150", "unit": "%",
                     "desc": "% of residents with income below 150% of the federal poverty level"},
    "unemployment_rate": {"source": "svi", "field": "EP_UNEMP", "unit": "%",
                          "desc": "% of the civilian labor force that is unemployed"},
    "housing_cost_burden": {"source": "svi", "field": "EP_HBURD", "unit": "%",
                            "desc": "% of households earning under $75,000 that spend over 30% of income on housing (housing affordability)"},
    "no_high_school_diploma": {"source": "svi", "field": "EP_NOHSDP", "unit": "%",
                               "desc": "% of adults 25+ without a high school diploma (education)"},
    "uninsured_rate": {"source": "svi", "field": "EP_UNINSUR", "unit": "%",
                       "desc": "% of residents without health insurance"},
    "no_vehicle_households": {"source": "svi", "field": "EP_NOVEH", "unit": "%",
                              "desc": "% of households without a vehicle (transport dependence)"},
    "no_internet_households": {"source": "svi", "field": "EP_NOINT", "unit": "%",
                               "desc": "% of households without a broadband internet subscription (digital access)"},
    "older_adults_share": {"source": "svi", "field": "EP_AGE65", "unit": "%",
                           "desc": "% of residents aged 65 and over"},
    "disability_rate": {"source": "svi", "field": "EP_DISABL", "unit": "%",
                        "desc": "% of residents with a disability"},
    "limited_english": {"source": "svi", "field": "EP_LIMENG", "unit": "%",
                        "desc": "% of residents 5+ who speak English less than well"},
    "minority_share": {"source": "svi", "field": "EP_MINRTY", "unit": "%",
                       "desc": "% of residents who are racial or ethnic minorities (equity)"},
    "crowded_housing": {"source": "svi", "field": "EP_CROWD", "unit": "%",
                        "desc": "% of occupied homes with more people than rooms (housing quality)"},
    "social_vulnerability": {"source": "svi", "field": "RPL_THEMES", "unit": "percentile (0–1)",
                             "desc": "overall social vulnerability, national percentile rank (higher = more vulnerable)"},
    # --- health-related social needs & access (PLACES, tract) ---------------
    "food_insecurity": {"source": "places", "field": "FOODINSECU", "unit": "%",
                        "desc": "% of adults food-insecure in the past 12 months (healthy food access)"},
    "housing_insecurity": {"source": "places", "field": "HOUSINSECU", "unit": "%",
                           "desc": "% of adults housing-insecure in the past 12 months"},
    "lack_of_transportation": {"source": "places", "field": "LACKTRPT", "unit": "%",
                               "desc": "% of adults lacking reliable transportation in the past 12 months"},
    "lack_of_health_insurance": {"source": "places", "field": "ACCESS2", "unit": "%",
                                 "desc": "% of adults 18–64 without health insurance"},
    "routine_checkup": {"source": "places", "field": "CHECKUP", "unit": "%",
                        "desc": "% of adults with a routine doctor checkup in the past year (healthcare use)"},
    "loneliness": {"source": "places", "field": "LONELINESS", "unit": "%",
                   "desc": "% of adults reporting loneliness (social cohesion)"},
    # --- environment, pollution, services, safety (CHR / EPA, county) --------
    "air_pollution_pm25": {"source": "chr", "field": "v125_rawvalue", "unit": "µg/m³",
                           "desc": "average daily fine particulate matter (PM2.5) — air pollution, county-wide"},
    "drinking_water_violation": {"source": "chr", "field": "v124_rawvalue", "unit": "1 = yes, 0 = no",
                                 "desc": "whether the county had a health-based drinking water violation (water quality), county-wide"},
    "severe_housing_problems": {"source": "chr", "field": "v136_rawvalue", "unit": "%", "scale": 100,
                                "desc": "% of households with overcrowding, high cost, or lack of kitchen/plumbing, county-wide"},
    "food_environment_index": {"source": "chr", "field": "v133_rawvalue", "unit": "0 (worst) – 10 (best)",
                               "desc": "food environment index (grocery access and food insecurity), county-wide"},
    "limited_healthy_food_access": {"source": "chr", "field": "v083_rawvalue", "unit": "%", "scale": 100,
                                    "desc": "% of low-income residents who live far from a grocery store, county-wide"},
    "primary_care_physicians": {"source": "chr", "field": "v004_rawvalue", "unit": "per 100,000 people", "scale": 100000,
                                "desc": "primary care physicians per 100,000 residents (healthcare capacity), county-wide"},
    "mental_health_providers": {"source": "chr", "field": "v062_rawvalue", "unit": "per 100,000 people", "scale": 100000,
                                "desc": "mental health providers per 100,000 residents (healthcare capacity), county-wide"},
    "exercise_opportunities": {"source": "chr", "field": "v132_rawvalue", "unit": "%", "scale": 100,
                               "desc": "% of residents with adequate access to places for physical activity, county-wide"},
    "injury_deaths": {"source": "chr", "field": "v135_rawvalue", "unit": "per 100,000 people",
                      "desc": "injury deaths per 100,000 residents (personal safety), county-wide"},
    "motor_vehicle_crash_deaths": {"source": "chr", "field": "v039_rawvalue", "unit": "per 100,000 people",
                                   "desc": "motor vehicle crash deaths per 100,000 residents (traffic safety), county-wide"},
    "social_associations": {"source": "chr", "field": "v140_rawvalue", "unit": "per 10,000 people",
                            "desc": "membership associations per 10,000 residents (social cohesion), county-wide"},
    "income_inequality": {"source": "chr", "field": "v044_rawvalue", "unit": "ratio",
                          "desc": "household income at the 80th percentile divided by the 20th (income inequality), county-wide"},
    "unhealthy_air_days": {"source": "aqi", "field": "pct_unhealthy_days", "unit": "% of days",
                           "desc": "% of monitored days with air quality unhealthy for sensitive groups or worse (AQI > 100), county-wide; only counties with EPA monitors"},
    "median_aqi": {"source": "aqi", "field": "Median AQI", "unit": "AQI",
                   "desc": "median daily air quality index over the last full year, county-wide; only counties with EPA monitors"},
}

SVI_FIELDS = sorted({d["field"] for d in INDICATORS.values() if d["source"] == "svi"})
PLACES_FIELDS = sorted({d["field"] for d in INDICATORS.values() if d["source"] == "places"})
CHR_FIELDS = sorted({d["field"] for d in INDICATORS.values() if d["source"] == "chr"})

TABLE_TTL_S = 30 * 86400
CHR_TTL_S = 180 * 86400

_SVI_URL = ("https://onemap.cdc.gov/OneMapServices/rest/services/SVI/"
            "CDC_ATSDR_Social_Vulnerability_Index_2022_USA/FeatureServer/2/query")
_PLACES_TRACT_URL = "https://data.cdc.gov/resource/cwsq-ngmh.json"
_CHR_URL = "https://www.countyhealthrankings.org/sites/default/files/media/document/analytic_data{year}.csv"
_AQI_URL = "https://aqs.epa.gov/aqsweb/airdata/annual_aqi_by_county_{year}.zip"


def sources_for(metrics) -> list[str]:
    return sorted({INDICATORS[m]["source"] for m in metrics if m in INDICATORS})


# --- caching ------------------------------------------------------------------

def _cached_json(path: Path, ttl_s: float, build):
    """Read `path` if fresh, else build(), store and return it. A failed build
    raises; a stale copy is used only when the rebuild fails, and says so."""
    fresh = path.exists() and time.time() - path.stat().st_mtime < ttl_s
    if fresh:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    try:
        data = build()
    except Exception:
        if path.exists():
            logger.warning(f"refresh of {path.name} failed; using the saved copy")
            return json.loads(path.read_text(encoding="utf-8"))
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
    return data


# --- fetchers -----------------------------------------------------------------

def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    # SVI marks suppressed cells with -999; nothing here can be negative.
    return None if x != x or x < 0 else x


def svi_tracts(state_fips: str, county_fips: str, cache_dir: Path) -> dict:
    """{tract GEOID: {"pop": E_TOTPOP, field: value}} for the county."""
    def build():
        r = requests.get(_SVI_URL, params={
            "where": f"STCNTY='{state_fips}{county_fips}'",
            "outFields": ",".join(["FIPS", "E_TOTPOP", *SVI_FIELDS]),
            "returnGeometry": "false", "f": "json", "resultRecordCount": 5000,
        }, headers=_UA, timeout=60)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(str(body["error"])[:200])
        out = {}
        for f in body.get("features") or []:
            a = f.get("attributes") or {}
            if not a.get("FIPS"):
                continue
            out[str(a["FIPS"])] = {"pop": _num(a.get("E_TOTPOP")),
                                   **{k: _num(a.get(k)) for k in SVI_FIELDS}}
        if not out:
            raise LookupError("the Social Vulnerability Index has no tracts for this county")
        return out
    return _cached_json(cache_dir / f"svi_{state_fips}{county_fips}.json", TABLE_TTL_S, build)


def places_tracts(state_fips: str, county_fips: str, cache_dir: Path,
                  fields=None) -> dict:
    """{tract GEOID: {"pop": totalpopulation, MEASURE: crude %}} for the county."""
    import hashlib

    fields = sorted(fields or PLACES_FIELDS)
    tag = "" if fields == PLACES_FIELDS else "_" + hashlib.sha1(",".join(fields).encode()).hexdigest()[:8]

    def build():
        ids = ",".join(f"'{m}'" for m in fields)
        r = requests.get(_PLACES_TRACT_URL, params={
            "countyfips": f"{state_fips}{county_fips}",
            "$where": f"measureid in({ids})",
            "$select": "locationname,measureid,data_value,totalpopulation",
            "$limit": 50000,
        }, headers=_UA, timeout=60)
        r.raise_for_status()
        out: dict[str, dict] = {}
        for row in r.json():
            t = str(row.get("locationname") or "")
            if not t:
                continue
            rec = out.setdefault(t, {"pop": _num(row.get("totalpopulation"))})
            rec[row.get("measureid")] = _num(row.get("data_value"))
        if not out:
            raise LookupError("CDC PLACES has no tract estimates for this county")
        return out
    return _cached_json(cache_dir / f"places_{state_fips}{county_fips}{tag}.json", TABLE_TTL_S, build)


def chr_counties(cache_dir: Path, year: int | None = None) -> dict:
    """{5-digit FIPS: {field: value}} for every county plus "00000" (US)."""
    def build():
        years = [year] if year else [time.gmtime().tm_year - k for k in range(0, 3)]
        last = None
        for y in years:
            try:
                r = requests.get(_CHR_URL.format(year=y), headers=_UA, timeout=120)
                head = r.content[:200].lstrip(b"\xef\xbb\xbf").lower()
                if r.status_code != 200 or not head.startswith(b"state fips"):
                    last = RuntimeError(f"County Health Rankings {y}: HTTP {r.status_code}")
                    continue
                rows = csv.reader(io.StringIO(r.content.decode("utf-8-sig", errors="replace")))
                next(rows)
                codes = next(rows)
                idx = {c: codes.index(c) for c in CHR_FIELDS if c in codes}
                fips_i = codes.index("fipscode")
                out = {"_year": y}
                for row in rows:
                    if len(row) <= fips_i:
                        continue
                    out[row[fips_i].zfill(5)] = {c: _num(row[i]) if i < len(row) else None
                                                 for c, i in idx.items()}
                return out
            except requests.RequestException as exc:
                last = exc
        raise last or RuntimeError("County Health Rankings data could not be downloaded")
    return _cached_json(cache_dir / "chr_counties.json", CHR_TTL_S, build)


def aqi_counties(cache_dir: Path) -> dict:
    """{"State|County": {"Median AQI": n, "pct_unhealthy_days": %}, "_year": y}
    for the most recent complete year EPA has published."""
    def build():
        this = time.gmtime().tm_year
        last = None
        for y in (this - 1, this - 2):
            try:
                r = requests.get(_AQI_URL.format(year=y), headers=_UA, timeout=60)
                if r.status_code != 200 or r.content[:2] != b"PK":
                    last = RuntimeError(f"EPA AirData {y}: HTTP {r.status_code}")
                    continue
                z = zipfile.ZipFile(io.BytesIO(r.content))
                with z.open(z.namelist()[0]) as fh:
                    rows = list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8")))
                out = {"_year": y}
                for row in rows:
                    days = _num(row.get("Days with AQI"))
                    bad = sum(_num(row.get(k)) or 0 for k in (
                        "Unhealthy for Sensitive Groups Days", "Unhealthy Days",
                        "Very Unhealthy Days", "Hazardous Days"))
                    out[f"{row['State']}|{row['County']}"] = {
                        "Median AQI": _num(row.get("Median AQI")),
                        "pct_unhealthy_days": round(100 * bad / days, 2) if days else None,
                        "days": days,
                    }
                return out
            except requests.RequestException as exc:
                last = exc
        raise last or RuntimeError("EPA AirData could not be downloaded")
    return _cached_json(cache_dir / "aqi_counties.json", TABLE_TTL_S, build)


# --- combining tracts into an area ---------------------------------------------

def aggregate(rows: dict, field: str, shares: dict[str, float]) -> tuple[float | None, float]:
    """Population-weighted average of `field` over the tracts in `shares`.

    `shares` maps tract GEOID -> the fraction of that tract's population inside
    the project area (1.0 for a whole tract). Returns (value, coverage), where
    coverage is the share of the area's population whose tract has a value.
    Below half coverage the value is not reported: a figure resting on a
    minority of the residents is not a figure for the area.
    """
    num = den = den_all = 0.0
    for tract, share in shares.items():
        rec = rows.get(tract)
        if not rec or share <= 0:
            continue
        pop = rec.get("pop") or 0.0
        w = share * pop
        den_all += w
        v = rec.get(field)
        if v is None or w <= 0:
            continue
        num += w * v
        den += w
    if den_all <= 0:
        return None, 0.0
    coverage = den / den_all
    if den <= 0 or coverage < 0.5:
        return None, coverage
    return num / den, coverage


def county_value(table: dict, key: str, field: str, scale: float = 1.0) -> float | None:
    rec = table.get(key) or {}
    v = rec.get(field)
    return None if v is None else v * scale


def aqi_key(table: dict, state_name: str, county_name: str) -> str | None:
    """EPA names counties without their suffix ("Franklin", "Orleans")."""
    def norm(s: str) -> str:
        s = (s or "").strip().lower()
        for suffix in (" county", " parish", " borough", " census area", " municipality",
                       " city and borough"):
            if s.endswith(suffix):
                s = s[: -len(suffix)]
        return s.replace(".", "").replace("'", "")

    want = (norm(state_name), norm(county_name))
    for key in table:
        if "|" not in key:
            continue
        st, co = key.split("|", 1)
        if (norm(st), norm(co)) == want:
            return key
    return None
