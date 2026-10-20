import threading
import time

import pytest

from conftest import fast_scraper
from nykaa_scraper.utils import extract_product_id, clean_text, safe_int

URL = "https://www.nykaa.com/mock-serum/p/123456?skuId=1"


def assert_complete(result, mock):
    ids = [r.id for r in result.reviews]
    assert len(ids) == len(set(ids)), "duplicate reviews returned"
    assert set(ids) == mock.ids
    assert result.summary.total_scraped == len(mock.reviews)
    assert result.summary.total_available == len(mock.reviews)


def test_small_product_collects_everything(mock_api):
    mock = mock_api(n_reviews=150)
    result = fast_scraper().scrape(URL)
    assert_complete(result, mock)
    # One partition is enough when everything fits under the page ceiling
    assert mock.requests < 15


def test_reviews_are_parsed_defensively(mock_api):
    mock_api(n_reviews=60)
    result = fast_scraper().scrape("123456")
    by_id = {r.id: r for r in result.reviews}
    r0 = by_id["100000"]   # rating "5.0"-style string, likeCount None, name None
    assert r0.rating in (1, 2, 3, 4, 5)
    assert r0.like_count == 0
    assert r0.reviewer_name == "Anonymous"
    assert "<br" not in r0.description and "\x0b" not in r0.description
    assert "line two & more" in r0.description


def test_page_ceiling_is_beaten_with_partitions(mock_api):
    # 400 reviews but only 5 pages x 20 = 100 reachable per query; each star bucket fits
    mock = mock_api(n_reviews=400, ceiling=5, star_weights=(0.2, 0.2, 0.2, 0.2, 0.2))
    result = fast_scraper().scrape(URL)
    assert_complete(result, mock)


def test_bigger_page_size_is_detected_and_used(mock_api):
    # 5 pages x 100 = 500 per query once the bigger page size is detected (vs 100 without)
    mock = mock_api(n_reviews=1500, ceiling=5, page_size_param="pageSize", star_weights=(0.2, 0.2, 0.2, 0.2, 0.2))
    scraper = fast_scraper()
    result = scraper.scrape(URL)
    assert scraper._extra_params == {"pageSize": "100"}
    assert_complete(result, mock)


def test_unsupported_page_size_is_not_used(mock_api):
    mock_api(n_reviews=300)
    scraper = fast_scraper()
    scraper.scrape(URL)
    assert scraper._extra_params == {}


def test_combined_filters_reach_overflowing_star_bucket(mock_api):
    # 5-star bucket alone is far bigger than every sort order can reach
    kwargs = dict(n_reviews=2000, ceiling=5, star_weights=(0.8, 0.1, 0.05, 0.03, 0.02))
    mock = mock_api(combined_filters=True, **kwargs)
    with_combo = fast_scraper().scrape(URL)
    # Combined filters must add reviews beyond what single filters reach
    mock2 = mock_api(combined_filters=False, **kwargs)
    plain = fast_scraper().scrape(URL)
    assert len(with_combo.reviews) > len(plain.reviews)
    assert {r.id for r in with_combo.reviews} <= mock.ids
    assert {r.id for r in plain.reviews} <= mock2.ids
    # When reviews are unreachable the result says so instead of pretending to be complete
    assert plain.summary.total_available == 2000 > plain.summary.total_scraped


def test_survives_rate_limits_errors_and_challenges(mock_api):
    mock = mock_api(n_reviews=400, rate_limit_every=23, fail_every=17, challenge_every=29)
    result = fast_scraper().scrape(URL)
    assert_complete(result, mock)


def test_limit_and_filter(mock_api):
    mock = mock_api(n_reviews=300)
    assert len(fast_scraper().scrape(URL, max_reviews=45).reviews) == 45
    two_star = fast_scraper().scrape(URL, filter_by="STAR_2")
    expected = {str(r["id"]) for r in mock.reviews if int(float(r["rating"])) == 2}
    assert {r.id for r in two_star.reviews} == expected


def test_cancel_returns_partial_results(mock_api):
    mock_api(n_reviews=3000, ceiling=40)
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    start = time.time()
    result = fast_scraper().scrape(URL, cancel_event=cancel)
    assert time.time() - start < 10
    assert 0 < len(result.reviews) < 3000


def test_unreachable_api_raises_clear_error(monkeypatch):
    from nykaa_scraper import NykaaScraper
    monkeypatch.setattr(NykaaScraper, "BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(NykaaScraper, "GATEWAY_REVIEWS_URL", "http://127.0.0.1:9/gateway-api/products/{product_id}/reviews")
    scraper = NykaaScraper(max_retries=1, timeout=2)
    with pytest.raises(RuntimeError, match="could not be reached"):
        scraper.scrape("123456")


@pytest.mark.parametrize("target,expected", [
    ("27905374", "27905374"),
    ("https://www.nykaa.com/plum-x/p/27905374?skuId=1", "27905374"),
    ("www.nykaa.com/plum-x/p/27905374", "27905374"),
    ("nykaa.com/a/p/509378?productId=22050679&skuId=1", "22050679"),
    ("https://nykaa.app.link/AbC123", None),
    ("Kay Beauty Lipstick", None),
])
def test_extract_product_id(target, expected):
    assert extract_product_id(target)[0] == expected


def test_text_helpers():
    assert clean_text('"Great&amp;nice<br>ok\x0b"') == "Great&nice\nok"
    assert clean_text(None) == ""
    assert safe_int("4.0") == 4
    assert safe_int(None) == 0
    assert safe_int("x", 3) == 3


def test_cancel_before_crawl_raises_cancelled(mock_api):
    from nykaa_scraper import ScrapeCancelled
    mock_api(n_reviews=100)
    cancel = threading.Event()
    cancel.set()
    scraper = fast_scraper()
    with pytest.raises(ScrapeCancelled):
        scraper.scrape(URL, cancel_event=cancel)
    assert scraper._cancel_event is None


def test_circuit_breaker_stops_when_blocked(mock_api):
    # Probe succeeds, then every request fails: must give up quickly with a warning
    mock = mock_api(n_reviews=2000)
    real_handle = mock.handle

    def handle(path, query):
        if "/reviews" in path and mock.requests > 3:
            with mock.lock:
                mock.requests += 1
            return 500, "text/plain", "blocked"
        return real_handle(path, query)

    mock.handle = handle
    start = time.time()
    result = fast_scraper(max_retries=1).scrape(URL)
    assert time.time() - start < 30
    assert "refusing" in result.summary.warning
    assert 0 < len(result.reviews) < 2000


def test_progress_respects_limit(mock_api):
    mock_api(n_reviews=500)
    seen = []
    fast_scraper().scrape(URL, max_reviews=30, progress_callback=lambda c, t, p: seen.append((c, t)))
    assert seen and all(c <= 30 and t == 30 for c, t in seen)


def test_search_query_is_url_encoded(mock_api, monkeypatch):
    mock_api(n_reviews=10)
    scraper = fast_scraper()
    scraper._ensure_session()
    urls = []

    class Resp:
        status_code = 200
        text = ""

    monkeypatch.setattr(scraper.session, "get", lambda url, **kw: urls.append(url) or Resp())
    scraper.search_products("Dot & Key serum")
    assert urls[0].endswith("q=Dot+%26+Key+serum")
