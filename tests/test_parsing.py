from app.portal.devalue import page_data
from app.portal.parsing import meter_from_detail_page, normalise_energy, parse_labelled, slug
from tests.fake_portal import fixture


def test_parse_labelled():
    assert parse_labelled("Malviya Nagar DT 1 (DT-001)") == {"name": "Malviya Nagar DT 1", "code": "DT-001"}
    assert parse_labelled("Circle 5") == {"name": "Circle 5", "code": ""}
    assert parse_labelled(" (C-05)") == {"name": "", "code": "C-05"}
    assert parse_labelled(None) == {"name": "", "code": ""}


def test_slug():
    assert slug("CT Operated") == "ct_operated"
    assert slug("") is None


def test_both_detail_layouts_normalise_to_the_same_shape():
    legacy = meter_from_detail_page(page_data(fixture("detail_legacy.json")), {"latitude": "26.9", "longitude": "75.8"})
    v2 = meter_from_detail_page(page_data(fixture("detail_v2.json")), None)

    assert legacy["record_format"] == "legacy" and v2["record_format"] == "v2"
    assert legacy.keys() == v2.keys()
    assert (legacy["serial_no"], legacy["status"], legacy["install_type"]) == ("SE33962", "decommissioned", "whole_current")
    assert (v2["meter_id"], v2["serial_no"], v2["status"], v2["install_type"]) == ("J100004", "SE65293", "faulty", "ct_operated")
    assert legacy["location"] == {"lat": 26.9, "lng": 75.8} and v2["location"] is None
    assert v2["network"]["dt"] == {"name": "Bani Park DT 5", "code": "DT-005"}


def test_energy_half_hourly():
    readings, issues, interval = normalise_energy(fixture("energy_halfhourly.json")["data"])
    assert interval == 30 and issues == []
    assert readings[0]["timestamp"].isoformat() == "2026-06-30T00:00:00+05:30"
    assert readings[0]["kwh_delta"] is None
    assert readings[1]["kwh_delta"] == round(readings[1]["kwh_register"] - readings[0]["kwh_register"], 4)


def test_energy_daily_with_blank_duplicate_row():
    readings, issues, interval = normalise_energy(fixture("energy_daily_dup.json")["data"])
    assert interval == 1440
    assert len(readings) == 8  # 9 rows, one duplicate dropped
    assert readings[-1]["kwh_register"] == 12732.34  # the complete row wins
    assert [i["type"] for i in issues] == ["duplicate_timestamp"]


def test_energy_register_decrease_and_missing_values_are_flagged():
    rows = [
        {"timestamp": "01/06/2026 00:00", "kwh": "10", "kvah": "11", "voltR": "230"},
        {"timestamp": "01/06/2026 00:30", "kwh": "9", "kvah": "12", "voltR": ""},
        {"timestamp": "bad", "kwh": "9", "kvah": "12", "voltR": "230"},
    ]
    readings, issues, _ = normalise_energy(rows)
    assert readings[1]["kwh_delta"] is None
    assert {i["type"] for i in issues} == {"missing_values", "register_decreased", "unparseable_timestamp"}
