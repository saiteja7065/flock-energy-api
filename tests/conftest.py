import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.portal.client import PortalClient
from tests.fake_portal import EMAIL, PASSWORD, FakePortal


def make_settings(**overrides) -> Settings:
    return Settings(base_url="https://urja.test", email=EMAIL, password=PASSWORD, **overrides)


def make_client(portal: FakePortal, **overrides) -> PortalClient:
    client = PortalClient(make_settings(**overrides), transport=portal.transport)
    client.retry_delays_s = (0, 0, 0)
    return client


@pytest.fixture
def portal() -> FakePortal:
    return FakePortal()


@pytest.fixture
def api(portal):
    app = create_app(make_settings(), make_client(portal))
    with TestClient(app) as test_client:
        yield test_client
