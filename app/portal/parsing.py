"""Normalisation of raw portal payloads into this service's own shapes.

Portal quirks handled here (see PROTOCOL.md for how they were found):
- numbers arrive as strings ("48438.74"), blanks as "";
- timestamps are "DD/MM/YYYY HH:mm" with no timezone (we assume IST);
- meter detail comes in two layouts depending on the meter's record format
  ("legacy" parameter list vs "v2" JSON-encoded string with PascalCase keys);
- hierarchy levels on the detail page are "Name (CODE)" strings.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30), "IST")
LEVELS = ("zone", "circle", "division", "subdivision", "substation", "feeder", "dt")

_DETAIL_PAGE_LEVELS = {
    "Zone": "zone",
    "Circle": "circle",
    "Division": "division",
    "Subdivision": "subdivision",
    "Sub Station": "substation",
    "Feeder": "feeder",
    "DT": "dt",
}

# Both detail layouts, mapped onto one vocabulary.
_DETAIL_FIELDS = {
    "Meter ID": "meter_id",
    "MeterId": "meter_id",
    "Serial No": "serial_no",
    "SerialNo": "serial_no",
    "Make": "make",
    "Phase Type": "phase",
    "PhaseType": "phase",
    "Installation Status": "status",
    "InstallationStatus": "status",
    "Installation Type": "install_type",
    "InstallationType": "install_type",
}

_LABELLED = re.compile(r"^(?P<name>.*?)\s*\((?P<code>[^()]*)\)\s*$")


def slug(value: str | None) -> str | None:
    """'CT Operated' -> 'ct_operated'. Enumerations are open-ended on purpose:
    an unexpected portal value passes through instead of failing the request."""
    if value is None or not str(value).strip():
        return None
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def parse_labelled(value: str | None) -> dict:
    """'Malviya Nagar DT 1 (DT-001)' -> {'name': 'Malviya Nagar DT 1', 'code': 'DT-001'}."""
    value = (value or "").strip()
    match = _LABELLED.match(value)
    if match:
        return {"name": match["name"].strip(), "code": match["code"].strip()}
    return {"name": value, "code": ""}


def parse_portal_timestamp(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%d/%m/%Y %H:%M").replace(tzinfo=IST)


# ---------------------------------------------------------------- meters


def meter_from_export(row: dict) -> dict:
    """One row of /portal/export -> meter record (network still unrepaired)."""
    geo = row.get("geo") or {}
    hierarchy = row.get("hierarchy") or {}
    return {
        "meter_id": row["meterId"],
        "serial_no": row.get("serialNo") or None,
        "make": row.get("make") or None,
        "phase": slug(row.get("phaseType")),
        "status": slug(row.get("installStatus")),
        "install_type": slug(row.get("installType")),
        "record_format": row.get("build") or None,
        "dt_code": row.get("dtCode") or None,
        "location": _location(geo.get("lat"), geo.get("lng")),
        "network": {
            level: {
                "code": ((hierarchy.get(level) or {}).get("code") or "").strip(),
                "name": ((hierarchy.get(level) or {}).get("name") or "").strip(),
            }
            for level in LEVELS
        },
    }


def meter_from_detail_page(page: dict, geo: dict | None) -> dict:
    """Decoded /meters/{id}/__data.json (+ /geo) -> meter record. Used when the export is unavailable."""
    detail = page.get("detail") or {}
    if "classData" in detail:  # "v2" record format
        fields = (json.loads(detail["classData"]) or {}).get("installed_meter") or {}
        record_format = "v2"
    else:  # "legacy" record format
        fields = {p["parameterName"]: p["parameterValue"] for p in detail.get("data", [])}
        record_format = "legacy"

    values = {_DETAIL_FIELDS[k]: v for k, v in fields.items() if k in _DETAIL_FIELDS}
    hierarchy = page.get("hierarchy") or {}
    network = {level: parse_labelled(hierarchy.get(label)) for label, level in _DETAIL_PAGE_LEVELS.items()}
    geo = geo or {}
    return {
        "meter_id": values.get("meter_id") or page.get("meterId"),
        "serial_no": values.get("serial_no") or None,
        "make": values.get("make") or None,
        "phase": slug(values.get("phase")),
        "status": slug(values.get("status")),
        "install_type": slug(values.get("install_type")),
        "record_format": record_format,
        "dt_code": network["dt"]["code"] or None,
        "location": _location(geo.get("latitude"), geo.get("longitude")),
        "network": network,
    }


def _location(lat: Any, lng: Any) -> dict | None:
    lat, lng = to_float(lat), to_float(lng)
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return {"lat": lat, "lng": lng}


# ----------------------------------------------------------- consumption


def normalise_energy(rows: list[dict]) -> tuple[list[dict], list[dict], int | None]:
    """Portal energy rows -> (readings, data-quality issues, interval in minutes).

    kWh/kVAh are cumulative register values, so consumption per interval is the
    difference to the previous reading.
    """
    issues: list[dict] = []
    by_time: dict[datetime, dict] = {}

    for row in rows:
        try:
            ts = parse_portal_timestamp(row.get("timestamp", ""))
        except ValueError:
            issues.append({"type": "unparseable_timestamp", "timestamp": None, "detail": repr(row.get("timestamp"))})
            continue
        reading = {
            "timestamp": ts,
            "kwh_register": to_float(row.get("kwh")),
            "kvah_register": to_float(row.get("kvah")),
            "voltage_r": to_float(row.get("voltR")),
        }
        missing = [k for k in ("kwh_register", "kvah_register", "voltage_r") if reading[k] is None]
        if ts in by_time:
            kept = max(by_time[ts], reading, key=_completeness)
            issues.append({"type": "duplicate_timestamp", "timestamp": ts, "detail": "kept the most complete row"})
            by_time[ts] = kept
            continue
        if missing:
            issues.append({"type": "missing_values", "timestamp": ts, "detail": ", ".join(missing)})
        by_time[ts] = reading

    readings = [by_time[ts] for ts in sorted(by_time)]
    previous = None
    for reading in readings:
        reading["kwh_delta"] = None
        current = reading["kwh_register"]
        if current is None:
            continue
        if previous is not None:
            delta = round(current - previous, 4)
            if delta < 0:
                issues.append({"type": "register_decreased", "timestamp": reading["timestamp"], "detail": f"{previous} -> {current}"})
            else:
                reading["kwh_delta"] = delta
        previous = current

    return readings, issues, _interval_minutes(readings)


def _completeness(reading: dict) -> int:
    return sum(reading[k] is not None for k in ("kwh_register", "kvah_register", "voltage_r"))


def _interval_minutes(readings: list[dict]) -> int | None:
    gaps = [(b["timestamp"] - a["timestamp"]).total_seconds() / 60 for a, b in zip(readings, readings[1:])]
    return int(median(gaps)) if gaps else None
