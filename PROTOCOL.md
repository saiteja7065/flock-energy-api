# PROTOCOL — how the Urja Meter Ops portal works

Everything here was found by observing the portal as a normal logged-in user. I used
`curl`, read the JavaScript bundles the browser downloads, and called the same endpoints the
pages call. Nothing was written to the portal. Observations are from 24 Oct 2026.

## 1. What it is

`https://urja-ops.flockenergy.tech` is a **SvelteKit** app. The `X-Sveltekit-Page` header,
the `/_app/immutable/...` bundles and the `__sveltekit_*` bootstrap script give it away. Pages
are server-rendered shells. Most data is then loaded by in-page `fetch` calls to a small
JSON API under `/portal/*`. Auth is handled by **better-auth**.

| Page | What the browser calls |
|---|---|
| `/login` | `POST /login` (SvelteKit form action) |
| `/meters` (list + search box) | `GET /portal/meters/search?q=&page=` |
| `/meters/{id}` (detail) | server-side load data (`/meters/{id}/__data.json`) + `GET /portal/meters/{id}/geo` + `GET /portal/meters/{id}/energy` |
| `/transformers` | `GET /portal/dts?page=`; **"Export" button** → `GET /portal/keys`, then signed `GET /portal/export` |
| Sign out | `POST /api/auth/sign-out` |

How I found the API: the server-rendered `/meters` table said "No meters found", so the
rows had to arrive later from JavaScript. Reading the route bundles
(`/_app/immutable/nodes/*.js`, listed in `entry/app.*.js`) and grepping for `` fetch(` `` turned up
every endpoint above.

## 2. Authentication

**Login**: `POST /login`, `application/x-www-form-urlencoded`, fields `email` and `password`.

- SvelteKit's CSRF guard returns **403 "Cross-site POST form submissions are forbidden"**
  unless the `Origin` header equals the portal's own origin.
- With `Accept: application/json` (or `x-sveltekit-action: true`) the result is JSON:
  - success → `200 {"type":"redirect","status":303,"location":"/meters"}` plus
    `Set-Cookie: __Secure-better-auth.session_token=…; Max-Age=3600; HttpOnly; Secure; SameSite=Lax`
  - **wrong password → also HTTP 200**, with `{"type":"failure","status":401,"data":"…Invalid email or password."}`.
    You can't use the status code to tell whether login worked. Check the body type and that the cookie was set.
- The session lasts **1 hour** (`Max-Age=3600`; `GET /api/auth/get-session` shows `expiresAt`).

**Using the session**: send the cookie, nothing else. The `/portal/*` endpoints need no CSRF
token or bearer header.

**What an expired or missing session looks like.** It differs by surface, so the client checks all three:

| Surface | Response |
|---|---|
| `/portal/*` JSON API | `401 {"error":"unauthorized","message":"A valid session is required."}` |
| `/meters/{id}/__data.json` | **`200`** `{"type":"redirect","location":"/login"}` |
| HTML pages | `302 Location: /login` |

## 3. Endpoints and data

All JSON. Numbers mostly arrive as **strings**.

### `GET /portal/meters/search?q=<text>&page=<n>`
`{"data":[{meterId, serialNo, make, phaseType, installStatus, dtCode}], "total":403, "page":1, "pageSize":20}`
- `q` is a **case-insensitive substring match on meter ID and serial number only** (`HPL` and `DT-001` → 0 results).
- Page size is fixed at 20 (`pageSize`/`limit` are ignored). `page=0` is clamped to 1, and a page past the end returns `[]`.

### `GET /portal/dts?page=<n>`
`{"data":[{code, name, feederCode, capacityKva}], "total":40}`. This is the only source of
transformer capacity, which comes back as an integer.

### `GET /meters/{id}/__data.json` (meter detail + hierarchy)
There's no JSON API for meter details. They only exist in the page's server-side load data,
serialised with **devalue**: a flat array where objects refer to other entries by index.
Decoded, it's `{meterId, detail, hierarchy}`.

- `hierarchy`: `{"Zone":"Jaipur Zone 1 (Z-01)", "Circle":…, "Division":…, "Subdivision":…, "Sub Station":…, "Feeder":…, "DT":"Malviya Nagar DT 1 (DT-001)"}`.
  Each level is a `"Name (CODE)"` string.
- **`detail` has two different layouts**, depending on an internal record format the UI never shows:
  - *legacy*: `{"data":[{"parameterName":"Serial No","parameterValue":"SE33962"}, …]}`
  - *v2*: `{"classData":"{\"installed_meter\":{\"MeterId\":\"J100004\",\"SerialNo\":…}}"}`. That's a **JSON document
    encoded as a string**, with PascalCase keys. The page's own JS has an `if ("classData" in e)` branch for this.
- Unknown meter → **HTTP 200**, with the page node set to `{"type":"error","status":404,"error":{"message":"Meter not found"}}`.

### `GET /portal/meters/{id}/geo`
`{"data":{"latitude":"26.93…","longitude":"75.83…"}}`, as strings. Unknown meter → `404 {"error":"not_found"}`.

### `GET /portal/meters/{id}/energy[?from=YYYY-MM-DD&to=YYYY-MM-DD]`
`{"data":[{"timestamp":"23/06/2026 23:30","kwh":"48438.74","kvah":"52313.84","voltR":"226"}, …]}`
- `kwh`/`kvah` are **cumulative register readings**, not consumption per interval. Consumption is the difference between consecutive readings.
- Timestamps are `DD/MM/YYYY HH:mm` with **no timezone**.
- **Undocumented range parameters**: the UI never sends any, but `from`/`to` (ISO dates, inclusive) work.
  - No params: the latest 7 days ending `30/06/2026 23:30`. Half-hourly meters get 337 rows (7×48 **+1**, an off-by-one that starts at 23:30 the day before).
  - Only `from` or only `to`: a 7-day window anchored on that date.
  - `from > to`, or a range with no data: `[]`.
  - **Invalid formats are silently ignored** (`from=garbage` or `from=01/06/2026` give the default window, not an error).
  - Data exists for **June 2026 only**. `2026-01-01..2026-12-31` returns 1 Jun–30 Jun (1440 half-hourly rows).
- **Two granularities**: 40 meters report every 30 minutes; the other 363 report **daily** at 00:00.

### `GET /portal/keys` and `GET /portal/export` — the bulk path
The Transformers page has an Export button. Its code:

1. `GET /portal/keys` → `{"data":{"signingSecret":"…"}}`
2. Build `message = "GET\n/portal/export\npage=1\n<unix seconds>"`
3. `signature = hex(HMAC-SHA256(signingSecret, message))` (Web Crypto in the browser)
4. `GET /portal/export?page=1` with headers `x-timestamp: <unix seconds>` and `x-signature: <hex>`

The response is **every meter in one call** (403 rows, about 0.2 s), richer than anything else:
`{meterId, serialNo, make, phaseType, installStatus, installType, build, dtCode, hierarchy:{zone…dt:{name,code}}, geo:{lat,lng}}`.
The hierarchy is already structured and the coordinates are numbers.
- Missing or wrong signature, or a timestamp 10 minutes old → `401 {"error":"signature_invalid"}`. The server checks clock freshness, so the client signs using the portal's clock, taken from its `Date` header.
- `page` is ignored: page 2 and `pageSize=500` return the same 403 rows.
- The secret was the same across separate sessions, but the client fetches it at runtime anyway and re-fetches it once if a signature is rejected.
- `build` (`legacy` 238 / `v2` 165) matches which detail layout the meter page uses.

## 4. Rate limiting

There's no documentation and there are no `X-RateLimit-*` or `Retry-After` headers. I found the
limit by hitting it while reading energy for every meter in a row:
`429 {"error":"rate_limited","message":"Rate limit exceeded; slow down."}`.
Measured: **about 120 requests per rolling ~60 s window** (119 calls in 24 s passed; access came back about 37 s later).
It applies across endpoints.

Consequences for the design:
- Bulk work goes through the export: 1 call instead of 21 search pages, plus 403 detail pages and 403 geo calls.
- The client throttles itself to 100 requests/min and retries 429s with backoff.

## 5. Data quality — the messy parts

Out of 403 meters:

| Issue | Example | Count |
|---|---|---|
| Blank hierarchy **code** with a name | `circle: {"name":"Circle 5","code":""}` | 14 |
| Blank hierarchy **name** with a code | `feeder: {"name":"","code":"F-003"}` | 8 |
| DT under an **old name** | DT-007 appears as "Sanganer DT 7" (10 meters) and "Old Malviya Nagar Xfmr" (3 meters: J100400–J100402) | 3 |
| **Codes reused across parents** | `D-01` sits under C-01, C-03 and C-05; `F-001` under SS-01 and SS-11 | structural |
| Duplicate reading, blank values | J100089 has two `30/06/2026 00:00` rows, one with `kwh:""`, `voltR:""` | rare |
| Serial prefix disagrees with make | Prefixes look like make codes (`AL`, `SE`, `GE`, `HP`, `L&`) but match the meter's `make` only 80/403 times, e.g. J100003 is a Genus with serial `L&84997`. Left as-is: no way to tell which field is wrong. | 323 |
| Mixed reading granularity | 30-min vs daily | 40 / 363 |
| Numbers as strings, dates `DD/MM/YYYY`, no timezone | everywhere | — |

How I read the hierarchy:
- Every meter's `dtCode` agrees with the DT in its hierarchy, and every DT has one clear
  **majority path**. Each odd variant comes from a single meter with a blank field. So blanks are
  data-entry gaps, not real topology differences.
- Once those are repaired, codes below circle level still repeat under different parents.
  The names repeat too ("Division 1" under three circles). A radial distribution network can't
  feed one feeder from two substations, so I treat codes as **unique within their parent**
  and identify nodes by their full code path. DT codes and circle codes are globally unique.
- The three "Old Malviya Nagar Xfmr" meters are the last three meter IDs. Every other DT has
  exactly 10 meters, and these three are the only meters with a `GN…` serial scheme. So they
  look like later additions, from a different intake process, recorded under the DT's previous name.
  Their codes agree with DT-007, so they stay on DT-007 and the name becomes an alias. Location
  doesn't settle it: meters on every DT are spread over about 12 km. **This is an assumption.**

## 6. Other observations

- Response bodies are small, and nothing is cached (`Cache-Control: private, no-store` on `__data.json`).
- `GET /api/auth/get-session` (better-auth) shows session details, including the IP and user agent.
- The portal's own JS has no retry or error handling. If a fetch fails, the table just stays empty.
