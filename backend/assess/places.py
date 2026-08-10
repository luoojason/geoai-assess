"""CDC PLACES county-level health outcomes (BRFSS model-based estimates).

One SODA query for the county, one for the US benchmark. Age-adjusted
prevalence only. These are modelled county-level estimates: since 2026-07-27 the
study area is the whole county, so they match its extent — but they are modelled
rather than measured and carry no variation within the county, so nothing here
may be attributed to a tract or a neighborhood. The report says so.
"""

# Defers annotation evaluation so the PEP 604 union in _query's return type
# (`int | None`, 3.10+ syntax) does not raise at import on Python 3.9. Prof. Shi
# hit exactly this class of crash on Windows Store Python 3.9 via
# app.py -> hcas.py -> gis/counties.py; that site was fixed, this one was missed,
# and hcas.py is safe only because it already carries this same import.
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_URL = "https://data.cdc.gov/resource/swc5-untb.json"

# measureid -> human label; the built-environment-relevant subset of PLACES
MEASURES = {
    "OBESITY": "Obesity",
    "DIABETES": "Diagnosed diabetes",
    "LPA": "No leisure-time physical activity",
    "MHLTH": "Frequent mental distress",
    "PHLTH": "Frequent physical distress",
    "DEPRESSION": "Depression",
    "CASTHMA": "Current asthma",
    "BPHIGH": "High blood pressure",
    "SLEEP": "Short sleep duration (<7 hours)",
    "LACKTRPT": "Lack of reliable transportation",
    # health-related social needs — the demographics/equity angle without
    # needing the (now key-gated) Census ACS API
    "FOODINSECU": "Food insecurity",
    "HOUSINSECU": "Housing insecurity",
    "FOODSTAMP": "Received food stamps (SNAP)",
}


def _query(**filters) -> tuple[dict[str, float], int | None]:
    """Return ({measureid: age-adjusted prevalence}, total_population | None).

    PLACES carries the county's `totalpopulation` on every measure row; we pull
    it once so the study-area attribute table can show a real population figure.
    """
    params = {
        "$limit": 50,
        "datavaluetypeid": "AgeAdjPrv",
        "$where": "measureid in({})".format(",".join(f"'{m}'" for m in MEASURES)),
        **filters,
    }
    r = requests.get(_URL, params=params, timeout=30)
    r.raise_for_status()
    rows = r.json()
    values = {
        row["measureid"]: float(row["data_value"])
        for row in rows
        if row.get("data_value")
    }
    population = None
    for row in rows:
        if row.get("totalpopulation"):
            try:
                population = int(float(row["totalpopulation"]))
            except (TypeError, ValueError):
                population = None
            break
    return values, population


def fetch_total_population(state_fips: str, county_fips: str) -> int | None:
    """The county's total population, without the health measures.

    PLACES carries `totalpopulation` on every row, so one small query answers
    "how many people live here" at step 1 — which the Census ACS API can no
    longer do without a key. Returns None if the request fails; the caller
    reports it as unavailable rather than guessing (Prof. Shi 2026-08-07 #10).
    """
    try:
        _values, population = _query(locationid=f"{state_fips}{county_fips}")
        return population
    except Exception as exc:
        logger.warning(f"fetch_total_population failed: {exc!r}")
        return None


def fetch_health_outcomes(state_fips: str, county_fips: str) -> dict:
    """County prevalence vs US benchmark for the registered PLACES measures.

    Returns {"source": ..., "measures": [{id, label, county_pct, us_pct}, ...]}.
    Measures suppressed or missing for the county are omitted. Raises on
    network/HTTP failure — the caller decides whether that is fatal.
    """
    county, population = _query(locationid=f"{state_fips}{county_fips}")
    us, _ = _query(stateabbr="US")
    return {
        "source": "CDC PLACES (BRFSS model-based estimates, age-adjusted %)",
        "total_population": population,
        "measures": [
            {
                "id": mid,
                "label": label,
                "county_pct": county[mid],
                "us_pct": us.get(mid),
            }
            for mid, label in MEASURES.items()
            if mid in county
        ],
    }
