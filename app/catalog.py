"""In-memory meter catalogue built from the portal's bulk export.

One signed export call returns every meter with its hierarchy and coordinates,
so instead of paging through the portal per request we keep a snapshot and
answer list/filter/geo/hierarchy questions locally.

The hierarchy in the export is messy (see PROTOCOL.md, "Data quality"): a few
meters have blank codes or names at some level, and one DT appears under an
old name. Every DT has a clear majority path though, so each meter's path is
repaired to its DT's majority and the repair is recorded on the meter.

Codes below circle level are reused across parents (D-01 exists under C-01,
C-03 and C-05), so tree nodes are identified by their full code path
("Z-01/C-01/D-01"), not by their code alone.
"""

import asyncio
import logging
import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.errors import PortalError, PortalUnavailable
from app.portal.client import PortalClient
from app.portal.parsing import LEVELS, meter_from_export

log = logging.getLogger(__name__)

FAILED_REFRESH_COOLDOWN_S = 60


@dataclass
class Snapshot:
    meters: dict[str, dict]
    transformers: dict[str, dict]
    tree: list[dict]  # zone nodes
    nodes: dict[str, dict]  # node_id -> node
    fetched_at: datetime
    repairs: Counter = field(default_factory=Counter)


def build_snapshot(export_rows: list[dict], dt_rows: list[dict], fetched_at: datetime | None = None) -> Snapshot:
    meters = {m["meter_id"]: m for m in map(meter_from_export, export_rows)}
    dt_info = {d["code"]: d for d in dt_rows}
    repairs = _repair_networks(meters.values(), dt_info)
    nodes, tree = _build_tree(meters.values())

    transformers = {}
    for code, node in ((n["code"], n) for n in nodes.values() if n["level"] == "dt"):
        info = dt_info.get(code, {})
        transformers[code] = {
            "code": code,
            "name": node["name"],
            "aliases": node["aliases"],
            "capacity_kva": info.get("capacityKva"),
            "feeder_code": info.get("feederCode") or node["id"].split("/")[-2],
            "node_id": node["id"],
            "meter_count": node["meter_count"],
            "status_counts": node["status_counts"],
            "meter_ids": node["meter_ids"],
        }
    return Snapshot(meters, transformers, tree, nodes, fetched_at or datetime.now(timezone.utc), repairs)


def _repair_networks(meters, dt_info: dict) -> Counter:
    by_dt: dict[str, list[dict]] = defaultdict(list)
    for meter in meters:
        by_dt[meter["dt_code"] or meter["network"]["dt"]["code"]].append(meter)

    repairs: Counter = Counter()
    for dt_code, group in by_dt.items():
        canonical, aliases = {}, set()
        for level in LEVELS:
            entries = [m["network"][level] for m in group]
            code = _majority(e["code"] for e in entries)
            names = Counter(e["name"] for e in entries if e["name"] and e["code"] in (code, ""))
            name = names.most_common(1)[0][0] if names else ""
            if level == "dt":
                # The transformer list is the authoritative source for DT names.
                name = (dt_info.get(dt_code) or {}).get("name") or name
                aliases = {n for n in names if n != name}
            canonical[level] = {"code": code, "name": name}

        for meter in group:
            meter["data_quality"] = []
            for level in LEVELS:
                original, fixed = meter["network"][level], canonical[level]
                for attr in ("code", "name"):
                    if original[attr] != fixed[attr]:
                        issue = "blank" if not original[attr] else "conflict"
                        meter["data_quality"].append(
                            {"field": f"network.{level}.{attr}", "issue": issue, "original": original[attr], "resolved_to": fixed[attr]}
                        )
                        repairs[f"{issue}_{attr}"] += 1
            path = []
            meter["network"] = {}
            for level in LEVELS:
                path.append(canonical[level]["code"])
                meter["network"][level] = {**canonical[level], "node_id": "/".join(path)}
            meter["dt_code"] = canonical["dt"]["code"]
            meter["_dt_aliases"] = sorted(aliases)
    return repairs


def _majority(values) -> str:
    counts = Counter(v for v in values if v)
    return counts.most_common(1)[0][0] if counts else ""


def _build_tree(meters) -> tuple[dict[str, dict], list[dict]]:
    nodes: dict[str, dict] = {}
    roots: list[dict] = []
    for meter in sorted(meters, key=lambda m: m["meter_id"]):
        parent = None
        for level in LEVELS:
            ref = meter["network"][level]
            node = nodes.get(ref["node_id"])
            if node is None:
                node = nodes[ref["node_id"]] = {
                    "id": ref["node_id"],
                    "level": level,
                    "code": ref["code"],
                    "name": ref["name"],
                    "meter_count": 0,
                    "status_counts": Counter(),
                    "children": [],
                }
                if level == "dt":
                    node["aliases"] = meter["_dt_aliases"]
                    node["meter_ids"] = []
                (parent["children"] if parent else roots).append(node)
            node["meter_count"] += 1
            node["status_counts"][meter["status"] or "unknown"] += 1
            parent = node
        parent["meter_ids"].append(meter["meter_id"])
        del meter["_dt_aliases"]

    for node in nodes.values():
        node["status_counts"] = dict(node["status_counts"])
        node["children"].sort(key=lambda n: n["code"])
    roots.sort(key=lambda n: n["code"])
    return nodes, roots


# ------------------------------------------------------------------ queries


def haversine_km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    a = math.sin(math.radians(b_lat - a_lat) / 2) ** 2 + math.cos(math.radians(a_lat)) * math.cos(
        math.radians(b_lat)
    ) * math.sin(math.radians(b_lng - a_lng) / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(a))


def query_meters(
    snapshot: Snapshot,
    *,
    q: str | None = None,
    status: str | None = None,
    make: str | None = None,
    phase: str | None = None,
    install_type: str | None = None,
    record_format: str | None = None,
    dt: str | None = None,
    node: str | None = None,
    near: tuple[float, float, float] | None = None,  # (lat, lng, radius_km)
) -> list[tuple[dict, float | None]]:
    """Linear scan over the snapshot; fine for thousands of meters (see README on scale)."""
    q = q.lower() if q else None
    results = []
    for meter in snapshot.meters.values():
        if q and q not in meter["meter_id"].lower() and q not in (meter["serial_no"] or "").lower():
            continue
        if status and meter["status"] != status:
            continue
        if make and (meter["make"] or "").lower() != make.lower():
            continue
        if phase and meter["phase"] != phase:
            continue
        if install_type and meter["install_type"] != install_type:
            continue
        if record_format and meter["record_format"] != record_format:
            continue
        if dt and meter["dt_code"] != dt:
            continue
        if node:
            dt_node = meter["network"]["dt"]["node_id"]
            if dt_node != node and not dt_node.startswith(node + "/"):
                continue
        distance = None
        if near:
            if not meter["location"]:
                continue
            distance = haversine_km(near[0], near[1], meter["location"]["lat"], meter["location"]["lng"])
            if distance > near[2]:
                continue
        results.append((meter, distance))

    if near:
        results.sort(key=lambda r: r[1])
    else:
        results.sort(key=lambda r: r[0]["meter_id"])
    return results


# ---------------------------------------------------------------- freshness


class Catalog:
    """Holds the current snapshot and decides when to refresh it.

    Policy: refresh when older than `ttl_s`. If a refresh fails we keep serving
    the previous snapshot (flagged stale) and don't retry for a minute, so a
    portal outage doesn't turn every API call into a slow failure.
    """

    def __init__(self, client: PortalClient, ttl_s: int, clock=time.monotonic):
        self._client = client
        self._ttl_s = ttl_s
        self._clock = clock
        self._snapshot: Snapshot | None = None
        self._loaded_at: float | None = None
        self._failed_at: float | None = None
        self._last_error: str | None = None
        self._lock = asyncio.Lock()

    def _is_fresh(self) -> bool:
        return self._loaded_at is not None and self._clock() - self._loaded_at < self._ttl_s

    def _cooling_down(self) -> bool:
        return self._failed_at is not None and self._clock() - self._failed_at < FAILED_REFRESH_COOLDOWN_S

    async def get(self) -> Snapshot:
        async with self._lock:
            if self._snapshot and (self._is_fresh() or self._cooling_down()):
                return self._snapshot
            if self._snapshot is None and self._cooling_down():
                raise PortalUnavailable(f"catalogue unavailable ({self._last_error})", retry_after=FAILED_REFRESH_COOLDOWN_S)
            try:
                return await self._load()
            except PortalError:
                if self._snapshot is None:
                    raise
                log.exception("snapshot refresh failed; serving stale snapshot")
                return self._snapshot

    async def refresh(self) -> Snapshot:
        async with self._lock:
            return await self._load()

    async def _load(self) -> Snapshot:
        try:
            export_rows = await self._client.export_meters()
            dt_rows = await self._client.list_transformers()
        except PortalError as exc:
            self._failed_at, self._last_error = self._clock(), f"{exc.code}: {exc.message}"
            raise
        self._snapshot = build_snapshot(export_rows, dt_rows)
        self._loaded_at, self._failed_at, self._last_error = self._clock(), None, None
        log.info("snapshot loaded: %d meters, repairs=%s", len(self._snapshot.meters), dict(self._snapshot.repairs))
        return self._snapshot

    def status(self) -> dict:
        snap = self._snapshot
        return {
            "loaded": snap is not None,
            "fetched_at": snap.fetched_at if snap else None,
            "age_s": round(self._clock() - self._loaded_at) if self._loaded_at is not None else None,
            "ttl_s": self._ttl_s,
            "stale": snap is not None and not self._is_fresh(),
            "meter_count": len(snap.meters) if snap else 0,
            "transformer_count": len(snap.transformers) if snap else 0,
            "repairs": dict(snap.repairs) if snap else {},
            "last_error": self._last_error,
        }
