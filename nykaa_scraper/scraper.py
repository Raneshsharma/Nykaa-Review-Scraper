"""
Core Nykaa Review Scraper.
Uses curl_cffi with browser TLS impersonation to reliably fetch reviews
bypassing Akamai WAF and rate limitations.

Nykaa's reviews API only serves a limited number of pages per query (~40), so a
single sort order cannot reach every review on popular products. The scraper
works around this by crawling many "partitions" (star rating x sort order,
verified buyers x sort order, ...) and de-duplicating by review ID until the
number of unique reviews matches the count Nykaa reports.

Pages inside a partition are fetched concurrently by a small pool of worker
threads, each with its own browser-impersonated session. Failed pages are never
silently dropped: they are retried in a final recovery pass.
"""

import re
import json
import math
import time
import random
import threading
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional, List, Dict, Any, Callable, Tuple, Set
from urllib.parse import quote_plus
from curl_cffi import requests

from .models import Review, ProductInfo, RatingSummary, ScrapeResult
from .utils import extract_product_id, clean_text, safe_int, logger


# Page fetch outcomes
PAGE_OK = "ok"        # page returned reviews
PAGE_END = "end"      # partition exhausted (empty page or pagination ceiling)
PAGE_ERROR = "error"  # request failed after all retries; page must be retried later

STAR_FILTERS = ["STAR_5", "STAR_4", "STAR_3", "STAR_2", "STAR_1"]
ALL_SORTS = ["MOST_RECENT", "MOST_HELPFUL", "POSITIVE_FIRST", "NEGATIVE_FIRST"]

# Absolute safety cap on pages per partition (Nykaa normally stops at ~40)
HARD_PAGE_CAP = 200

# Page-size parameters the API might honour; each is verified at runtime before use.
# A bigger page both cuts requests and pushes the ~40-page ceiling further out.
PAGE_SIZE_CANDIDATES = [{"pageSize": "100"}, {"pageSize": "50"}, {"size": "50"}, {"limit": "50"}]


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


class NykaaScraper:
    """
    Scraper for Nykaa customer reviews and product details.
    """

    BASE_URL = "https://www.nykaa.com"
    GATEWAY_REVIEWS_URL = "https://www.nykaa.com/gateway-api/products/{product_id}/reviews"

    SORT_OPTIONS = {
        "MOST_RECENT": "Most Recent",
        "MOST_HELPFUL": "Most Useful",
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
        "VERIFIED_BUYER": "Verified Buyers",
    }

    def __init__(
        self,
        impersonate: str = "chrome120",
        timeout: int = 20,
        delay_range: Tuple[float, float] = (0.15, 0.45),
        max_retries: int = 5,
        workers: int = 4,
    ):
        self.impersonate = impersonate
        self.timeout = timeout
        self.delay_range = delay_range
        self.max_retries = max_retries
        self.workers = max(1, workers)

        # Main session (product page, search, first-page probes)
        self.session = requests.Session(impersonate=self.impersonate)
        self._session_initialized = False

        # Per-thread sessions for concurrent page fetching (curl handles are not thread-safe)
        self._local = threading.local()

        # Shared back-off: when any worker is rate limited, every worker pauses
        self._cooldown_lock = threading.Lock()
        self._cooldown_until = 0.0

        self._cancel_event: Optional[threading.Event] = None

        # Extra query params (e.g. a verified page size) added to every reviews request
        self._extra_params: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Session handling
    # ------------------------------------------------------------------ #

    def _warm_session(self, session: requests.Session) -> bool:
        """Visits the homepage so Akamai issues its cookies to this session."""
        try:
            resp = session.get(
                self.BASE_URL,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                return True
            logger.warning(f"Session init returned status {resp.status_code}")
        except Exception as e:
            logger.warning(f"Session initialization warning: {e}")
        return False

    def _ensure_session(self, force_refresh: bool = False):
        """Initializes cookies and session headers by visiting homepage if not already done."""
        if self._session_initialized and not force_refresh:
            return
        if force_refresh:
            self.session = requests.Session(impersonate=self.impersonate)
        logger.info("Initializing Nykaa browser session...")
        self._session_initialized = self._warm_session(self.session)
        if self._session_initialized:
            logger.info("Nykaa session initialized successfully.")

    def _thread_session(self, force_refresh: bool = False) -> requests.Session:
        """Returns the calling thread's own session, creating and warming it if needed."""
        if threading.current_thread() is threading.main_thread() and not hasattr(self._local, "session"):
            # The main thread reuses the already-warm main session
            self._local.session = self.session
        if force_refresh or not hasattr(self._local, "session"):
            sess = requests.Session(impersonate=self.impersonate)
            self._warm_session(sess)
            self._local.session = sess
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

            pattern = r'<a\s+[^>]*href="(/([^/]+)/p/(\d+)[^"]*)"[^>]*>'
            matches = re.findall(pattern, resp.text)
            seen_ids = set()
            results = []

            for href, slug, pid in matches:
                if pid not in seen_ids:
                    seen_ids.add(pid)
                    clean_name = slug.replace("-", " ").title()
                    full_url = f"{self.BASE_URL}{href}" if href.startswith("/") else href
                    results.append({
                        "id": pid,
                        "name": clean_name,
                        "slug": slug,
                        "url": full_url,
                    })
                    if len(results) >= limit:
                        break

            return results
        except Exception as e:
            logger.error(f"Error during product search: {e}")
            return []

    def get_product_info(self, product_id: str, product_url: Optional[str] = None) -> ProductInfo:
        """
        Fetches metadata for a product (name, brand, image, price, rating).
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

                # 1. Try extracting structured data from application/ld+json (pages can carry several blocks)
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

                    image_val = product_item.get("image") or ""
                    info.image_url = image_val[0] if isinstance(image_val, list) and image_val else str(image_val)
                    offers = product_item.get("offers")
                    if isinstance(offers, list) and offers:
                        offers = offers[0]
                    if isinstance(offers, dict):
                        info.price = offers.get("price")
                        info.currency = offers.get("priceCurrency", "INR")
                        info.availability = str(offers.get("availability", "")).replace("https://schema.org/", "").replace("http://schema.org/", "")

                    agg = product_item.get("aggregateRating")
                    if isinstance(agg, dict):
                        try:
                            info.average_rating = float(agg["ratingValue"]) if agg.get("ratingValue") else None
                        except (TypeError, ValueError):
                            pass
                        info.total_reviews_count = safe_int(agg.get("reviewCount"))
                    break

                # 2. Fallbacks using OpenGraph meta tags if fields are missing
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
        sort: str = "MOST_RECENT",
        filter_by: Optional[str] = None,
        referer: Optional[str] = None,
        extra_params: Optional[Dict[str, str]] = None,
    ) -> PageResult:
        """
        Fetches a single page of reviews from Nykaa's reviews API (thread-safe).
        extra_params overrides the scraper's tuned extra params for this one request.
        """
        api_url = self.GATEWAY_REVIEWS_URL.format(product_id=product_id)

        params: Dict[str, Any] = {
            "pageNo": str(page_no),
            "domain": "nykaa",
            "sort": sort,
        }
        if filter_by and filter_by != "ALL":
            params["filters"] = filter_by
        params.update(self._extra_params if extra_params is None else extra_params)

        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
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
                        time.sleep(1.0 * attempt)
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
                        status=PAGE_OK if reviews else PAGE_END,
                        reviews=reviews,
                        total_count=self._extract_total(data),
                        page_no=page_no,
                    )

                elif resp.status_code in (400, 404, 422):
                    # Nykaa's API returns 400 when pageNo exceeds the maximum allowed page (e.g. page > 40)
                    logger.debug(f"Reached API pagination ceiling at page {page_no} (Status {resp.status_code})")
                    return PageResult(status=PAGE_END, page_no=page_no)

                elif resp.status_code == 429:
                    wait_time = 2.0 * attempt + random.uniform(0.5, 1.5)
                    logger.warning(f"Rate limited (429) on page {page_no}. Pausing {wait_time:.1f}s (attempt {attempt}/{self.max_retries})")
                    self._set_cooldown(wait_time)
                    if attempt >= 3:
                        session = self._thread_session(force_refresh=True)

                elif resp.status_code == 403:
                    # Akamai blocked this session's cookies/fingerprint: start a fresh one
                    wait_time = 2.0 * attempt
                    logger.warning(f"Blocked (403) on page {page_no}. New session in {wait_time:.1f}s (attempt {attempt}/{self.max_retries})")
                    self._set_cooldown(wait_time)
                    session = self._thread_session(force_refresh=True)

                else:
                    logger.warning(f"Reviews API returned HTTP {resp.status_code} for page {page_no} (attempt {attempt}/{self.max_retries})")
                    time.sleep(1.0 * attempt)

            except ScrapeCancelled:
                raise
            except Exception as e:
                logger.warning(f"Request error on page {page_no} (attempt {attempt}/{self.max_retries}): {e}")
                time.sleep(1.0 * attempt)

        return PageResult(status=PAGE_ERROR, page_no=page_no)

    # ------------------------------------------------------------------ #
    # Crawling
    # ------------------------------------------------------------------ #

    def _build_partitions(self, sort: str, filter_by: Optional[str]) -> List[Tuple[Optional[str], str]]:
        """Ordered list of (filter, sort) queries to crawl."""
        sorts = [sort] + [s for s in ALL_SORTS if s != sort]
        partitions: List[Tuple[Optional[str], str]] = []

        def add(p):
            if p not in partitions:
                partitions.append(p)

        if filter_by and filter_by != "ALL":
            for s in sorts:
                add((filter_by, s))
            return partitions

        # Unfiltered first: covers small products entirely in one pass
        add((None, sort))
        # Each star rating x every sort order: the main way past the per-query page ceiling
        for star in STAR_FILTERS:
            for s in sorts:
                add((star, s))
        # Extra angles that surface reviews the above can miss on very large products
        for s in sorts:
            add(("VERIFIED_BUYER", s))
        for s in sorts:
            add((None, s))
        return partitions

    def _tune_page_size(
        self, product_id: str, referer: str, sort: str, filter_by: Optional[str], baseline: int
    ) -> Optional[PageResult]:
        """
        Checks whether the API honours a bigger page size. Adopts the first candidate that
        returns more reviews than the default page AND pages correctly (page 2 doesn't overlap
        page 1). Returns that candidate's page 1, or None if the API ignores them all.
        """
        for cand in PAGE_SIZE_CANDIDATES:
            self._check_cancelled()
            first = self.fetch_reviews_page(product_id, 1, sort, filter_by, referer, extra_params=cand)
            if first.status != PAGE_OK or len(first.reviews) <= baseline:
                continue
            second = self.fetch_reviews_page(product_id, 2, sort, filter_by, referer, extra_params=cand)
            first_ids = {r.id for r in first.reviews}
            if second.status == PAGE_END or (second.status == PAGE_OK and not first_ids & {r.id for r in second.reviews}):
                logger.info(f"API accepts {cand}: {len(first.reviews)} reviews per page instead of {baseline}.")
                self._extra_params = dict(cand)
                return first
        logger.info(f"API uses fixed pages of {baseline} reviews.")
        return None

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
        progress_callback(collected, total_available, pages_fetched) is called as pages arrive.
        cancel_event: set it from another thread to stop early; the reviews collected so far are returned.
        Raises ScrapeCancelled if cancelled before the crawl itself started.
        """
        self._cancel_event = cancel_event
        try:
            return self._scrape(target, max_reviews, sort, filter_by, progress_callback)
        finally:
            self._cancel_event = None

    def _scrape(self, target, max_reviews, sort, filter_by, progress_callback) -> ScrapeResult:
        self._extra_params = {}
        sort = sort if sort in ALL_SORTS else "MOST_RECENT"
        if filter_by == "ALL":
            filter_by = None
        if max_reviews is not None and max_reviews <= 0:
            max_reviews = None

        self._ensure_session()

        # Step 1: Resolve target to product ID and canonical URL
        primary_id, canonical_url, alt_id = self.resolve_target(target)

        logger.info(f"Fetching product information for ID: {primary_id}...")
        product_info = self.get_product_info(primary_id, canonical_url)

        # Step 2: Pick the product ID that actually has reviews
        probe = self.fetch_reviews_page(primary_id, 1, sort, filter_by, product_info.url)
        active_id = primary_id
        if probe.status != PAGE_OK and alt_id and alt_id != primary_id:
            logger.info(f"Primary ID {primary_id} returned no reviews. Trying alternate ID {alt_id}...")
            alt_probe = self.fetch_reviews_page(alt_id, 1, sort, filter_by, product_info.url)
            if alt_probe.status == PAGE_OK:
                probe, active_id = alt_probe, alt_id
                product_info.id = alt_id
        if probe.status == PAGE_ERROR:
            raise RuntimeError(
                "Nykaa's reviews API could not be reached after several retries "
                "(blocked or rate limited). Please wait a minute and try again."
            )

        # Total number of reviews Nykaa reports (for the requested filter)
        total_available = probe.total_count
        if not filter_by and total_available:
            product_info.total_reviews_count = total_available
        target_count = total_available or None
        if max_reviews:
            target_count = min(target_count, max_reviews) if target_count else max_reviews

        # Step 3: Use bigger pages if the API allows it (only worth it beyond a couple of pages)
        if probe.status == PAGE_OK and (target_count or 0) > 2 * len(probe.reviews):
            tuned = self._tune_page_size(active_id, product_info.url, sort, filter_by, len(probe.reviews))
            if tuned:
                probe = tuned

        seen_ids: Set[str] = set()
        all_reviews: List[Review] = []
        star_counts_seen = {i: 0 for i in range(1, 6)}
        filter_totals: Dict[str, int] = {}
        failed_pages: List[Tuple[Optional[str], str, int]] = []
        crawled: Set[Tuple[Optional[str], str]] = set()
        pages_fetched = 0
        consecutive_failures = 0
        warning = ""
        lock = threading.Lock()

        def add_reviews(reviews: List[Review]) -> int:
            added = 0
            with lock:
                for rev in reviews:
                    if rev.id not in seen_ids:
                        seen_ids.add(rev.id)
                        all_reviews.append(rev)
                        if rev.rating in star_counts_seen:
                            star_counts_seen[rev.rating] += 1
                        added += 1
            return added

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

        def star_complete(p_filter: Optional[str]) -> bool:
            """True once every review of this partition's star rating is already collected."""
            star = p_filter.split(",")[0] if p_filter else ""
            if star not in STAR_FILTERS or star not in filter_totals:
                return False
            return star_counts_seen[int(star[-1])] >= filter_totals[star]

        def track(res: PageResult):
            """Counts consecutive failed pages; trips the circuit breaker when Nykaa keeps refusing."""
            nonlocal consecutive_failures
            if res.status == PAGE_ERROR:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILED_PAGES:
                    raise _Blocked()
            else:
                consecutive_failures = 0

        def crawl_partition(p_filter: Optional[str], p_sort: str, first: Optional[PageResult] = None):
            nonlocal pages_fetched
            if (p_filter, p_sort) in crawled or done() or star_complete(p_filter):
                return
            crawled.add((p_filter, p_sort))
            self._check_cancelled()
            logger.info(f"--- Partition: Filter {p_filter or 'ALL'}, Sort {p_sort} ---")

            # Page 1 is fetched alone to learn page size and the partition's review count
            if first is None:
                first = self.fetch_reviews_page(active_id, 1, p_sort, p_filter, product_info.url)
            pages_fetched += 1

            if first.status == PAGE_ERROR:
                failed_pages.append((p_filter, p_sort, 1))
                track(first)
                return
            track(first)
            if first.status == PAGE_END:
                if p_filter:
                    filter_totals.setdefault(p_filter, 0)
                return
            if p_filter and first.total_count:
                filter_totals.setdefault(p_filter, first.total_count)

            partition_ids: Set[str] = {r.id for r in first.reviews}
            new_added = add_reviews(first.reviews)
            page_size = len(first.reviews)
            logger.info(f"Page 1: +{page_size} fetched ({new_added} new). Total: {len(all_reviews)}/{total_available or '?'}")
            report()

            # Estimate how many pages this partition has (if Nykaa tells us its count)
            if first.total_count and page_size:
                if first.total_count <= page_size:
                    return
                last_page = min(math.ceil(first.total_count / page_size), HARD_PAGE_CAP)
            else:
                last_page = HARD_PAGE_CAP

            def fetch(pno):
                time.sleep(random.uniform(*self.delay_range))
                return self.fetch_reviews_page(active_id, pno, p_sort, p_filter, product_info.url)

            # Remaining pages: concurrent batches, stopping at the first exhausted page
            next_page = 2
            while next_page <= last_page and not done() and not star_complete(p_filter):
                self._check_cancelled()
                batch = list(range(next_page, min(next_page + self.workers, last_page + 1)))
                next_page = batch[-1] + 1

                ended = False
                for res in sorted(executor.map(fetch, batch), key=lambda r: r.page_no):
                    pages_fetched += 1
                    if res.status == PAGE_ERROR:
                        failed_pages.append((p_filter, p_sort, res.page_no))
                        track(res)
                        continue
                    track(res)
                    if res.status == PAGE_END:
                        ended = True
                        break
                    page_ids = {r.id for r in res.reviews}
                    if page_ids <= partition_ids:
                        # API is repeating pages it already served: ceiling reached
                        ended = True
                        break
                    partition_ids |= page_ids
                    new_added = add_reviews(res.reviews)
                    logger.info(f"Page {res.page_no}: +{len(res.reviews)} fetched ({new_added} new). Total: {len(all_reviews)}/{total_available or '?'}")
                report()
                if ended:
                    break

        sorts = [sort] + [s for s in ALL_SORTS if s != sort]
        partitions = self._build_partitions(sort, filter_by)
        logger.info(
            f"Starting review scraping for '{product_info.name}' "
            f"(Nykaa reports {total_available or '?'} reviews, limit: {max_reviews or 'All'}, "
            f"{len(partitions)} partitions, {self.workers} workers)..."
        )

        executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="nykaa")
        try:
            # Phase 1: unfiltered, every star x sort, verified buyers x sort
            for idx, (p_filter, p_sort) in enumerate(partitions):
                if done():
                    break
                crawl_partition(p_filter, p_sort, first=probe if idx == 0 else None)

            # Phase 2: stars that are still incomplete (too many reviews for the page ceiling).
            # Split them further with combined filters, if the API supports combining.
            verified_total = filter_totals.get("VERIFIED_BUYER", 0)
            if not filter_by and not done():
                for star in STAR_FILTERS:
                    if done():
                        break
                    if not filter_totals.get(star) or star_complete(star):
                        continue
                    combo = f"{star},VERIFIED_BUYER"
                    first = self.fetch_reviews_page(active_id, 1, sort, combo, product_info.url)
                    supported = (
                        first.status == PAGE_OK
                        and 0 < first.total_count < filter_totals[star]
                        and first.total_count != verified_total
                        and all(r.rating == int(star[-1]) and r.is_buyer for r in first.reviews)
                    )
                    if not supported:
                        logger.info("API does not support combined filters; skipping deeper split.")
                        break
                    logger.info(f"{star} still incomplete; crawling {combo} ({first.total_count} reviews).")
                    for s in sorts:
                        crawl_partition(combo, s, first=first if s == sort else None)

            # Phase 3: Recovery pass for pages that failed (network / rate limits)
            if failed_pages and not done():
                logger.info(f"Retrying {len(failed_pages)} failed page(s) after a short pause...")
                time.sleep(5)
                still_failed = 0
                retry_list, failed_pages[:] = list(failed_pages), []
                for p_filter, p_sort, pno in retry_list:
                    if done():
                        break
                    self._check_cancelled()
                    if pno == 1:
                        # The whole partition was skipped: crawl it again from the start
                        crawled.discard((p_filter, p_sort))
                        crawl_partition(p_filter, p_sort)
                        continue
                    res = self.fetch_reviews_page(active_id, pno, p_sort, p_filter, product_info.url)
                    pages_fetched += 1
                    if res.status == PAGE_OK:
                        add_reviews(res.reviews)
                        report()
                    elif res.status == PAGE_ERROR:
                        still_failed += 1
                still_failed += len(failed_pages)  # pages that failed again inside re-crawled partitions
                if still_failed:
                    warning = f"{still_failed} page(s) could not be fetched even after retrying; some reviews may be missing."
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

        # Step 4: Compute summary statistics
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

        summary = RatingSummary(
            total_scraped=total_scraped,
            total_available=total_available,
            average_rating=avg_rating,
            star_distribution=star_counts,
            verified_buyer_count=verified_count,
            warning=warning,
        )

        coverage = f" ({total_scraped / total_available * 100:.1f}% of {total_available} reported)" if total_available else ""
        logger.info(f"Scraping completed! Extracted {total_scraped} unique reviews for '{product_info.name}'{coverage}.")

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
        query = {"domain": "nykaa", "pageNo": "1", "sort": "MOST_RECENT", **params}
        out: Dict[str, Any] = {"params": query}
        try:
            resp = self.session.get(
                self.GATEWAY_REVIEWS_URL.format(product_id=product_id),
                params=query,
                headers={"Accept": "application/json, text/plain, */*", "Referer": referer, "Origin": self.BASE_URL},
                timeout=self.timeout,
            )
            out["http_status"] = resp.status_code
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
            out["ratings"] = [i.get("rating") for i in items if isinstance(i, dict)]
            if items and isinstance(items[0], dict):
                out["review_keys"] = sorted(items[0].keys())
            # Any numeric fields that might describe paging
            out["numeric_fields"] = {
                k: v for src in (data, resp_obj) for k, v in src.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
        except Exception as e:
            out["error"] = str(e)
        time.sleep(0.3)
        return out

    def diagnose(self, target: str) -> Dict[str, Any]:
        """
        Probes how Nykaa's reviews API behaves for one product: page size, page ceiling,
        per-filter counts, whether sort orders differ, and which page-size / combined-filter
        parameters it accepts. Used to tune the crawler; prints nothing itself.
        """
        self._ensure_session()
        report: Dict[str, Any] = {"target": target, "time": datetime.now().isoformat(timespec="seconds")}
        pid, url, alt = self.resolve_target(target)
        report["resolved"] = {"product_id": pid, "url": url, "alternate_id": alt}
        info = self.get_product_info(pid, url)
        report["product"] = {"name": info.name, "ld_json_review_count": info.total_reviews_count}

        def call(**params):
            r = self._raw_reviews_request(pid, {k: str(v) for k, v in params.items()}, url)
            return r

        def brief(r):
            return {k: r.get(k) for k in ("http_status", "reported_count", "items", "error", "body_start") if k in r}

        base = call()
        if not base.get("items") and alt:
            pid = alt
            base = call()
            report["resolved"]["used_alternate_id"] = True
        report["page1"] = {k: v for k, v in base.items() if k not in ("ids", "ratings")}
        page_size = base.get("items") or 0

        report["filter_counts"] = {
            f: brief(call(filters=f)) for f in ["STAR_5", "STAR_4", "STAR_3", "STAR_2", "STAR_1", "VERIFIED_BUYER"]
        }

        report["page_ceiling"] = {str(p): brief(call(pageNo=p)) for p in (2, 39, 40, 41, 45, 60)}

        # Do different sort orders return different reviews inside the biggest star bucket?
        counts = {f: v.get("reported_count") or 0 for f, v in report["filter_counts"].items() if f.startswith("STAR_")}
        biggest = max(counts, key=counts.get) if counts else "STAR_5"
        sort_ids = {}
        for s in ALL_SORTS:
            ids = set()
            for p in (1, 20):
                ids |= set(call(filters=biggest, sort=s, pageNo=p).get("ids", []))
            sort_ids[s] = ids
        report["sort_overlap"] = {
            "filter": biggest,
            "pages_sampled": [1, 20],
            "pairs": {
                f"{a} vs {b}": len(sort_ids[a] & sort_ids[b])
                for i, a in enumerate(ALL_SORTS) for b in ALL_SORTS[i + 1:]
            },
            "ids_per_sort": {s: len(v) for s, v in sort_ids.items()},
        }

        report["page_size_params"] = {
            json.dumps(c): brief(call(**c)) for c in PAGE_SIZE_CANDIDATES
        }
        report["default_page_size"] = page_size

        combo_tests = {
            "comma": {"filters": f"{biggest},VERIFIED_BUYER"},
            "pipe": {"filters": f"{biggest}|VERIFIED_BUYER"},
        }
        report["combined_filters"] = {}
        for name, params in combo_tests.items():
            r = call(**params)
            report["combined_filters"][name] = {**brief(r), "ratings_seen": sorted(set(map(str, r.get("ratings", []))))}

        for s in ("OLDEST", "MOST_OLD", "LEAST_RECENT"):
            report.setdefault("other_sorts", {})[s] = brief(call(sort=s))

        return report
