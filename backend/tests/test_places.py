"""CDC PLACES outcome fetcher — parsing and dispatch, no network."""

from unittest.mock import Mock, patch

import pytest

from backend.assess import places


def _resp(rows):
    m = Mock()
    m.json.return_value = rows
    m.raise_for_status = Mock()
    return m


def test_fetch_pairs_county_with_us_benchmark():
    county_rows = [
        {"measureid": "OBESITY", "data_value": "19.2"},
        {"measureid": "LPA", "data_value": "20.4"},
        {"measureid": "SLEEP", "data_value": ""},  # suppressed -> omitted
    ]
    us_rows = [
        {"measureid": "OBESITY", "data_value": "33.3"},
        {"measureid": "LPA", "data_value": "23.7"},
    ]
    with patch.object(places.requests, "get", side_effect=[_resp(county_rows), _resp(us_rows)]) as g:
        out = places.fetch_health_outcomes("36", "061")

    assert g.call_count == 2
    assert g.call_args_list[0].kwargs["params"]["locationid"] == "36061"
    assert g.call_args_list[1].kwargs["params"]["stateabbr"] == "US"
    by_id = {m["id"]: m for m in out["measures"]}
    assert by_id["OBESITY"] == {
        "id": "OBESITY", "label": "Obesity", "county_pct": 19.2, "us_pct": 33.3,
    }
    assert "SLEEP" not in by_id
    assert "PLACES" in out["source"]


def test_fetch_raises_on_http_error():
    bad = Mock()
    bad.raise_for_status.side_effect = RuntimeError("503")
    with patch.object(places.requests, "get", return_value=bad):
        with pytest.raises(RuntimeError):
            places.fetch_health_outcomes("36", "061")
