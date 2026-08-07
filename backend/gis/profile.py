"""County descriptive profile and areal-unit availability, both from Census.

Prof. Shi 2026-08-07 (feedback 5 and 10):

  10. "Remove State FIPS and COUNTY FIPS. Add some more descriptive information
      from the Census data, like total area, total population, median household
      income … I tried to ask AI what the population of the specified county
      and it said this is not available from the current data."

  5. "Allow the user to specify the areal unit (now it's fixed to Census Tract).
      If the data of the user-specified unit is not available, the system will
      report that after search for the data."

Everything here degrades honestly: a field that could not be fetched is absent
and named in `unavailable`, never defaulted to a plausible number.
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_TIGER = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "tigerWMS_Current/MapServer"
)
_COUNTY_LAYER = 82

# Areal units the user may pick, mapped to their TIGERweb layer. `county` needs
# no sub-unit lookup — the study area IS the unit.
#
# ZCTAs are the one unit that does not nest inside a county: TIGERweb has no
# county field on layer 2, so it is matched geographically (intersects the
# county envelope) rather than by GEOID prefix.
AREAL_UNITS = {
    "county": {"label": "County (whole study area)", "layer": None},
    "tract": {"label": "Census tract", "layer": 8, "prefix": True},
    "block_group": {"label": "Census block group", "layer": 10, "prefix": True},
    "zcta": {"label": "ZIP code (ZCTA)", "layer": 2, "prefix": False},
    "county_subdivision": {"label": "County subdivision (town/township)", "layer": 22, "prefix": True},
}
DEFAULT_AREAL_UNIT = "tract"


def _county_attributes(state_fips: str, county_fips: str) -> dict:
    """Raw TIGERweb attribute row for the county (AREALAND, AREAWATER, NAME…)."""
    r = requests.get(
        f"{_TIGER}/{_COUNTY_LAYER}/query",
        params={
            "where": f"GEOID='{state_fips}{county_fips}'",
            "outFields": "GEOID,NAME,BASENAME,AREALAND,AREAWATER,CENTLAT,CENTLON",
            "returnGeometry": "false",
            "f": "json",
        },
        timeout=20,
    )
    r.raise_for_status()
    feats = r.json().get("features") or []
    if not feats:
        raise LookupError(f"TIGERweb has no county {state_fips}{county_fips}")
    return feats[0].get("attributes") or {}


def _county_extent(state_fips: str, county_fips: str) -> tuple:
    """The county's real bounding box in EPSG:4326, from TIGERweb."""
    r = requests.get(
        f"{_TIGER}/{_COUNTY_LAYER}/query",
        params={
            "where": f"GEOID='{state_fips}{county_fips}'",
            "returnExtentOnly": "true",
            "outSR": "4326",
            "f": "json",
        },
        timeout=20,
    )
    r.raise_for_status()
    ext = (r.json() or {}).get("extent") or {}
    try:
        return (ext["xmin"], ext["ymin"], ext["xmax"], ext["ymax"])
    except KeyError:
        raise LookupError(f"TIGERweb returned no extent for {state_fips}{county_fips}")


def _acs_profile(state_fips: str, county_fips: str, api_key: str) -> dict:
    """ACS 5-year county figures. api.census.gov now rejects keyless requests
    (verified 2026-08-07: every endpoint returns a 'Missing Key' page with HTTP
    200), so this runs only when a key is configured."""
    r = requests.get(
        "https://api.census.gov/data/2023/acs/acs5",
        params={
            "get": "NAME,B01003_001E,B19013_001E,B25077_001E,B01002_001E",
            "for": f"county:{county_fips}",
            "in": f"state:{state_fips}",
            "key": api_key,
        },
        timeout=20,
    )
    r.raise_for_status()
    rows = r.json()  # [[header...], [values...]]
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError("unexpected ACS response shape")
    values = dict(zip(rows[0], rows[1]))

    def num(key):
        raw = values.get(key)
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        # ACS uses large negative sentinels (-666666666) for suppressed cells.
        return None if v < 0 else v

    return {
        "population": num("B01003_001E"),
        "median_household_income_usd": num("B19013_001E"),
        "median_home_value_usd": num("B25077_001E"),
        "median_age": num("B01002_001E"),
        "source_acs": "US Census ACS 5-year (2023)",
    }


def county_profile(
    state_fips: str,
    county_fips: str,
    population: int | None = None,
    census_api_key: str | None = None,
) -> dict:
    """Descriptive figures for the county shown from step 1 onward.

    `population` lets a caller pass one it already has (CDC PLACES carries a
    county total), used only when ACS is unavailable. Fields that could not be
    fetched are omitted and listed in `unavailable`, each with the reason.
    """
    out: dict = {"fips": f"{state_fips}{county_fips}", "unavailable": {}}

    try:
        attrs = _county_attributes(state_fips, county_fips)
        land_m2 = float(attrs.get("AREALAND") or 0)
        water_m2 = float(attrs.get("AREAWATER") or 0)
        out["name"] = attrs.get("NAME")
        out["land_area_km2"] = round(land_m2 / 1_000_000, 1)
        out["water_area_km2"] = round(water_m2 / 1_000_000, 1)
        out["total_area_km2"] = round((land_m2 + water_m2) / 1_000_000, 1)
        out["source_boundary"] = "US Census TIGERweb (county boundary attributes)"
    except Exception as exc:
        logger.warning(f"county_profile TIGERweb lookup failed: {exc!r}")
        out["unavailable"]["area"] = f"Census TIGERweb could not be reached ({type(exc).__name__})."

    if census_api_key:
        try:
            acs = _acs_profile(state_fips, county_fips, census_api_key)
            for k, v in acs.items():
                if v is not None:
                    out[k] = v
            for k in ("median_household_income_usd", "median_home_value_usd"):
                if out.get(k) is None:
                    out["unavailable"][k] = "suppressed for this county in ACS."
        except Exception as exc:
            logger.warning(f"county_profile ACS lookup failed: {exc!r}")
            out["unavailable"]["acs"] = (
                f"The Census ACS request failed ({type(exc).__name__}) — check the "
                "CENSUS_API_KEY in .env and the network."
            )
    else:
        out["unavailable"]["acs"] = (
            "Median household income, median home value and median age come from "
            "the Census ACS API, which now requires a free key. Add "
            "CENSUS_API_KEY=... to the .env file next to START-HCAS "
            "(https://api.census.gov/data/key_signup.html) and re-run this step."
        )

    if out.get("population") is None and population is not None:
        out["population"] = int(population)
        out["source_population"] = "CDC PLACES county total population"
    elif out.get("population") is not None:
        out["source_population"] = out.get("source_acs", "US Census ACS")

    if out.get("population") is None:
        out["unavailable"]["population"] = (
            "Not fetched yet — the county total arrives with the health data at "
            "step 3, or from ACS when a Census API key is configured."
        )

    if out.get("population") and out.get("land_area_km2"):
        # Named for its denominator: the assistant reads this dict verbatim, and
        # on a county that is half water it described a land-area density as
        # "land + water combined" because the key did not say (seen 2026-08-07).
        out["population_density_per_km2_of_land"] = round(
            out["population"] / out["land_area_km2"], 1
        )
    return out


def areal_unit_availability(state_fips: str, county_fips: str, unit: str) -> dict:
    """Search Census for the requested areal unit inside the county.

    Returns {unit, label, available, count, source, note}. `available` is False
    with a plain-language note when the search finds nothing or cannot run —
    Prof. Shi asked for the system to *report* that, not to fail or to silently
    substitute another unit.
    """
    spec = AREAL_UNITS.get(unit)
    if spec is None:
        return {
            "unit": unit, "label": unit, "available": False, "count": 0,
            "note": f"'{unit}' is not a unit this system knows. Choose one of: "
                    + ", ".join(AREAL_UNITS),
        }
    label = spec["label"]
    if spec["layer"] is None:  # county: the study area itself
        return {
            "unit": unit, "label": label, "available": True, "count": 1,
            "source": "US Census TIGERweb county boundary",
            "note": "The whole county is the reporting unit.",
        }

    geoid = f"{state_fips}{county_fips}"
    if spec.get("prefix"):
        where = f"GEOID LIKE '{geoid}%'"
        params = {"where": where, "outFields": "GEOID", "returnGeometry": "false",
                  "returnCountOnly": "true", "f": "json"}
    else:
        # ZCTAs cross county lines and carry no county field, so they are matched
        # geographically — against the county's OWN bounding box, asked for by
        # name. An earlier version used a fixed +/-0.75 degree box around the
        # centroid, which is ~120 km wide and reported 206 ZIP areas "for"
        # Grafton County; a bbox that is not the county's is not a search of the
        # county. The wording below still says overlap, because a bounding box
        # is not the polygon.
        try:
            xmin, ymin, xmax, ymax = _county_extent(state_fips, county_fips)
        except Exception as exc:
            return {"unit": unit, "label": label, "available": False, "count": 0,
                    "note": f"Could not locate the county to search for {label}s ({type(exc).__name__})."}
        params = {
            "geometry": f"{xmin},{ymin},{xmax},{ymax}",
            "geometryType": "esriGeometryEnvelope", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "GEOID", "returnGeometry": "false",
            "returnCountOnly": "true", "f": "json",
        }

    try:
        r = requests.get(f"{_TIGER}/{spec['layer']}/query", params=params, timeout=25)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(str(body["error"])[:200])
        count = int(body.get("count", 0))
    except Exception as exc:
        logger.warning(f"areal_unit_availability({unit}) failed: {exc!r}")
        return {
            "unit": unit, "label": label, "available": False, "count": 0,
            "source": "US Census TIGERweb",
            "note": f"The search for {label} boundaries could not be completed "
                    f"({type(exc).__name__}) — check the network and re-run this step.",
        }

    if count == 0:
        return {
            "unit": unit, "label": label, "available": False, "count": 0,
            "source": "US Census TIGERweb",
            "note": f"Census publishes no {label} boundaries for this county, so "
                    f"results stay at the county level.",
        }
    # Nested units are counted inside the county by GEOID; ZCTAs are counted by
    # bounding-box overlap, so say which was measured rather than letting an
    # overlap count read as a containment count.
    where = "in this county" if spec.get("prefix") else "overlapping this county's extent"
    return {
        "unit": unit, "label": label, "available": True, "count": count,
        "source": "US Census TIGERweb",
        "note": f"{count} {label} boundaries found {where}.",
    }
