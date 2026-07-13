"""CDC PLACES county-level health outcomes (BRFSS model-based estimates).

One SODA query for the county, one for the US benchmark. Age-adjusted
prevalence only. These are county-wide estimates — coarser than the 5km
study area the OSM metrics describe; the report says so.
"""

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


def _query(**filters) -> dict[str, float]:
    params = {
        "$limit": 50,
        "datavaluetypeid": "AgeAdjPrv",
        "$where": "measureid in({})".format(",".join(f"'{m}'" for m in MEASURES)),
        **filters,
    }
    r = requests.get(_URL, params=params, timeout=30)
    r.raise_for_status()
    return {
        row["measureid"]: float(row["data_value"])
        for row in r.json()
        if row.get("data_value")
    }


def fetch_health_outcomes(state_fips: str, county_fips: str) -> dict:
    """County prevalence vs US benchmark for the registered PLACES measures.

    Returns {"source": ..., "measures": [{id, label, county_pct, us_pct}, ...]}.
    Measures suppressed or missing for the county are omitted. Raises on
    network/HTTP failure — the caller decides whether that is fatal.
    """
    county = _query(locationid=f"{state_fips}{county_fips}")
    us = _query(stateabbr="US")
    return {
        "source": "CDC PLACES (BRFSS model-based estimates, age-adjusted %)",
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
