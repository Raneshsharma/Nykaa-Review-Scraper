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
    assert result.summary.total_available == len(mock.reviews)


def test_small_product_collects_everything_in_one_pass(mock_api):
    mock = mock_api(n_reviews=150)
    result = fast_scraper().scrape(URL)
    assert_complete(result, mock)
    assert result.summary.warning == ""
    assert mock.requests <= 8  # 8 pages, nothing wasted on other sort orders


def test_reviews_are_parsed_defensively(mock_api):
    mock_api(n_reviews=60)
    result = fast_scraper().scrape("123456")
    by_id = {r.id: r for r in result.reviews}
    r0 = by_id["100000"]   # rating "5.0"-style string, likeCount None, name None
    assert r0.rating in (1, 2, 3, 4, 5)
    assert r0.like_count == 0
    assert r0.reviewer_name == "Anonymous"
    assert r0.child_id == "1001"
    assert "<br" not in r0.description and "\x0b" not in r0.description
    assert "line two & more" in r0.description


def test_hidden_reviews_are_explained_not_chased(mock_api):
    # Nykaa counts reviews it never serves; the scraper must not burn requests hunting them
    mock = mock_api(n_reviews=400, hidden_fraction=0.1)
    result = fast_scraper().scrape(URL)
    assert {r.id for r in result.reviews} == mock.ids
    assert result.summary.total_available == 400 > len(result.reviews)
    assert "hidden on Nykaa's side" in result.summary.warning
    assert mock.requests <= 21


def test_page_ceiling_is_beaten_by_star_split(mock_api):
    # 400 reviews but only 5 pages x 20 = 100 reachable per query; each star bucket fits
    mock = mock_api(n_reviews=400, ceiling=5, star_weights=(0.2, 0.2, 0.2, 0.2, 0.2))
    result = fast_scraper().scrape(URL)
    assert_complete(result, mock)


def test_overflowing_star_is_split_by_variant(mock_api):
    # 5-star bucket (~800) is far beyond one query (100); variants are discovered from reviews
    variants = [str(2000 + i) for i in range(12)]
    mock = mock_api(n_reviews=1000, ceiling=5, star_weights=(0.8, 0.1, 0.05, 0.03, 0.02),
                    variants=variants, hidden_fraction=0.05)
    result = fast_scraper().scrape(URL)
    assert {r.id for r in result.reviews} == mock.ids
    assert "hidden" in result.summary.warning


def test_unsplittable_bucket_reports_page_limit(mock_api):
    # One variant, 5-star bucket of ~500 with a 100-review reach: cannot be fully collected
    mock = mock_api(n_reviews=600, ceiling=5, star_weights=(0.85, 0.1, 0.05, 0, 0))
    result = fast_scraper().scrape(URL)
    assert len(result.reviews) < len(mock.ids)
    assert "page limit" in result.summary.warning


def test_survives_rate_limits_errors_and_challenges(mock_api):
    mock = mock_api(n_reviews=400, rate_limit_every=9, fail_every=17, challenge_every=29)
    result = fast_scraper().scrape(URL)
    assert_complete(result, mock)


def test_limit_sort_and_filter(mock_api):
    mock = mock_api(n_reviews=300)
    limited = fast_scraper().scrape(URL, max_reviews=45, sort="MOST_RECENT")
    assert len(limited.reviews) == 45
    newest = sorted(mock.reviews, key=lambda r: -r["_ts"])[:45]
    assert {r.id for r in limited.reviews} == {str(r["id"]) for r in newest}
    assert [r.created_on for r in limited.reviews] == sorted((r.created_on for r in limited.reviews), reverse=True)

    two_star = fast_scraper().scrape(URL, filter_by="STAR_2")
    expected = {str(r["id"]) for r in mock.reviews if mock.rating_of(r) == 2}
    assert {r.id for r in two_star.reviews} == expected

    negative = fast_scraper().scrape(URL, sort="NEGATIVE_FIRST")
    assert [r.rating for r in negative.reviews] == sorted(r.rating for r in negative.reviews)


def test_cancel_returns_partial_results(mock_api):
    mock_api(n_reviews=3000, ceiling=40, variants=[str(3000 + i) for i in range(5)])
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    start = time.time()
    result = fast_scraper().scrape(URL, cancel_event=cancel)
    assert time.time() - start < 15
    assert 0 < len(result.reviews) < 3000
    assert result.summary.warning == "Stopped early by user."


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


def test_unreachable_site_raises_clear_error(monkeypatch):
    from nykaa_scraper import NykaaScraper
    monkeypatch.setattr(NykaaScraper, "BASE_URL", "http://127.0.0.1:9")
    scraper = NykaaScraper(max_retries=1, timeout=2)
    with pytest.raises(RuntimeError, match="refused the connection"):
        scraper.scrape("123456")


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
