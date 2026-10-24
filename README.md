# Urja Meter Ops API

A clean, documented, read-only JSON API in front of **Urja Meter Ops**, a utility's internal
web portal for smart-meter data (meter details, network position, consumption) that has no
API of its own. Consumers of this service never need to touch the portal.

| Document | What's in it |
|---|---|
| [PROTOCOL.md](PROTOCOL.md) | How the portal actually works: auth, hidden endpoints, the signed bulk export, rate limits, data-quality issues |
| [openapi.json](openapi.json) | OpenAPI 3.1 description of **this** API (generated from code; browsable at `/docs` when running) |
| [REFLECTION.md](REFLECTION.md) | Answers to the reflection questions |

---

## What it does

```
            ┌──────────────────────── this service ─────────────────────────┐
 client ──► │ FastAPI routes ─► MeterService ─┬─► Catalog (in-memory snapshot) │
            │   app/main.py     app/service.py│     app/catalog.py             │
            │                                 │        ▲ refresh every 15 min   │
            │                                 └────────┴─► PortalClient ───────┼──► Urja portal
            │                                              app/portal/        │
            └─────────────────────────────────────────────────────────────────┘
```

- **Meters, transformers, hierarchy** are served from a **catalogue** built from the
  portal's bulk export: one signed request returns every meter with its hierarchy and
  coordinates. Filtering, proximity search and the network tree run locally on that
  snapshot. The portal can't do any of these itself.
- **Consumption** is fetched live per request, because the export doesn't include readings.
  It's normalised (typed numbers, ISO timestamps, per-interval kWh computed from the
  cumulative register) and cached for 5 minutes.
- **The portal session is managed for you**: the service logs in, notices expiry (the portal
  signals it three different ways), logs in again once even under concurrent load, stays
  under the portal's rate limit and retries 429/5xx.
- **Messy data is repaired, not hidden**: blank or conflicting hierarchy fields are fixed
  using the majority path of the meter's transformer. Every correction is listed in that
  meter's `data_quality` field.

### Endpoints (`/api/v1`)

| Method & path | Purpose |
|---|---|
| `GET /meters` | List/search/filter meters. Filters: `q` (ID/serial substring), `status`, `make`, `phase`, `install_type`, `record_format`, `dt`, `node` (any hierarchy node, e.g. `Z-01/C-01`), proximity (`lat`, `lng`, `radius_km`), `page`, `page_size` |
| `GET /meters/{id}` | One meter: nameplate, location, full network path, data-quality notes |
| `GET /meters/{id}/consumption?from=&to=` | Readings, per-interval kWh, summary (total kWh, voltage stats, peak interval), data-quality notes |
| `GET /transformers` · `GET /transformers/{code}` | Distribution transformers with capacity, meter counts by status, meter IDs |
| `GET /hierarchy?root=&depth=` | Zone → Circle → Division → Subdivision → Substation → Feeder → DT tree with meter counts per node |
| `GET /snapshot` · `POST /snapshot/refresh` | Catalogue age, staleness, repair counts, last error; force a reload |
| `GET /health` | Liveness (doesn't call the portal) |

Errors are always `{"error": {"code": "...", "message": "..."}}`:

| Situation | Status |
|---|---|
| Unknown meter, transformer or node | `404` |
| Invalid input (e.g. `from=30/06/2026`, `from > to`) | `422` |
| Portal down or rate-limiting us | `503` with `Retry-After` |
| Portal answered with something unexpected | `502` |

### Project layout

```
app/
  main.py            routes, error mapping, app factory
  service.py         use cases: meter lookup (with live fallback), consumption + cache
  catalog.py         snapshot build, hierarchy repair, tree, queries, freshness policy
  models.py          response schemas (drive openapi.json)
  config.py          settings from env / .env
  errors.py          domain errors -> HTTP status + error code
  portal/
    client.py        the only code that talks to the portal (auth, retries, signing)
    devalue.py       decoder for SvelteKit __data.json payloads
    parsing.py       portal shapes -> our shapes (both detail layouts, energy rows)
tests/               39 tests against a fake portal built from captured responses
scripts/export_openapi.py
```

---

## Running it

Requires Python 3.10+.

```bash
git clone https://github.com/saiteja7065/flock-energy-api.git && cd flock-energy-api
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                 # contains the portal credentials from the assignment
uvicorn app.main:app --port 8000
```

- Interactive docs: http://localhost:8000/docs (Swagger UI) or http://localhost:8000/redoc
- Tests (no network needed): `pytest`
- Regenerate the spec after changing the API: `python scripts/export_openapi.py`

**Deploying:** the [Dockerfile](Dockerfile) runs the service anywhere containers run. The live
demo uses it on a Hugging Face Space (Docker SDK, port 7860). [render.yaml](render.yaml) is an
alternative Render Blueprint. Either way, set `URJA_EMAIL` and `URJA_PASSWORD` as secrets in the
host's dashboard; they are never committed.

```bash
docker build -t urja-api . && docker run -p 7860:7860 --env-file .env urja-api
```

Configuration is via environment variables (or `.env`):

| Variable | Default | |
|---|---|---|
| `URJA_EMAIL`, `URJA_PASSWORD` | — (required) | portal login |
| `URJA_BASE_URL` | `https://urja-ops.flockenergy.tech` | |
| `URJA_SNAPSHOT_TTL_S` | `900` | catalogue refresh interval |
| `URJA_CONSUMPTION_TTL_S` | `300` | consumption cache lifetime |
| `URJA_MAX_REQUESTS_PER_MINUTE` | `100` | self-imposed limit (portal allows ~120) |
| `URJA_MAX_RETRIES`, `URJA_TIMEOUT_S` | `3`, `15` | |

### Sample requests

```bash
# Faulty three-phase meters within 3 km of a point, nearest first
curl "localhost:8000/api/v1/meters?status=faulty&phase=three&lat=26.91&lng=75.79&radius_km=3"

# One meter
curl localhost:8000/api/v1/meters/J100170
```
```json
{
  "meter_id": "J100170", "serial_no": "L&26991", "make": "HPL", "phase": "single",
  "status": "installed", "install_type": "ct_operated", "record_format": "v2", "dt_code": "DT-011",
  "location": {"lat": 26.941281921956783, "lng": 75.71731542362618},
  "network": {
    "zone":   {"code": "Z-02", "name": "Jaipur Zone 2", "node_id": "Z-02"},
    "circle": {"code": "C-05", "name": "Circle 5",      "node_id": "Z-02/C-05"},
    "dt":     {"code": "DT-011", "name": "Malviya Nagar DT 11", "node_id": "Z-02/C-05/D-01/SD-11/SS-11/F-011/DT-011"}
  },
  "data_quality": [
    {"field": "network.circle.code", "issue": "blank", "original": "", "resolved_to": "C-05"}
  ],
  "source": "snapshot", "as_of": "2026-10-24T12:01:26Z"
}
```

```bash
curl "localhost:8000/api/v1/meters/J100089/consumption"
```
```json
{
  "meter_id": "J100089", "requested_from": null, "requested_to": null,
  "interval_minutes": 1440, "timezone": "+05:30",
  "summary": {"readings": 8, "consumption_kwh": 53.51, "avg_voltage_r": 230.6,
              "min_voltage_r": 225.0, "max_voltage_r": 235.0,
              "first_reading_at": "2026-06-23T00:00:00+05:30", "last_reading_at": "2026-06-30T00:00:00+05:30",
              "peak_interval": {"timestamp": "2026-06-24T00:00:00+05:30", "kwh": 7.65}},
  "readings": [
    {"timestamp": "2026-06-30T00:00:00+05:30", "kwh_register": 12732.34, "kvah_register": 13750.93,
     "voltage_r": 229.0, "kwh_delta": 7.64}
  ],
  "data_quality": [{"type": "duplicate_timestamp", "timestamp": "2026-06-30T00:00:00+05:30",
                    "detail": "kept the most complete row"}]
}
```
(Trimmed: the meter's `network` also has division, subdivision, substation and feeder; `readings` shows one entry of eight.)

---

## Assumptions

- **Timestamps are IST (+05:30).** The portal sends none. The utility is in Jaipur, and India has no DST.
- **`kwh`/`kvah` are cumulative registers.** They only ever increase, by small and steady
  amounts. So consumption = the difference between consecutive readings; a decrease is flagged, not used.
- **Codes below circle level are unique only within their parent** (see PROTOCOL §5). Nodes are identified by code path.
- **The majority path of a DT is the truth.** A meter whose hierarchy disagrees with the
  other meters on its DT (always by a blank field, or the DT's old name) is a data-entry error.
- **The transformer list is authoritative for DT names.** Other names become `aliases`.
- **The data changes slowly.** It's a meter registry plus readings that end on 30 Jun 2026,
  so a 15-minute catalogue TTL and a 5-minute consumption cache are safe.
- **This service runs inside a trusted network**, like the portal it replaces, so it has no
  auth of its own (see "left out").
- **The rate limit is about 120 requests/minute**, measured. It isn't documented anywhere.

## Design decisions and trade-offs

- **Bulk export + local catalogue instead of proxying each request.** One call (about 0.2 s)
  replaces the 21 search pages plus 403 detail pages plus 403 geo calls it would take to
  collect the same data, which is several times the rate limit. It's also the only way to
  answer cross-cutting questions ("faulty meters under circle C-05 near this point").
  Cost: data can be up to 15 minutes old. Every response carries `as_of`, and
  `POST /snapshot/refresh` exists for when that matters.
- **Stale beats down.** If a refresh fails, the previous snapshot keeps being served
  (`/snapshot` shows `stale: true` and the error), and refreshes pause for 60 s so an outage
  doesn't make every call slow. With no snapshot at all, `GET /meters/{id}` falls back to the
  per-meter pages (`source: "live"`).
- **Two detail formats → one schema.** I implemented the `legacy`/`v2` difference and the
  devalue decoder even though the export makes them unnecessary in normal operation. That's
  the live fallback, and it proves the per-page path works if the export goes away.
- **Consumption stays live.** It isn't in the export, and fetching 403 histories would use the
  whole rate-limit budget for 4 minutes. A short cache keeps repeat views cheap.
- **Our own date validation.** The portal silently ignores malformed dates and returns a
  default window, which is a trap for callers. We return a 422 instead.
- **Open enumerations.** `status`, `phase` etc. are normalised to snake_case but not
  constrained to a fixed set. A new portal value passes through instead of breaking responses.
  Known values are documented in the schema.
- **Self-throttling below the portal's limit**, plus retries with backoff. Requests queue
  briefly rather than fail. After retries are used up, callers get `503` + `Retry-After`.
- **Path-parameter validation** (`^[A-Za-z0-9_-]{1,32}$`) so a meter ID can't be used to
  reach other portal paths (e.g. `../keys`).
- **FastAPI + httpx (async)** because the schema-to-OpenAPI generation is free, and one async
  client gives a single shared session and connection pool.

### Scale of the local query layer
Filters are a linear scan over an in-memory dict. Proximity is haversine over every meter.
At 403 meters that takes microseconds. This holds comfortably up to roughly 10⁵ meters
(milliseconds per query, tens of MB). Past that, or once several service instances need to
share state, I'd persist the snapshot to SQLite/Postgres (PostGIS or an R-tree for
proximity, indexes on status/DT/node path) and rebuild it incrementally. The export
endpoint would also need to become truly paginated: today it returns everything in one
response and ignores `page`.

## What I left out, and why

- **Auth on this API.** Out of scope for a read-only internal service. In production I'd put
  it behind the organisation's gateway or add API keys.
- **Persisting the catalogue.** In memory is enough at this size. A restart costs one export call.
- **The live search endpoint** (`/portal/meters/search`). The catalogue answers everything it
  can, and more. It's documented in PROTOCOL.md.
- **Bulk consumption / aggregates across meters** (e.g. consumption per DT). This would need
  every meter's history: ~403 calls, about 4 minutes of rate-limit budget. Feasible as a
  background job but not worth it in this time box.
- **A web client.** I put the time into the investigation and data correctness instead.
- **Metrics and tracing.** Logging only.

## What I'd improve with more time

1. A background refresh of the catalogue (stale-while-revalidate) so no request pays the refresh latency.
2. A background job to harvest consumption for all meters within the rate budget, enabling
   DT-level load, anomaly detection (flat or decreasing registers, voltage excursions) and
   consumption-weighted maps.
3. A small web client: map of meters coloured by status, hierarchy explorer, consumption chart.
4. Contract tests that run against the live portal on a schedule to catch upstream changes
   (new detail layouts, signature changes).
5. Prometheus metrics: portal latency, 429s, re-logins, snapshot age.

## Reflection

See [REFLECTION.md](REFLECTION.md).
