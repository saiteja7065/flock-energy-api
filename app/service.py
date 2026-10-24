"""Use cases behind the API routes: combines the catalogue and live portal calls."""

import logging
import time
from datetime import date, datetime, timezone
from statistics import mean

from app.catalog import Catalog
from app.errors import InvalidRequest, NotFound, PortalError
from app.portal.client import PortalClient
from app.portal.parsing import meter_from_detail_page, normalise_energy

log = logging.getLogger(__name__)

MAX_CACHED_CONSUMPTION = 500


class MeterService:
    def __init__(self, client: PortalClient, catalog: Catalog, consumption_ttl_s: int, clock=time.monotonic):
        self.client = client
        self.catalog = catalog
        self._consumption_ttl_s = consumption_ttl_s
        self._clock = clock
        self._consumption_cache: dict[tuple, tuple[float, dict]] = {}

    async def get_meter(self, meter_id: str) -> dict:
        try:
            snapshot = await self.catalog.get()
        except PortalError:
            log.warning("catalogue unavailable; fetching meter %s live", meter_id)
            return await self._get_meter_live(meter_id)
        meter = snapshot.meters.get(meter_id)
        if meter is None:
            raise NotFound(f"meter {meter_id} not found")
        return {**meter, "source": "snapshot", "as_of": snapshot.fetched_at}

    async def _get_meter_live(self, meter_id: str) -> dict:
        page = await self.client.get_meter_page(meter_id)
        geo = await self.client.get_geo(meter_id)
        meter = meter_from_detail_page(page, geo)
        # A single meter can't be repaired against its DT's majority, so node ids are omitted.
        for ref in meter["network"].values():
            ref["node_id"] = None
        return {**meter, "data_quality": [], "source": "live", "as_of": datetime.now(timezone.utc)}

    async def get_consumption(self, meter_id: str, date_from: date | None, date_to: date | None) -> dict:
        if date_from and date_to and date_from > date_to:
            raise InvalidRequest("'from' must be on or before 'to'")

        key = (meter_id, date_from, date_to)
        cached = self._consumption_cache.get(key)
        if cached and self._clock() < cached[0]:
            return cached[1]

        rows = await self.client.get_energy(
            meter_id, date_from.isoformat() if date_from else None, date_to.isoformat() if date_to else None
        )
        readings, issues, interval = normalise_energy(rows)
        result = {
            "meter_id": meter_id,
            "requested_from": date_from,
            "requested_to": date_to,
            "interval_minutes": interval,
            "summary": _summarise(readings),
            "readings": readings,
            "data_quality": issues,
        }

        if len(self._consumption_cache) >= MAX_CACHED_CONSUMPTION:
            self._consumption_cache.pop(next(iter(self._consumption_cache)))  # evict oldest
        self._consumption_cache[key] = (self._clock() + self._consumption_ttl_s, result)
        return result


def _summarise(readings: list[dict]) -> dict:
    deltas = [r for r in readings if r["kwh_delta"] is not None]
    volts = [r["voltage_r"] for r in readings if r["voltage_r"] is not None]
    peak = max(deltas, key=lambda r: r["kwh_delta"], default=None)
    return {
        "readings": len(readings),
        "first_reading_at": readings[0]["timestamp"] if readings else None,
        "last_reading_at": readings[-1]["timestamp"] if readings else None,
        "consumption_kwh": round(sum(r["kwh_delta"] for r in deltas), 3) if deltas else None,
        "avg_voltage_r": round(mean(volts), 1) if volts else None,
        "min_voltage_r": min(volts, default=None),
        "max_voltage_r": max(volts, default=None),
        "peak_interval": {"timestamp": peak["timestamp"], "kwh": peak["kwh_delta"]} if peak else None,
    }
