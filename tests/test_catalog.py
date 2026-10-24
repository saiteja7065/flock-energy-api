import asyncio

import pytest

from app.catalog import Catalog, build_snapshot, haversine_km, query_meters
from app.errors import PortalError, PortalUnavailable
from tests.conftest import make_client
from tests.fake_portal import FakePortal


@pytest.fixture
def snapshot():
    portal = FakePortal()
    return build_snapshot(portal.export, portal.dts)


def test_blank_codes_and_names_are_repaired_and_recorded(snapshot):
    # J100170 has a blank circle code in the export (captured from the live portal).
    meter = snapshot.meters["J100170"]
    assert meter["network"]["circle"]["code"] == "C-05"
    assert {"field": "network.circle.code", "issue": "blank", "original": "", "resolved_to": "C-05"} in meter["data_quality"]


def test_renamed_dt_keeps_authoritative_name_and_alias(snapshot):
    dt7 = snapshot.transformers["DT-007"]
    assert dt7["name"] == "Sanganer DT 7"
    assert dt7["aliases"] == ["Old Malviya Nagar Xfmr"]
    assert dt7["meter_count"] == 13
    renamed = snapshot.meters["J100400"]
    assert renamed["network"]["dt"]["name"] == "Sanganer DT 7"
    assert renamed["data_quality"][0]["issue"] == "conflict"


def test_reused_codes_become_distinct_nodes(snapshot):
    # D-01 exists under C-01 (DT-001) and under C-05 (DT-011): two different divisions.
    divisions = [n["id"] for n in snapshot.nodes.values() if n["level"] == "division" and n["code"] == "D-01"]
    assert sorted(divisions) == ["Z-01/C-01/D-01", "Z-02/C-05/D-01"]


def test_tree_counts_add_up(snapshot):
    assert sum(z["meter_count"] for z in snapshot.tree) == len(snapshot.meters)
    for node in snapshot.nodes.values():
        if node["children"]:
            assert node["meter_count"] == sum(c["meter_count"] for c in node["children"])


def test_query_filters(snapshot):
    assert {m["dt_code"] for m, _ in query_meters(snapshot, dt="DT-011")} == {"DT-011"}
    under_c01 = query_meters(snapshot, node="Z-01/C-01")
    assert under_c01 and all(m["network"]["circle"]["node_id"] == "Z-01/C-01" for m, _ in under_c01)
    assert [m["meter_id"] for m, _ in query_meters(snapshot, q="se339")] == ["J100000"]
    assert all(m["status"] == "faulty" for m, _ in query_meters(snapshot, status="faulty"))


def test_query_near_sorts_by_distance(snapshot):
    origin = snapshot.meters["J100000"]["location"]
    results = query_meters(snapshot, near=(origin["lat"], origin["lng"], 5))
    assert results[0][0]["meter_id"] == "J100000"
    distances = [d for _, d in results]
    assert distances == sorted(distances) and distances[-1] <= 5


def test_haversine():
    assert haversine_km(26.0, 75.0, 27.0, 75.0) == pytest.approx(111.195, abs=0.01)  # one degree of latitude


def test_catalog_serves_stale_snapshot_when_refresh_fails():
    portal = FakePortal()
    now = [0.0]
    catalog = Catalog(make_client(portal), ttl_s=10, clock=lambda: now[0])

    first = asyncio.run(catalog.get())
    now[0] = 11
    portal.fail_export = True
    assert asyncio.run(catalog.get()) is first
    status = catalog.status()
    assert status["stale"] and "signature" in status["last_error"]


def test_catalog_fails_fast_during_cooldown_without_snapshot():
    portal = FakePortal()
    portal.fail_export = True
    catalog = Catalog(make_client(portal), ttl_s=10, clock=lambda: 0.0)
    with pytest.raises(PortalError):
        asyncio.run(catalog.get())
    calls = len(portal.calls)
    with pytest.raises(PortalUnavailable):
        asyncio.run(catalog.get())
    assert len(portal.calls) == calls  # no new portal traffic
