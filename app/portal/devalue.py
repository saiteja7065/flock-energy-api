"""Decoder for SvelteKit's `__data.json` payloads.

SvelteKit serialises page data with `devalue`: every value is stored once in a
flat array and containers refer to other values by index. For example

    [{"meterId": 1, "tags": 2}, "J100000", [3], "legacy"]

decodes to {"meterId": "J100000", "tags": ["legacy"]}. Negative indices encode
special constants, and arrays starting with a string are typed values
(["Date", "..."], ["Set", ...] ...). Only what the portal uses (plus a few cheap
extras) is supported.
"""

from datetime import datetime
from typing import Any

from app.errors import NotFound, PortalError

_SPECIALS = {-1: None, -2: None, -3: float("nan"), -4: float("inf"), -5: float("-inf"), -6: -0.0}


class SessionExpired(Exception):
    """The payload is SvelteKit's 'redirect to /login' response."""


def unflatten(values: Any) -> Any:
    if isinstance(values, int):
        return _SPECIALS[values]
    if not isinstance(values, list) or not values:
        raise ValueError("devalue payload must be a non-empty list")

    hydrated: dict[int, Any] = {}

    def hydrate(index: int) -> Any:
        if index in _SPECIALS:
            return _SPECIALS[index]
        if index in hydrated:
            return hydrated[index]

        value = values[index]
        if isinstance(value, dict):
            obj: dict[str, Any] = {}
            hydrated[index] = obj  # register before recursing: devalue allows cycles
            for key, ref in value.items():
                obj[key] = hydrate(ref)
            return obj
        if isinstance(value, list):
            if value and isinstance(value[0], str):
                result = _typed(value, hydrate)
            else:
                result = []
                hydrated[index] = result
                result.extend(hydrate(ref) for ref in value)
                return result
            hydrated[index] = result
            return result

        hydrated[index] = value  # primitive
        return value

    return hydrate(0)


def _typed(value: list, hydrate) -> Any:
    tag, args = value[0], value[1:]
    if tag == "Date":
        return datetime.fromisoformat(args[0].replace("Z", "+00:00"))
    if tag == "Set":
        return [hydrate(ref) for ref in args]
    if tag == "Map":
        return {hydrate(args[i]): hydrate(args[i + 1]) for i in range(0, len(args), 2)}
    if tag == "BigInt":
        return int(args[0])
    if tag == "null":  # object with a null prototype: ["null", key, ref, key, ref...]
        return {args[i]: hydrate(args[i + 1]) for i in range(0, len(args), 2)}
    raise ValueError(f"unsupported devalue type {tag!r}")


def page_data(payload: dict) -> Any:
    """Return the decoded data of the page node (the last node) of a `__data.json` response."""
    kind = payload.get("type")
    if kind == "redirect":
        if str(payload.get("location", "")).startswith("/login"):
            raise SessionExpired()
        raise PortalError(f"unexpected portal redirect to {payload.get('location')}")
    if kind != "data" or not payload.get("nodes"):
        raise PortalError("unrecognised __data.json payload")

    node = payload["nodes"][-1]
    if node is None:
        raise PortalError("page node missing from __data.json payload")
    if node.get("type") == "error":
        status = node.get("status")
        message = (node.get("error") or {}).get("message", "portal error")
        if status == 404:
            raise NotFound(message)
        raise PortalError(f"portal page error {status}: {message}")
    return unflatten(node["data"])
