import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# The mock server is local; never send it through an HTTP proxy
for var in ("NO_PROXY", "no_proxy"):
    os.environ[var] = ",".join(filter(None, [os.environ.get(var), "127.0.0.1", "localhost"]))

from mock_nykaa import MockNykaa  # noqa: E402
from nykaa_scraper import NykaaScraper  # noqa: E402


@pytest.fixture
def mock_api(monkeypatch):
    """Factory: start a mock API with the given behaviour and point the scraper at it."""
    servers = []

    def start(**kwargs):
        mock = MockNykaa(**kwargs).start()
        servers.append(mock)
        monkeypatch.setattr(NykaaScraper, "BASE_URL", mock.base_url)
        monkeypatch.setattr(
            NykaaScraper, "GATEWAY_REVIEWS_URL", mock.base_url + "/gateway-api/products/{product_id}/reviews"
        )
        return mock

    yield start
    for s in servers:
        s.stop()


def fast_scraper(**kwargs):
    kwargs.setdefault("delay_range", (0.0, 0.01))
    return NykaaScraper(**kwargs)
