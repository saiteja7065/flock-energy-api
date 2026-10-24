import asyncio

import pytest

from app.errors import NotFound, PortalAuthError, PortalRateLimited
from app.portal.client import sign
from tests.conftest import make_client
from tests.fake_portal import FakePortal


def run(coro):
    return asyncio.run(coro)


def test_sign_matches_portal_algorithm():
    # Expected value computed by running the portal's own signer (Web Crypto HMAC-SHA256) in Node.
    assert sign("secret", "GET", "/portal/export", "page=1", "1700000000") == (
        "81340cd1e31e597cb066514f51f103416827265923e80f700aefb1b64dc6a3b9"
    )


def test_wrong_password_is_detected_despite_http_200():
    portal = FakePortal()
    client = make_client(portal)
    client._settings.password = "wrong"
    with pytest.raises(PortalAuthError):
        run(client.login())


def test_relogs_in_when_portal_api_session_expires():
    portal = FakePortal()
    client = make_client(portal)

    async def scenario():
        await client.get_geo("J100000")
        portal.expire_sessions()
        return await client.get_geo("J100000")

    assert run(scenario())["latitude"] == "26.9"
    assert portal.logins == 2


def test_relogs_in_when_page_data_redirects_to_login():
    portal = FakePortal()
    client = make_client(portal)

    async def scenario():
        await client.get_geo("J100000")
        portal.expire_sessions()
        return await client.get_meter_page("J100000")

    assert run(scenario())["meterId"] == "J100000"
    assert portal.logins == 2


def test_concurrent_requests_after_expiry_trigger_a_single_login():
    portal = FakePortal()
    client = make_client(portal)

    async def scenario():
        await client.get_geo("J100000")
        portal.expire_sessions()
        await asyncio.gather(*(client.get_geo("J100000") for _ in range(5)))

    run(scenario())
    assert portal.logins == 2


def test_retries_rate_limited_requests():
    portal = FakePortal()
    client = make_client(portal)
    run(client.login())
    portal.rate_limit_next = 2
    assert run(client.get_geo("J100000"))
    portal.rate_limit_next = 10
    with pytest.raises(PortalRateLimited):
        run(client.get_geo("J100000"))


def test_signed_export_returns_all_rows():
    portal = FakePortal()
    rows = run(make_client(portal).export_meters())
    assert len(rows) == len(portal.export)


def test_unknown_meter_is_not_found():
    portal = FakePortal()
    client = make_client(portal)
    with pytest.raises(NotFound):
        run(client.get_energy("J999999"))
    with pytest.raises(NotFound):
        run(client.get_meter_page("J999999"))
