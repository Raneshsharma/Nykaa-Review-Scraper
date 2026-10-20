"""
Core Nykaa Review Scraper.

How Nykaa's reviews API behaves (verified against the live site):
  * GET /gateway-api/products/{id}/reviews?pageNo=&sort=&filters=&childIds=&domain=nykaa
  * Pages hold 20 reviews (MOST_RECENT pages are often shorter) and stop at page 40,
    so one query can never reach more than ~800 reviews.
  * Only verified-buyer reviews are served; reviewCount is the number of those.
  * filters=STAR_1..STAR_5 and childIds=<variant id> narrow a query and report their
    own reviewCount; the two can be combined.
  * Requests are randomly rate limited (HTTP 429, ~10%) and clear within a second or
    two; Akamai blocks outdated browser fingerprints with HTTP 403.

To collect every review the scraper splits the product into "buckets" small enough to
crawl completely: the whole product, then each star rating, then each star rating of
each variant (shade/size). A bucket's reported count tells us when it is complete,
so no request is wasted on buckets that are already fully collected.
"""

import re
import json
import math
import time
import random
import threading
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional, List, Dict, Any, Callable, Tuple, Set
from urllib.parse import quote_plus
from curl_cffi import requests

from .models import Review, ProductInfo, RatingSummary, ScrapeResult
from .utils import extract_product_id, clean_text, safe_int, logger


# Page fetch outcomes
PAGE_OK = "ok"        # request succeeded (the page may still hold zero reviews)
PAGE_END = "end"      # beyond the last page Nykaa will serve (HTTP 400)
PAGE_ERROR = "error"  # request failed after all retries; page must be retried later

STAR_FILTERS = ["STAR_5", "STAR_4", "STAR_3", "STAR_2", "STAR_1"]
# Crawl order: sorts with full 20-review pages first; MOST_RECENT pages are often shorter
CRAWL_SORTS = ["MOST_USEFUL", "MOST_HELPFUL", "POSITIVE_FIRST", "NEGATIVE_FIRST", "MOST_RECENT"]
# Within one star rating POSITIVE_FIRST / NEGATIVE_FIRST return the MOST_USEFUL order
STAR_CRAWL_SORTS = ["MOST_USEFUL", "MOST_HELPFUL", "MOST_RECENT"]
ALL_SORTS = ["MOST_RECENT", "MOST_HELPFUL", "MOST_USEFUL", "POSITIVE_FIRST", "NEGATIVE_FIRST"]

API_PAGE_SIZE = 20
PAGE_CEILING = 40
QUERY_REACH = API_PAGE_SIZE * PAGE_CEILING  # most reviews one (filter, sort) query can return

# Browser fingerprints to try, newest first. Akamai blocks outdated ones (e.g. chrome120).
IMPERSONATE_CANDIDATES = ["chrome", "safari", "edge101", "chrome131", "safari17_0", "chrome124"]

# Stop hammering Nykaa once this many page requests in a row have failed every retry
MAX_CONSECUTIVE_FAILED_PAGES = 12


class ScrapeCancelled(Exception):
    """Raised when a caller cancels a scrape before any reviews could be collected."""


class _Blocked(Exception):
    """Internal: Nykaa keeps refusing requests; stop and return what we have."""


@dataclass
class PageResult:
    status: str
    reviews: List[Review] = field(default_factory=list)
    total_count: int = 0
    page_no: int = 0


@dataclass(frozen=True)
class Bucket:
    """A slice of a product's reviews: optionally one variant and/or one star rating."""
    child: Optional[str] = None
    star: Optional[int] = None

    def params(self) -> Dict[str, str]:
        p: Dict[str, str] = {}
        if self.star:
            p["filters"] = f"STAR_{self.star}"
        if self.child:
            p["childIds"] = self.child
        return p

    def matches(self, review: Review) -> bool:
        return (self.star is None or review.rating == self.star) and (
            self.child is None or review.child_id == self.child
        )

    def __str__(self) -> str:
        parts = []
        if self.child:
            parts.append(f"variant {self.child}")
        if self.star:
            parts.append(f"{self.star}★")
        return " / ".join(parts) or "all reviews"


class NykaaScraper:
    """
    Scraper for Nykaa customer reviews and product details.
    """

    BASE_URL = "https://www.nykaa.com"
    GATEWAY_REVIEWS_URL = "https://www.nykaa.com/gateway-api/products/{product_id}/reviews"

    SORT_OPTIONS = {
        "MOST_RECENT": "Most Recent",
        "MOST_HELPFUL": "Most Helpful",
        "MOST_USEFUL": "Most Useful",
        "POSITIVE_FIRST": "Highest Rated",
        "NEGATIVE_FIRST": "Lowest Rated",
    }

    FILTER_OPTIONS = {
        "ALL": "All Reviews",
        "STAR_5": "5 Star",
        "STAR_4": "4 Star",
        "STAR_3": "3 Star",
        "STAR_2": "2 Star",
        "STAR_1": "1 Star",
    }

    def __init__(
        self,
        impersonate: Optional[str] = None,
        timeout: int = 20,
        delay_range: Tuple[float, float] = (0.0, 0.15),
        max_retries: int = 6,
        workers: int = 12,
    ):
        # None = pick the first fingerprint Akamai accepts
        self._impersonate_candidates = [impersonate] if impersonate else list(IMPERSONATE_CANDIDATES)
        self.impersonate = self._impersonate_candidates[0]
        self.timeout = timeout
        self.delay_range = delay_range
        self.max_retries = max_retries
        self.workers = max(1, workers)

        # Main session (product page, search, first-page probes)
        self.session = requests.Session(impersonate=self.impersonate)
        self._session_initialized = False
        self._session_lock = threading.Lock()

        # Per-thread sessions for concurrent page fetching (curl handles are not thread-safe)
        self._local = threading.local()

        # Shared back-off, only used when one request keeps getting rate limited
        self._cooldown_lock = threading.Lock()
        self._cooldown_until = 0.0

        self._cancel_event: Optional[threading.Event] = None

    # ------------------------------------------------------------------ #
    # Session handling
    # ------------------------------------------------------------------ #

    def _new_session(self) -> Tuple[requests.Session, bool]:
        """
        A fresh session whose cookies Akamai has accepted. If the current fingerprint is
        refused (403), moves on to the next candidate and remembers the one that works.
        """
        with self._session_lock:
            start = self._impersonate_candidates.index(self.impersonate) if self.impersonate in self._impersonate_candidates else 0
            order = self._impersonate_candidates[start:] + self._impersonate_candidates[:start]
        last_status = None
        for imp in order:
            try:
                sess = requests.Session(impersonate=imp)
                resp = sess.get(
                    self.BASE_URL,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                        "Accept-Language": "en-IN,en;q=0.9",
                    },
                    timeout=self.timeout,
                )
                last_status = resp.status_code
                if resp.status_code == 200:
                    if imp != self.impersonate:
                        logger.info(f"Using browser fingerprint '{imp}'.")
                        with self._session_lock:
                            self.impersonate = imp
                    return sess, True
                if resp.status_code != 403:
                    # Not a fingerprint problem (e.g. 5xx); keep this session and let retries handle it
                    return sess, False
                logger.debug(f"Fingerprint '{imp}' refused by Nykaa (403).")
            except Exception as e:
                last_status = str(e)
                logger.debug(f"Fingerprint '{imp}' failed: {e}")
        logger.warning(f"Nykaa refused every browser fingerprint (last result: {last_status}).")
        return requests.Session(impersonate=self.impersonate), False

    def _ensure_session(self, force_refresh: bool = False):
        """Initializes cookies by visiting the homepage if not already done."""
        if self._session_initialized and not force_refresh:
            return
        logger.info("Initializing Nykaa browser session...")
        self.session, self._session_initialized = self._new_session()
        if self._session_initialized:
            logger.info("Nykaa session initialized successfully.")

    def _thread_session(self, force_refresh: bool = False) -> requests.Session:
        """Returns the calling thread's own session, creating and warming it if needed."""
        if threading.current_thread() is threading.main_thread() and not force_refresh:
            return self.session
        if force_refresh or not hasattr(self._local, "session"):
            self._local.session, _ = self._new_session()
            if threading.current_thread() is threading.main_thread():
                self.session = self._local.session
        return self._local.session

    def _wait_for_cooldown(self):
        while True:
            self._check_cancelled()
            with self._cooldown_lock:
                remaining = self._cooldown_until - time.time()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.5))

    def _set_cooldown(self, seconds: float):
        with self._cooldown_lock:
            self._cooldown_until = max(self._cooldown_until, time.time() + seconds)

    def _check_cancelled(self):
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise ScrapeCancelled()

    # ------------------------------------------------------------------ #
    # Target resolution, search and product metadata
    # ------------------------------------------------------------------ #

    def resolve_target(self, target: str) -> Tuple[str, str, Optional[str]]:
        """
        Turns a URL / product ID / search query into (product_id, product_url, alternate_id).
        Follows redirects for share / short links (e.g. nykaa.app.link) that have no ID in them.
        """
        target = target.strip()
        primary_id, canonical_url, alt_id = extract_product_id(target)
        if primary_id:
            return primary_id, canonical_url, alt_id

        looks_like_url = target.lower().startswith(("http://", "https://")) or re.match(r"^[\w.-]+\.[a-z]{2,}/", target, re.I)
        if looks_like_url:
            url = target if target.lower().startswith("http") else f"https://{target}"
            self._ensure_session()
            try:
                resp = self.session.get(url, allow_redirects=True, timeout=self.timeout)
                pid, curl, aid = extract_product_id(str(resp.url))
                if pid:
                    return pid, curl, aid
                # Some share links land on an HTML page that embeds the canonical product URL
                m = re.search(r'https?://www\.nykaa\.com/[^"\'\s]*/p/\d+[^"\'\s]*', resp.text)
                if m:
                    pid, curl, aid = extract_product_id(m.group(0))
                    if pid:
                        return pid, curl, aid
            except Exception as e:
                logger.warning(f"Could not follow link '{target}': {e}")
            raise ValueError(f"Could not find a Nykaa product ID in link: '{target}'")

        logger.info(f"Target '{target}' is not a direct URL/ID. Searching Nykaa...")
        search_matches = self.search_products(target, limit=1)
        if not search_matches:
            raise ValueError(f"Could not find any product matching search term: '{target}'")
        first_match = search_matches[0]
        logger.info(f"Resolved search to product: '{first_match['name']}' (ID: {first_match['id']})")
        return first_match["id"], first_match["url"], None

    @staticmethod
    def _preloaded_state(html_text: str) -> Dict[str, Any]:
        """The JSON state Nykaa's pages embed for their React app (empty dict if absent)."""
        m = re.search(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*</script>", html_text, re.S)
        if not m:
            return {}
        try:
            return json.loads(m.group(1))
        except Exception:
            return {}

    def search_products(self, query: str, limit: int = 6) -> List[Dict[str, str]]:
        """
        Searches Nykaa for products matching the query.
        Returns list of dicts with id, name, slug, and url.
        """
        self._ensure_session()
        search_url = f"{self.BASE_URL}/search/result/?q={quote_plus(query.strip())}"

        try:
            resp = self.session.get(
                search_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": self.BASE_URL,
                },
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                logger.error(f"Search request failed with status {resp.status_code}")
                return []

            results: List[Dict[str, str]] = []
            seen_ids: Set[str] = set()

            # 1. Search results from the page's embedded state, in Nykaa's ranking order
            state = self._preloaded_state(resp.text)
            products = []
            for key in ("searchListingPage", "categoryListing"):
                products = (((state.get(key) or {}).get("listingData") or {}).get("products")) or []
                if products:
                    break
            for p in products:
                pid = str(p.get("parentId") or p.get("id") or p.get("productId") or "")
                slug = str(p.get("slug") or "")
                if not pid or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                path = slug if slug.startswith("/") else f"/{slug}" if slug else f"/product/p/{pid}"
                results.append({
                    "id": pid,
                    "name": clean_text(p.get("name") or p.get("title") or slug.replace("-", " ").title()),
                    "slug": slug,
                    "url": f"{self.BASE_URL}{path}",
                })
                if len(results) >= limit:
                    return results

            # 2. Fallback: product links in the HTML
            pattern = r'<a\s+[^>]*href="(/([^/"]+)/p/(\d+)[^"]*)"[^>]*>'
            for href, slug, pid in re.findall(pattern, resp.text):
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                results.append({
                    "id": pid,
                    "name": slug.replace("-", " ").title(),
                    "slug": slug,
                    "url": f"{self.BASE_URL}{href}",
                })
                if len(results) >= limit:
                    break

            return results
        except Exception as e:
            logger.error(f"Error during product search: {e}")
            return []

    def get_product_info(self, product_id: str, product_url: Optional[str] = None) -> ProductInfo:
        """
        Fetches metadata for a product (name, brand, price, rating, variant IDs).
        """
        self._ensure_session()
        target_url = product_url or f"{self.BASE_URL}/product/p/{product_id}"

        info = ProductInfo(id=product_id, url=target_url)

        try:
            resp = self.session.get(
                target_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": self.BASE_URL,
                },
                timeout=self.timeout,
            )

            if resp.status_code == 200:
                html_text = resp.text

                # 1. The page's own React state: most complete source, includes variants
                product = ((self._preloaded_state(html_text).get("productPage") or {}).get("product")) or {}
                if product:
                    info.name = clean_text(product.get("name") or "")
                    info.brand = clean_text(product.get("brandName") or "")
                    info.image_url = str(product.get("imageUrl") or "")
                    info.price = product.get("offerPrice") or product.get("mrp")
                    info.availability = "InStock" if product.get("inStock") else "OutOfStock"
                    try:
                        info.average_rating = float(product["rating"]) if product.get("rating") else None
                    except (TypeError, ValueError):
                        pass
                    info.total_reviews_count = safe_int(product.get("reviewCount"))
                    info.total_ratings_count = safe_int(product.get("ratingCount"))
                    info.parent_id = str(product.get("parentId") or "") or None
                    info.variant_ids = [
                        str(v["childId"]) for v in product.get("variants") or [] if isinstance(v, dict) and v.get("childId")
                    ]

                # 2. Structured data (application/ld+json) for anything still missing
                if not info.name:
                    for ld_block in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html_text, re.DOTALL):
                        try:
                            data = json.loads(ld_block)
                        except Exception as json_err:
                            logger.debug(f"Could not parse ld+json: {json_err}")
                            continue
                        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
                        product_item = next((i for i in items if isinstance(i, dict) and i.get("@type") == "Product"), None)
                        if not product_item:
                            continue
                        info.name = clean_text(product_item.get("name") or "")
                        brand_val = product_item.get("brand")
                        if isinstance(brand_val, dict):
                            info.brand = brand_val.get("name", "")
                        elif isinstance(brand_val, str):
                            info.brand = brand_val
                        offers = product_item.get("offers")
                        if isinstance(offers, list) and offers:
                            offers = offers[0]
                        if isinstance(offers, dict):
                            info.price = offers.get("price")
                            info.currency = offers.get("priceCurrency", "INR")
                        break

                # 3. OpenGraph / <title> fallbacks
                if not info.name:
                    og_title = re.search(r'<meta property="og:title"\s+content="([^"]+)"', html_text)
                    if og_title:
                        info.name = clean_text(og_title.group(1))
                if not info.image_url:
                    og_img = re.search(r'<meta property="og:image"\s+content="([^"]+)"', html_text)
                    if og_img:
                        info.image_url = og_img.group(1)
                if not info.name:
                    title_match = re.search(r'<title>(.*?)</title>', html_text)
                    if title_match:
                        info.name = clean_text(title_match.group(1).split("|")[0])
            else:
                logger.warning(f"Product page returned HTTP {resp.status_code}")

        except Exception as e:
            logger.warning(f"Could not fetch full product HTML: {e}")

        if not info.name:
            info.name = f"Nykaa Product #{product_id}"

        return info

    # ------------------------------------------------------------------ #
    # Reviews API
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_review(item: Dict[str, Any]) -> Optional[Review]:
        """Converts one raw API review into a Review. Never raises on odd/missing fields."""
        review_id = item.get("id") or item.get("reviewId")
        if review_id in (None, ""):
            return None
        rating = safe_int(item.get("rating"), default=0)
        return Review(
            id=str(review_id),
            child_id=str(item["childId"]) if item.get("childId") else None,
            title=clean_text(item.get("title")),
            description=clean_text(item.get("description")),
            reviewer_name=clean_text(item.get("name")) or "Anonymous",
            rating=min(max(rating, 0), 5),
            created_on=str(item.get("createdOn") or ""),
            relative_date=str(item.get("reviewCreationTextV2") or item.get("reviewCreationText") or ""),
            is_buyer=bool(item.get("isBuyer")),
            label=clean_text(item.get("label")),
            like_count=safe_int(item.get("likeCount")),
            customer_profile_url=str(item.get("customerProfileUrl") or ""),
            user_review_count=safe_int(item.get("userReviewCount")),
            pro_user=bool(item.get("proUser")),
        )

    @staticmethod
    def _extract_total(data: Dict[str, Any]) -> int:
        resp_obj = data.get("response") if isinstance(data.get("response"), dict) else {}
        for container in (data, resp_obj):
            for key in ("reviewCount", "totalCount", "total"):
                val = safe_int(container.get(key))
                if val:
                    return val
        return 0

    def fetch_reviews_page(
        self,
        product_id: str,
        page_no: int = 1,
        sort: str = "MOST_USEFUL",
        filter_by: Optional[str] = None,
        referer: Optional[str] = None,
        child_id: Optional[str] = None,
    ) -> PageResult:
        """
        Fetches a single page of reviews from Nykaa's reviews API (thread-safe).
        """
        api_url = self.GATEWAY_REVIEWS_URL.format(product_id=product_id)

        params: Dict[str, Any] = {
            "pageNo": str(page_no),
            "domain": "nykaa",
            "sort": sort,
        }
        if filter_by and filter_by != "ALL":
            params["filters"] = filter_by
        if child_id:
            params["childIds"] = child_id

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": referer or f"{self.BASE_URL}/product/p/{product_id}",
            "Origin": self.BASE_URL,
        }

        session = self._thread_session()

        for attempt in range(1, self.max_retries + 1):
            self._wait_for_cooldown()
            try:
                resp = session.get(api_url, params=params, headers=headers, timeout=self.timeout)

                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except Exception:
                        # Akamai sometimes serves an HTML challenge with status 200
                        logger.warning(f"Non-JSON response on page {page_no}; refreshing session (attempt {attempt}/{self.max_retries})")
                        session = self._thread_session(force_refresh=True)
                        time.sleep(0.5 * attempt)
                        continue

                    resp_obj = data.get("response") if isinstance(data.get("response"), dict) else {}
                    raw_items = resp_obj.get("reviewData") or resp_obj.get("data") or data.get("reviews") or []

                    reviews = []
                    for item in raw_items:
                        if isinstance(item, dict):
                            rev = self._parse_review(item)
                            if rev:
                                reviews.append(rev)

                    return PageResult(
                        status=PAGE_OK,
                        reviews=reviews,
                        total_count=self._extract_total(data),
                        page_no=page_no,
                    )

                elif resp.status_code == 400:
                    # Nykaa answers 400 for pages beyond its ceiling (page > 40) and unknown params
                    logger.debug(f"HTTP 400 on page {page_no}: end of pages")
                    return PageResult(status=PAGE_END, page_no=page_no)

                elif resp.status_code == 429:
                    # Nykaa rate-limits a random ~10% of requests; a short pause on this request is enough
                    wait_time = 0.7 * attempt + random.uniform(0.2, 0.8)
                    logger.debug(f"Rate limited (429) on page {page_no}; retrying in {wait_time:.1f}s (attempt {attempt}/{self.max_retries})")
                    if attempt >= 4:
                        # Persistent limiting: let every worker breathe
                        self._set_cooldown(wait_time)
                    time.sleep(wait_time)

                elif resp.status_code == 403:
                    # Akamai blocked this session's cookies/fingerprint: start a fresh one
                    logger.warning(f"Blocked (403) on page {page_no}; new session (attempt {attempt}/{self.max_retries})")
                    time.sleep(1.0 * attempt)
                    session = self._thread_session(force_refresh=True)

                else:
                    logger.debug(f"Reviews API returned HTTP {resp.status_code} for page {page_no} (attempt {attempt}/{self.max_retries})")
                    time.sleep(0.8 * attempt)

            except ScrapeCancelled:
                raise
            except Exception as e:
                logger.debug(f"Request error on page {page_no} (attempt {attempt}/{self.max_retries}): {e}")
                time.sleep(0.8 * attempt)

        logger.warning(f"Giving up on page {page_no} after {self.max_retries} attempts.")
        return PageResult(status=PAGE_ERROR, page_no=page_no)

    # ------------------------------------------------------------------ #
    # Crawling
    # ------------------------------------------------------------------ #

    @staticmethod
    def _order_reviews(reviews: List[Review], sort: str) -> List[Review]:
        """Orders the collected reviews the way the user asked for."""
        if sort == "MOST_RECENT":
            return sorted(reviews, key=lambda r: r.created_on, reverse=True)
        if sort in ("MOST_HELPFUL", "MOST_USEFUL"):
            return sorted(reviews, key=lambda r: (r.like_count, r.created_on), reverse=True)
        if sort == "POSITIVE_FIRST":
            return sorted(reviews, key=lambda r: (r.rating, r.created_on), reverse=True)
        if sort == "NEGATIVE_FIRST":
            return sorted(reviews, key=lambda r: (-r.rating, r.created_on), reverse=True)
        return reviews

    def scrape(
        self,
        target: str,
        max_reviews: Optional[int] = None,
        sort: str = "MOST_RECENT",
        filter_by: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int, int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> ScrapeResult:
        """
        Scrapes reviews for a product URL, product ID or search query.

        max_reviews: stop after this many unique reviews (None = all).
        sort: order of the returned reviews (and which reviews come first when limited).
        filter_by: None/"ALL" or "STAR_1".."STAR_5".
        progress_callback(collected, target_total, pages_fetched) is called as pages arrive.
        cancel_event: set it from another thread to stop early; the reviews collected so far are returned.
        Raises ScrapeCancelled if cancelled before the crawl itself started.
        """
        self._cancel_event = cancel_event
        try:
            return self._scrape(target, max_reviews, sort, filter_by, progress_callback)
        finally:
            self._cancel_event = None

    def _scrape(self, target, max_reviews, sort, filter_by, progress_callback) -> ScrapeResult:
        sort = sort if sort in ALL_SORTS else "MOST_RECENT"
        if filter_by in ("ALL", "VERIFIED_BUYER", ""):
            # Nykaa only serves verified-buyer reviews, so this filter is always on
            filter_by = None
        if filter_by and filter_by not in STAR_FILTERS:
            raise ValueError(f"Unknown filter '{filter_by}'. Use one of: ALL, {', '.join(STAR_FILTERS)}")
        if max_reviews is not None and max_reviews <= 0:
            max_reviews = None

        self._ensure_session()
        if not self._session_initialized:
            raise RuntimeError(
                "Nykaa refused the connection (blocked by its bot protection). "
                "Try again in a few minutes, from a different network, or update curl_cffi."
            )

        # Step 1: Resolve target to product ID and canonical URL
        primary_id, canonical_url, alt_id = self.resolve_target(target)

        logger.info(f"Fetching product information for ID: {primary_id}...")
        product_info = self.get_product_info(primary_id, canonical_url)
        referer = product_info.url
        root = Bucket(star=int(filter_by[-1]) if filter_by else None)

        # Step 2: Pick the product ID that actually has reviews
        first_sort = sort if max_reviews else CRAWL_SORTS[0]
        probe = self.fetch_reviews_page(primary_id, 1, first_sort, filter_by, referer)
        active_id = primary_id
        candidates = [c for c in (alt_id, product_info.parent_id) if c and c != primary_id]
        for cand in candidates:
            if probe.status == PAGE_OK and probe.reviews:
                break
            logger.info(f"Product ID {active_id} returned no reviews. Trying ID {cand}...")
            alt_probe = self.fetch_reviews_page(cand, 1, first_sort, filter_by, referer)
            if alt_probe.status == PAGE_OK and alt_probe.reviews:
                probe, active_id = alt_probe, cand
                product_info.id = cand
        if probe.status == PAGE_ERROR:
            raise RuntimeError(
                "Nykaa's reviews API could not be reached after several retries "
                "(blocked or rate limited). Please wait a minute and try again."
            )

        # Total number of reviews Nykaa reports (for the requested filter)
        total_available = probe.total_count if probe.status == PAGE_OK else 0
        if not filter_by and total_available:
            product_info.total_reviews_count = total_available
        target_count = total_available or None
        if max_reviews:
            target_count = min(target_count, max_reviews) if target_count else max_reviews

        # --- crawl state ---------------------------------------------------
        lock = threading.Lock()
        seen_ids: Set[str] = set()
        all_reviews: List[Review] = []
        bucket_counts: Dict[Bucket, int] = {root: total_available}
        known_children: List[str] = list(product_info.variant_ids)
        failed_pages: List[Tuple[Bucket, str, int]] = []
        crawled: Set[Tuple[Bucket, str]] = set()
        # Buckets whose every servable review has been read: Nykaa's reviewCount includes reviews
        # it never serves (hidden/moderated), so "collected == reported" isn't always reachable
        exhausted: Set[Bucket] = set()
        # Buckets bigger than one query can reach that stayed incomplete after every sort order
        ceiling_limited: List[Bucket] = []
        pages_fetched = 0
        consecutive_failures = 0
        warning = ""

        def add_reviews(reviews: List[Review]) -> int:
            added = 0
            with lock:
                for rev in reviews:
                    if rev.id not in seen_ids:
                        seen_ids.add(rev.id)
                        all_reviews.append(rev)
                        added += 1
                    if rev.child_id and rev.child_id not in known_children:
                        known_children.append(rev.child_id)
            return added

        def collected_in(bucket: Bucket) -> int:
            with lock:
                if bucket == root:
                    return len(all_reviews)
                return sum(1 for r in all_reviews if bucket.matches(r))

        def bucket_done(bucket: Bucket) -> bool:
            if bucket in exhausted:
                return True
            count = bucket_counts.get(bucket)
            return count is not None and collected_in(bucket) >= count

        def report():
            if progress_callback:
                try:
                    collected = len(all_reviews)
                    if max_reviews:
                        collected = min(collected, max_reviews)
                    progress_callback(collected, target_count or total_available, pages_fetched)
                except Exception:
                    pass

        def done() -> bool:
            if max_reviews and len(all_reviews) >= max_reviews:
                return True
            return bool(target_count) and len(all_reviews) >= target_count

        def track(res: PageResult):
            """Counts consecutive failed pages; trips the circuit breaker when Nykaa keeps refusing."""
            nonlocal consecutive_failures
            with lock:
                if res.status == PAGE_ERROR:
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILED_PAGES:
                        raise _Blocked()
                else:
                    consecutive_failures = 0

        def fetch(bucket: Bucket, p_sort: str, page_no: int) -> PageResult:
            if self.delay_range[1] > 0:
                time.sleep(random.uniform(*self.delay_range))
            return self.fetch_reviews_page(
                active_id, page_no, p_sort, bucket.params().get("filters"), referer, bucket.params().get("childIds")
            )

        def handle_page(bucket: Bucket, p_sort: str, res: PageResult) -> None:
            nonlocal pages_fetched
            pages_fetched += 1
            track(res)
            if res.status == PAGE_ERROR:
                failed_pages.append((bucket, p_sort, res.page_no))
            elif res.status == PAGE_OK:
                if res.page_no == 1 and res.total_count:
                    bucket_counts.setdefault(bucket, res.total_count)
                add_reviews(res.reviews)

        first_pages: Dict[Tuple[Bucket, str], PageResult] = {}
        # Page ceiling actually observed (Nykaa answers 400 past it); starts at the known value
        ceiling = {"pages": PAGE_CEILING}

        def query_reach() -> int:
            return ceiling["pages"] * API_PAGE_SIZE

        def first_page(bucket: Bucket, p_sort: str) -> PageResult:
            """Page 1 of a bucket: tells us how many reviews it holds. Cached."""
            if (bucket, p_sort) in first_pages:
                return first_pages[(bucket, p_sort)]
            self._check_cancelled()
            res = fetch(bucket, p_sort, 1)
            handle_first_page(bucket, p_sort, res)
            return res

        def handle_first_page(bucket: Bucket, p_sort: str, res: PageResult) -> None:
            first_pages[(bucket, p_sort)] = res
            handle_page(bucket, p_sort, res)
            if res.status == PAGE_OK and not res.total_count and not res.reviews:
                bucket_counts.setdefault(bucket, 0)
            report()

        def prefetch_first_pages(buckets: List[Bucket], p_sort: str) -> None:
            """Fetches page 1 of many buckets in parallel (learns their sizes in one go)."""
            todo = [b for b in buckets if (b, p_sort) not in first_pages and b not in bucket_counts]
            futures = {executor.submit(fetch, b, p_sort, 1): b for b in todo}
            try:
                for fut in as_completed(futures):
                    handle_first_page(futures[fut], p_sort, fut.result())
                    self._check_cancelled()
            finally:
                for fut in futures:
                    fut.cancel()

        def note_early_end(page_no: int) -> None:
            """Nykaa stopped serving pages earlier than expected: lower the assumed ceiling."""
            with lock:
                if page_no - 1 < ceiling["pages"]:
                    ceiling["pages"] = max(1, page_no - 1)
                    logger.info(f"Nykaa serves at most {ceiling['pages']} pages per query.")

        def crawl(bucket: Bucket, p_sort: str) -> Tuple[bool, int]:
            """
            Fetches every page of one (bucket, sort) query, in parallel, until the bucket is complete.
            Returns (full_pass, new_reviews): full_pass is True if every page was read successfully.
            """
            if (bucket, p_sort) in crawled or done() or bucket_done(bucket):
                return False, 0
            crawled.add((bucket, p_sort))
            before = collected_in(bucket)

            pages_from = 2
            if (bucket, p_sort) in first_pages or bucket not in bucket_counts:
                # Page 1 alone first: it tells us how many pages there are
                first = first_page(bucket, p_sort)
                if first.status != PAGE_OK:
                    return False, 0
                if done() or bucket_done(bucket):
                    return False, collected_in(bucket) - before
            else:
                pages_from = 1  # size already known: page 1 goes in the parallel batch too

            count = bucket_counts.get(bucket) or query_reach()
            last_page = min(ceiling["pages"], max(1, math.ceil(count / API_PAGE_SIZE)))
            if last_page < pages_from:
                return True, collected_in(bucket) - before
            logger.info(f"Crawling {bucket} sorted by {p_sort} (pages {pages_from}-{last_page}). Collected {len(all_reviews)}/{total_available or '?'}")

            futures = {executor.submit(fetch, bucket, p_sort, pno): pno for pno in range(pages_from, last_page + 1)}
            ok_pages = 0
            try:
                for fut in as_completed(futures):
                    res = fut.result()
                    handle_page(bucket, p_sort, res)
                    if res.status == PAGE_OK:
                        ok_pages += 1
                    elif res.status == PAGE_END and res.page_no <= last_page:
                        note_early_end(res.page_no)
                    report()
                    if done() or bucket_done(bucket):
                        break
                    self._check_cancelled()
            finally:
                for fut in futures:
                    fut.cancel()
            return ok_pages == len(futures), collected_in(bucket) - before

        def crawl_small_buckets(buckets: List[Bucket]) -> None:
            """
            Crawls many buckets that each fit in one query with a single shared batch of page
            requests, so workers stay busy even when every bucket is only a few pages long.
            A bucket whose pages were all read successfully is complete.
            """
            p_sort = sorts_for(buckets[0])[0] if buckets else CRAWL_SORTS[0]
            jobs: Dict[Any, Tuple[Bucket, int]] = {}
            pending: Dict[Bucket, int] = {}
            for b in buckets:
                first = first_pages.get((b, p_sort))
                if first is None or first.status != PAGE_OK or bucket_done(b):
                    continue
                crawled.add((b, p_sort))
                count = bucket_counts.get(b) or 0
                last_page = min(ceiling["pages"], max(1, math.ceil(count / API_PAGE_SIZE)))
                pending[b] = last_page - 1
                if last_page < 2:
                    exhausted.add(b)
                    continue
                for pno in range(2, last_page + 1):
                    jobs[executor.submit(fetch, b, p_sort, pno)] = (b, pno)
            if jobs:
                logger.info(f"Crawling {len(pending)} smaller groups together ({len(jobs)} pages). Collected {len(all_reviews)}/{total_available or '?'}")
            failed: Set[Bucket] = set()
            try:
                for fut in as_completed(jobs):
                    b, pno = jobs[fut]
                    res = fut.result()
                    handle_page(b, p_sort, res)
                    if res.status == PAGE_ERROR:
                        failed.add(b)
                    elif res.status == PAGE_END:
                        note_early_end(pno)
                    report()
                    if done():
                        break
                    self._check_cancelled()
            finally:
                for fut in jobs:
                    fut.cancel()
            for b in pending:
                if b not in failed and bucket_counts.get(b, 0) <= query_reach():
                    exhausted.add(b)

        def crawl_in_order(bucket: Bucket, p_sort: str) -> None:
            """For limited scrapes: pages in parallel batches but added in page order, so the
            result is exactly the first N reviews in the requested sort order."""
            crawled.add((bucket, p_sort))
            count = bucket_counts.get(bucket) or query_reach()
            last_page = min(ceiling["pages"], max(1, math.ceil(count / API_PAGE_SIZE)))
            next_page = 2
            while next_page <= last_page and not done():
                self._check_cancelled()
                needed = math.ceil((max_reviews - len(all_reviews)) / API_PAGE_SIZE)
                batch = list(range(next_page, min(next_page + max(1, min(needed, self.workers)), last_page + 1)))
                next_page = batch[-1] + 1
                for res in executor.map(lambda pno: fetch(bucket, p_sort, pno), batch):
                    handle_page(bucket, p_sort, res)
                    report()
                    if res.status == PAGE_END or done():
                        return

        def sorts_for(bucket: Bucket) -> List[str]:
            # Within one star rating, POSITIVE_FIRST/NEGATIVE_FIRST return the MOST_USEFUL order
            if bucket.star:
                return STAR_CRAWL_SORTS
            return CRAWL_SORTS

        def crawl_all_sorts(bucket: Bucket, can_split: bool) -> None:
            """
            Crawls a bucket with one sort order after another until it is complete. A bucket that
            fits in one query is finished after one full pass (other orders return the same reviews).
            A bigger one gets a full pass in every sort order: each order reaches a different window
            of 800 reviews (MOST_RECENT in particular finds many that MOST_USEFUL ranks too low).
            """
            count = bucket_counts.get(bucket, 0)
            extra_found = 0
            for i, s in enumerate(sorts_for(bucket)):
                full_pass, new = crawl(bucket, s)
                if done() or bucket_done(bucket):
                    return
                if can_split and count > query_reach():
                    return  # the ceiling turned out lower than assumed: let the caller split instead
                if i == 0 and full_pass and count <= query_reach():
                    # Every page was read and the bucket fits in one query: nothing more to find
                    exhausted.add(bucket)
                    return
                if i > 0:
                    extra_found += new
            if count > query_reach() and extra_found:
                # Other sort orders found reviews the first missed: the page limit cuts this bucket
                # off, so a few reviews may sit outside every order's reach
                ceiling_limited.append(bucket)
            exhausted.add(bucket)

        def complete(bucket: Bucket, depth: int = 0) -> None:
            """Collects every review in a bucket, splitting it when it's too big for one query."""
            if done() or bucket_done(bucket):
                return
            if bucket not in bucket_counts:
                first = first_page(bucket, sorts_for(bucket)[0])
                if first.status != PAGE_OK:
                    return
            count = bucket_counts.get(bucket, 0)
            if count == 0 or bucket_done(bucket):
                return
            logger.info(f"{'  ' * depth}{bucket}: {count} reviews")

            can_split = not (bucket.child and bucket.star)
            if count <= query_reach() or not can_split:
                crawl_all_sorts(bucket, can_split)
                if done() or bucket_done(bucket) or not can_split or count <= query_reach():
                    return

            if bucket.star is None and bucket.child is None:
                # Split the product by star rating
                stars = [Bucket(star=k) for k in (5, 4, 3, 2, 1)]
                prefetch_first_pages(stars, sorts_for(stars[0])[0])
                crawl_small_buckets([b for b in stars if 0 < bucket_counts.get(b, 0) <= query_reach()])
                for star_bucket in stars:
                    complete(star_bucket, depth + 1)
                return

            # A star rating with too many reviews for one query: split it by variant (shade/size).
            # Variants come from the product page plus every childId seen in reviews; each pass over
            # the star bucket itself can reveal variants that are no longer sold.
            done_children: Set[str] = set()
            for i, s in enumerate(sorts_for(bucket)):
                crawl(bucket, s)
                new_children = [c for c in list(known_children) if c not in done_children]
                children = [Bucket(child=c, star=bucket.star) for c in new_children]
                prefetch_first_pages(children, sorts_for(bucket)[0])
                crawl_small_buckets([b for b in children if 0 < bucket_counts.get(b, 0) <= query_reach()])
                for child_bucket in children:
                    if done() or bucket_done(bucket):
                        return
                    done_children.add(child_bucket.child)
                    complete(child_bucket, depth + 1)
                if done() or bucket_done(bucket):
                    return
            exhausted.add(bucket)

        logger.info(
            f"Starting review scraping for '{product_info.name}' "
            f"(Nykaa reports {total_available or '?'} reviews, limit: {max_reviews or 'All'}, {self.workers} workers)..."
        )

        executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="nykaa")
        try:
            handle_page(root, first_sort, probe)
            report()

            first_pages[(root, first_sort)] = probe
            if max_reviews and max_reviews <= query_reach():
                # Limited scrape: take the first N in the requested order
                crawl_in_order(root, first_sort)

            # Full crawl: split until every bucket is complete
            complete(root)

            # Recovery pass for pages that failed (network / rate limits)
            if failed_pages and not done():
                logger.info(f"Retrying {len(failed_pages)} failed page(s) after a short pause...")
                time.sleep(3)
                retry_list, failed_pages[:] = list(failed_pages), []
                for bucket, p_sort, pno in retry_list:
                    if done():
                        break
                    self._check_cancelled()
                    if bucket_done(bucket):
                        continue
                    handle_page(bucket, p_sort, fetch(bucket, p_sort, pno))
                    report()
                if failed_pages:
                    warning = f"{len(failed_pages)} page(s) could not be fetched even after retrying; some reviews may be missing."
                    logger.warning(warning)

        except (ScrapeCancelled, KeyboardInterrupt):
            # KeyboardInterrupt: Ctrl+C in the CLI keeps what was collected
            warning = "Stopped early by user."
            logger.warning("Scrape cancelled; returning the reviews collected so far.")
        except _Blocked:
            warning = (
                "Nykaa kept refusing requests (blocked or rate limited), so scraping stopped early. "
                "Wait a few minutes and try again, or use fewer workers."
            )
            logger.error(warning)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        if max_reviews:
            all_reviews = all_reviews[:max_reviews]
        all_reviews = self._order_reviews(all_reviews, sort)

        # Summary statistics
        total_scraped = len(all_reviews)
        star_counts = {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0}
        verified_count = 0
        rating_sum = 0

        for r in all_reviews:
            star_key = str(r.rating)
            if star_key in star_counts:
                star_counts[star_key] += 1
            rating_sum += r.rating
            if r.is_buyer:
                verified_count += 1

        avg_rating = round(rating_sum / total_scraped, 2) if total_scraped > 0 else 0.0

        if not warning and target_count and total_scraped < target_count:
            if ceiling_limited:
                warning = (
                    f"Collected {total_scraped} of {target_count} reviews. Most of the gap is reviews Nykaa "
                    "counts but hides; a few may also be beyond Nykaa's page limit in very large groups "
                    f"({', '.join(str(b) for b in ceiling_limited[:5])})."
                )
            else:
                warning = (
                    f"Nykaa counts {target_count} reviews but only makes {total_scraped} of them available "
                    "(the rest are hidden on Nykaa's side). All available reviews were collected."
                )

        summary = RatingSummary(
            total_scraped=total_scraped,
            total_available=total_available,
            average_rating=avg_rating,
            star_distribution=star_counts,
            verified_buyer_count=verified_count,
            warning=warning,
        )

        coverage = f" ({total_scraped / total_available * 100:.1f}% of {total_available} reported)" if total_available else ""
        logger.info(
            f"Scraping completed! Extracted {total_scraped} unique reviews for '{product_info.name}'{coverage} "
            f"using {pages_fetched} page requests."
        )

        return ScrapeResult(
            product=product_info,
            summary=summary,
            reviews=all_reviews,
            scraped_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #

    def _raw_reviews_request(self, product_id: str, params: Dict[str, str], referer: str) -> Dict[str, Any]:
        """One raw reviews call for diagnostics: status, top-level keys, count and IDs."""
        query = {"domain": "nykaa", "pageNo": "1", "sort": "MOST_USEFUL", **params}
        out: Dict[str, Any] = {"params": query}
        for attempt in range(4):
            try:
                resp = self.session.get(
                    self.GATEWAY_REVIEWS_URL.format(product_id=product_id),
                    params=query,
                    headers={"Accept": "application/json, text/plain, */*", "Referer": referer, "Origin": self.BASE_URL},
                    timeout=self.timeout,
                )
            except Exception as e:
                out["error"] = str(e)
                return out
            out["http_status"] = resp.status_code
            if resp.status_code != 429:
                break
            time.sleep(1.5 * (attempt + 1))
        try:
            data = resp.json()
        except Exception:
            out["body_start"] = resp.text[:300]
            return out
        resp_obj = data.get("response") if isinstance(data.get("response"), dict) else {}
        items = resp_obj.get("reviewData") or resp_obj.get("data") or data.get("reviews") or []
        out["top_level_keys"] = sorted(data.keys())
        out["response_keys"] = sorted(resp_obj.keys())
        out["reported_count"] = self._extract_total(data)
        out["items"] = len(items)
        out["ids"] = [str(i.get("id")) for i in items if isinstance(i, dict)]
        out["child_ids"] = sorted({str(i.get("childId")) for i in items if isinstance(i, dict) and i.get("childId")})
        sort_opts = (data.get("sort") or {}).get("options") or []
        out["sort_options"] = [o.get("id") for o in sort_opts if isinstance(o, dict)]
        filter_opts = (data.get("filter") or {}).get("options") or []
        out["filter_options"] = [(o.get("id"), o.get("isSelected")) for o in filter_opts if isinstance(o, dict)]
        time.sleep(0.3)
        return out

    def diagnose(self, target: str) -> Dict[str, Any]:
        """
        Probes how Nykaa's reviews API behaves for one product: fingerprint, page size and
        ceiling, per-star and per-variant counts, and the sort/filter options it offers.
        Used to troubleshoot; prints nothing itself.
        """
        self._ensure_session()
        report: Dict[str, Any] = {
            "target": target,
            "time": datetime.now().isoformat(timespec="seconds"),
            "fingerprint": self.impersonate,
            "session_ok": self._session_initialized,
        }
        pid, url, alt = self.resolve_target(target)
        report["resolved"] = {"product_id": pid, "url": url, "alternate_id": alt}
        info = self.get_product_info(pid, url)
        report["product"] = {
            "name": info.name, "page_review_count": info.total_reviews_count,
            "page_rating_count": info.total_ratings_count, "parent_id": info.parent_id, "variant_ids": info.variant_ids,
        }

        def brief(r):
            return {k: r.get(k) for k in ("http_status", "reported_count", "items", "error", "body_start") if k in r}

        base = self._raw_reviews_request(pid, {}, url)
        report["page1"] = {k: v for k, v in base.items() if k != "ids"}
        report["star_counts"] = {f: brief(self._raw_reviews_request(pid, {"filters": f}, url)) for f in STAR_FILTERS}
        report["sorts"] = {s: brief(self._raw_reviews_request(pid, {"sort": s}, url)) for s in ALL_SORTS}
        report["page_ceiling"] = {str(p): brief(self._raw_reviews_request(pid, {"pageNo": str(p)}, url)) for p in (39, 40, 41)}
        children = list(dict.fromkeys(info.variant_ids + base.get("child_ids", [])))[:10]
        report["variant_counts"] = {c: brief(self._raw_reviews_request(pid, {"childIds": c}, url)) for c in children}
        return report
