import pytest

from app.errors import NotFound
from app.portal.devalue import SessionExpired, page_data, unflatten
from tests.fake_portal import fixture


def test_unflatten_resolves_shared_references():
    assert unflatten([{"a": 1, "b": 2}, "x", [1, -1]]) == {"a": "x", "b": ["x", None]}


def test_unflatten_typed_values():
    data = unflatten([{"when": 1, "tags": 2}, ["Date", "2026-06-30T00:00:00.000Z"], ["Set", 3], "a"])
    assert data["when"].year == 2026 and data["tags"] == ["a"]


def test_legacy_meter_page():
    data = page_data(fixture("detail_legacy.json"))
    assert data["meterId"] == "J100000"
    assert {"parameterName": "Serial No", "parameterValue": "SE33962"} in data["detail"]["data"]
    assert data["hierarchy"]["DT"] == "Malviya Nagar DT 1 (DT-001)"


def test_v2_meter_page_keeps_class_data_as_string():
    data = page_data(fixture("detail_v2.json"))
    assert isinstance(data["detail"]["classData"], str)


def test_missing_meter_page_raises_not_found():
    with pytest.raises(NotFound):
        page_data(fixture("detail_missing.json"))


def test_logged_out_payload_raises_session_expired():
    with pytest.raises(SessionExpired):
        page_data(fixture("detail_logged_out.json"))
