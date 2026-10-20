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


class ScrapeCancelled(Exception):
    """Raised internally when a caller cancels a running scrape."""


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
        formatted_query = query.strip().replace(" ", "+")
        search_url = f"{self.BASE_URL}/search/result/?q={formatted_query}"

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
        """
        self._cancel_event = cancel_event
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
        first_sort = sort
        probe = self.fetch_reviews_page(primary_id, 1, first_sort, filter_by, product_info.url)
        active_id = primary_id
        if probe.status != PAGE_OK and alt_id and alt_id != primary_id:
            logger.info(f"Primary ID {primary_id} returned no reviews. Trying alternate ID {alt_id}...")
            alt_probe = self.fetch_reviews_page(alt_id, 1, first_sort, filter_by, product_info.url)
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

        seen_ids: Set[str] = set()
        all_reviews: List[Review] = []
        star_counts_seen = {i: 0 for i in range(1, 6)}
        star_totals: Dict[str, int] = {}
        failed_pages: List[Tuple[Optional[str], str, int]] = []
        pages_fetched = 0
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
                    progress_callback(len(all_reviews), total_available, pages_fetched)
                except Exception:
                    pass

        def done() -> bool:
            if max_reviews and len(all_reviews) >= max_reviews:
                return True
            return bool(target_count) and len(all_reviews) >= target_count

        partitions = self._build_partitions(sort, filter_by)
        logger.info(
            f"Starting review scraping for '{product_info.name}' "
            f"(Nykaa reports {total_available or '?'} reviews, limit: {max_reviews or 'All'}, "
            f"{len(partitions)} partitions, {self.workers} workers)..."
        )

        executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="nykaa")
        try:
            for partition_idx, (p_filter, p_sort) in enumerate(partitions):
                if done():
                    break
                self._check_cancelled()

                # Skip a star partition once every review of that star is already collected
                if p_filter in star_totals:
                    star_num = int(p_filter[-1])
                    if star_totals[p_filter] and star_counts_seen[star_num] >= star_totals[p_filter]:
                        continue

                filter_name = p_filter or "ALL"
                logger.info(f"--- Partition {partition_idx + 1}/{len(partitions)} (Filter: {filter_name}, Sort: {p_sort}) ---")

                # Page 1 is fetched alone to learn page size and the partition's review count
                if partition_idx == 0 and (p_filter, p_sort) == (filter_by, first_sort):
                    first = probe
                else:
                    first = self.fetch_reviews_page(active_id, 1, p_sort, p_filter, product_info.url)
                pages_fetched += 1

                if first.status == PAGE_ERROR:
                    failed_pages.append((p_filter, p_sort, 1))
                    continue
                if first.status == PAGE_END:
                    if p_filter in STAR_FILTERS:
                        star_totals[p_filter] = 0
                    continue

                if p_filter in STAR_FILTERS and first.total_count:
                    star_totals.setdefault(p_filter, first.total_count)

                partition_ids: Set[str] = {r.id for r in first.reviews}
                new_added = add_reviews(first.reviews)
                page_size = len(first.reviews)
                logger.info(f"Page 1: +{page_size} fetched ({new_added} new). Total: {len(all_reviews)}/{total_available or '?'}")
                report()

                # Estimate how many pages this partition has (if Nykaa tells us its count)
                if first.total_count and page_size:
                    est_pages = math.ceil(first.total_count / page_size)
                    if first.total_count <= page_size:
                        continue
                else:
                    est_pages = HARD_PAGE_CAP
                last_page = min(est_pages, HARD_PAGE_CAP)

                # Remaining pages: concurrent batches, stopping at the first exhausted page
                next_page = 2
                partition_ended = False
                while next_page <= last_page and not partition_ended and not done():
                    self._check_cancelled()
                    batch = list(range(next_page, min(next_page + self.workers, last_page + 1)))
                    next_page = batch[-1] + 1

                    def fetch(pno, f=p_filter, s=p_sort):
                        time.sleep(random.uniform(*self.delay_range))
                        return self.fetch_reviews_page(active_id, pno, s, f, product_info.url)

                    results = list(executor.map(fetch, batch))
                    for res in sorted(results, key=lambda r: r.page_no):
                        pages_fetched += 1
                        if res.status == PAGE_ERROR:
                            failed_pages.append((p_filter, p_sort, res.page_no))
                            continue
                        if res.status == PAGE_END:
                            partition_ended = True
                            break
                        page_ids = {r.id for r in res.reviews}
                        if page_ids <= partition_ids:
                            # API is repeating pages it already served: ceiling reached
                            partition_ended = True
                            break
                        partition_ids |= page_ids
                        new_added = add_reviews(res.reviews)
                        logger.info(f"Page {res.page_no}: +{len(res.reviews)} fetched ({new_added} new). Total: {len(all_reviews)}/{total_available or '?'}")
                    report()

            # Step 3: Recovery pass for pages that failed (network / rate limits)
            if failed_pages and not done():
                logger.info(f"Retrying {len(failed_pages)} failed page(s) after a short pause...")
                time.sleep(5)
                still_failed = 0
                for p_filter, p_sort, pno in failed_pages:
                    if done():
                        break
                    self._check_cancelled()
                    res = self.fetch_reviews_page(active_id, pno, p_sort, p_filter, product_info.url)
                    pages_fetched += 1
                    if res.status == PAGE_OK:
                        add_reviews(res.reviews)
                        report()
                    elif res.status == PAGE_ERROR:
                        still_failed += 1
                if still_failed:
                    logger.warning(f"{still_failed} page(s) could not be fetched even after retrying.")

        except ScrapeCancelled:
            logger.warning("Scrape cancelled; returning the reviews collected so far.")
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            self._cancel_event = None

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
        )

        coverage = f" ({total_scraped / total_available * 100:.1f}% of {total_available} reported)" if total_available else ""
        logger.info(f"Scraping completed! Extracted {total_scraped} unique reviews for '{product_info.name}'{coverage}.")

        return ScrapeResult(
            product=product_info,
            summary=summary,
            reviews=all_reviews,
            scraped_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
