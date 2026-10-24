"""An in-process imitation of the Urja portal, built from captured responses.

It reproduces the behaviours the client must cope with: form-action login
(HTTP 200 even on failure), better-auth cookie, 401 JSON on /portal/*,
redirect-payload on __data.json, HMAC-signed export, and 429s on demand.
"""

import hashlib
import hmac
import json
import time
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures"
COOKIE = "__Secure-better-auth.session_token"
SECRET = "test-secret"
EMAIL, PASSWORD = "operator@urja.local", "urja-ops-2026"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakePortal:
    def __init__(self):
        self.export = fixture("export_sample.json")["data"]
        dts = {}
        for r in self.export:
            h = r["hierarchy"]
            if h["dt"]["name"] and h["dt"]["name"] != "Old Malviya Nagar Xfmr" and h["feeder"]["code"]:
                dts[r["dtCode"]] = {"code": r["dtCode"], "name": h["dt"]["name"], "feederCode": h["feeder"]["code"], "capacityKva": 100}
        self.dts = sorted(dts.values(), key=lambda d: d["code"])
        self.sessions: set[str] = set()
        self.logins = 0
        self.calls: list[str] = []
        self.rate_limit_next = 0  # respond 429 to this many upcoming requests
        self.fail_export = False

    def expire_sessions(self):
        self.sessions.clear()

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self.rate_limit_next:
            self.rate_limit_next -= 1
            return httpx.Response(429, json={"error": "rate_limited", "message": "Rate limit exceeded; slow down."})

        if path == "/login" and request.method == "POST":
            return self._login(request)

        authed = request.headers.get("cookie", "").replace(f"{COOKIE}=", "") in self.sessions
        if path.endswith("/__data.json"):
            if not authed:
                return httpx.Response(200, json={"type": "redirect", "location": "/login"})
            meter_id = path.split("/")[2]
            name = {"J100000": "detail_legacy.json", "J100004": "detail_v2.json"}.get(meter_id, "detail_missing.json")
            return httpx.Response(200, json=fixture(name))
        if not authed:
            return httpx.Response(401, json={"error": "unauthorized", "message": "A valid session is required."})

        if path == "/portal/keys":
            return httpx.Response(200, json={"data": {"signingSecret": SECRET}})
        if path == "/portal/export":
            return self._export(request)
        if path == "/portal/dts":
            page = int(request.url.params.get("page", 1))
            return httpx.Response(200, json={"data": self.dts[(page - 1) * 20 : page * 20], "total": len(self.dts)})
        if path.startswith("/portal/meters/"):
            meter_id, kind = path.split("/")[3:5]
            if meter_id not in {r["meterId"] for r in self.export} | {"J100004", "J100089"}:
                return httpx.Response(404, json={"error": "not_found", "message": "Meter not found"})
            if kind == "geo":
                return httpx.Response(200, json={"data": {"latitude": "26.9", "longitude": "75.8"}})
            name = "energy_daily_dup.json" if meter_id == "J100089" else "energy_halfhourly.json"
            return httpx.Response(200, json=fixture(name))
        return httpx.Response(404, text="not found")

    def _login(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("origin") != "https://urja.test":
            return httpx.Response(403, text="Cross-site POST form submissions are forbidden")
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        if form.get("email") != EMAIL.replace("@", "%40") or form.get("password") != PASSWORD:
            data = '[{"email":1,"error":2},"x","Invalid email or password."]'
            return httpx.Response(200, json={"type": "failure", "status": 401, "data": data})
        self.logins += 1
        token = f"token{self.logins}"
        self.sessions.add(token)
        return httpx.Response(
            200,
            json={"type": "redirect", "status": 303, "location": "/meters"},
            headers={"set-cookie": f"{COOKIE}={token}; Max-Age=3600; Path=/; HttpOnly; Secure; SameSite=Lax"},
        )

    def _export(self, request: httpx.Request) -> httpx.Response:
        ts = request.headers.get("x-timestamp", "0")
        message = "\n".join(["GET", "/portal/export", request.url.query.decode(), ts]).encode()
        expected = hmac.new(SECRET.encode(), message, hashlib.sha256).hexdigest()
        if self.fail_export or abs(int(ts) - time.time()) > 300 or request.headers.get("x-signature") != expected:
            return httpx.Response(401, json={"error": "signature_invalid", "message": "Missing or invalid request signature."})
        return httpx.Response(200, json={"data": self.export, "total": len(self.export)})
