def test_list_meters_paginates_and_reports_freshness(api):
    body = api.get("/api/v1/meters", params={"page_size": 5, "page": 2}).json()
    assert body["total"] == 43 and len(body["data"]) == 5 and body["page"] == 2
    assert body["as_of"]
    assert body["data"][0]["network"]["dt"]["node_id"].startswith("Z-")


def test_filters_and_proximity(api):
    body = api.get("/api/v1/meters", params={"dt": "DT-007", "status": "installed"}).json()
    assert body["total"] > 0 and all(m["dt_code"] == "DT-007" and m["status"] == "installed" for m in body["data"])

    near = api.get("/api/v1/meters", params={"lat": 26.938961, "lng": 75.830956, "radius_km": 0.5}).json()
    assert near["data"][0]["meter_id"] == "J100000" and near["data"][0]["distance_km"] < 0.01

    assert api.get("/api/v1/meters", params={"lat": 26.9}).status_code == 422


def test_get_meter_and_404(api):
    meter = api.get("/api/v1/meters/J100000").json()
    assert meter["serial_no"] == "SE33962" and meter["source"] == "snapshot"

    missing = api.get("/api/v1/meters/J999999")
    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "not_found", "message": "meter J999999 not found"}}


def test_get_meter_falls_back_to_live_pages_when_export_fails(api, portal):
    portal.fail_export = True
    meter = api.get("/api/v1/meters/J100004").json()
    assert meter["source"] == "live" and meter["record_format"] == "v2" and meter["status"] == "faulty"


def test_consumption(api):
    body = api.get("/api/v1/meters/J100089/consumption").json()
    assert body["interval_minutes"] == 1440
    assert body["summary"]["readings"] == 8
    assert body["summary"]["consumption_kwh"] == round(12732.34 - 12678.83, 3)
    assert body["data_quality"][0]["type"] == "duplicate_timestamp"
    assert body["readings"][0]["timestamp"] == "2026-06-23T00:00:00+05:30"


def test_consumption_validation(api):
    assert api.get("/api/v1/meters/J100000/consumption", params={"from": "2026-06-30", "to": "2026-06-01"}).status_code == 422
    bad = api.get("/api/v1/meters/J100000/consumption", params={"from": "30/06/2026"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"
    assert api.get("/api/v1/meters/J999999/consumption").status_code == 404


def test_consumption_is_cached(api, portal):
    api.get("/api/v1/meters/J100000/consumption")
    calls = len(portal.calls)
    api.get("/api/v1/meters/J100000/consumption")
    assert len(portal.calls) == calls


def test_transformers_and_hierarchy(api):
    dts = api.get("/api/v1/transformers").json()
    assert dts["total"] == 4
    dt7 = api.get("/api/v1/transformers/DT-007").json()
    assert dt7["aliases"] == ["Old Malviya Nagar Xfmr"] and len(dt7["meter_ids"]) == 13
    assert api.get("/api/v1/transformers/DT-999").status_code == 404

    tree = api.get("/api/v1/hierarchy", params={"depth": 2}).json()["data"]
    assert all(c["children"] == [] for z in tree for c in z["children"])
    sub = api.get("/api/v1/hierarchy", params={"root": "Z-01/C-01"}).json()["data"]
    assert sub[0]["id"] == "Z-01/C-01" and sub[0]["children"]
    assert api.get("/api/v1/hierarchy", params={"root": "nope"}).status_code == 404


def test_portal_rate_limit_maps_to_503_with_retry_after(api, portal):
    api.get("/api/v1/meters")  # warm up the session
    portal.rate_limit_next = 100
    response = api.get("/api/v1/meters/J100000/consumption")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "60"
    assert response.json()["error"]["code"] == "portal_rate_limited"


def test_snapshot_status_and_refresh(api):
    api.get("/api/v1/meters")
    status = api.get("/api/v1/snapshot").json()
    assert status["loaded"] and status["meter_count"] == 43 and status["repairs"]
    assert api.post("/api/v1/snapshot/refresh").json()["age_s"] == 0
