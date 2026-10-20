"""
Core Nykaa Review Scraper.
Uses curl_cffi with browser TLS impersonation to reliably fetch reviews
bypassing Akamai WAF and rate limitations.
Implements multi-partition deep scraping to overcome Nykaa's 40-page API ceiling.
"""

import re
import json
import time
import random
from datetime import datetime
from typing import Optional, List, Dict, Any, Callable, Tuple, Set
from curl_cffi import requests

from .models import Review, ProductInfo, RatingSummary, ScrapeResult
from .utils import extract_product_id, clean_text, logger


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
        "WITH_IMAGES": "With Images",
    }

    def __init__(
        self,
        impersonate: str = "chrome120",
        timeout: int = 15,
        delay_range: Tuple[float, float] = (0.9, 1.6),
        max_retries: int = 5,
    ):
        self.impersonate = impersonate
        self.timeout = timeout
        self.delay_range = delay_range
        self.max_retries = max_retries
        self.session = requests.Session(impersonate=self.impersonate)
        self._session_initialized = False

    def _ensure_session(self, force_refresh: bool = False):
        """Initializes cookies and session headers by visiting homepage if not already done."""
        if self._session_initialized and not force_refresh:
            return

        try:
            logger.info("Initializing Nykaa browser session...")
            resp = self.session.get(
                self.BASE_URL,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                self._session_initialized = True
                logger.info("Nykaa session initialized successfully.")
            else:
                logger.warning(f"Session init returned status {resp.status_code}")
        except Exception as e:
            logger.warning(f"Session initialization warning: {e}")

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

                # 1. Try extracting structured data from application/ld+json
                ld_match = re.search(r'<script type="application/ld\+json">(.*?)</script>', html_text, re.DOTALL)
                if ld_match:
                    try:
                        data = json.loads(ld_match.group(1))
                        items = data if isinstance(data, list) else [data]
                        for item in items:
                            if item.get("@type") == "Product":
                                info.name = clean_text(item.get("name") or "")
                                brand_val = item.get("brand")
                                if isinstance(brand_val, dict):
                                    info.brand = brand_val.get("name", "")
                                elif isinstance(brand_val, str):
                                    info.brand = brand_val

                                info.image_url = item.get("image") or ""
                                offers = item.get("offers")
                                if isinstance(offers, dict):
                                    info.price = offers.get("price")
                                    info.currency = offers.get("priceCurrency", "INR")
                                    info.availability = offers.get("availability", "").replace("http://schema.org/", "")

                                agg = item.get("aggregateRating")
                                if isinstance(agg, dict):
                                    info.average_rating = float(agg.get("ratingValue", 0)) if agg.get("ratingValue") else None
                                    info.total_reviews_count = int(agg.get("reviewCount", 0)) if agg.get("reviewCount") else 0
                                break
                    except Exception as json_err:
                        logger.debug(f"Could not parse ld+json: {json_err}")

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

    def fetch_reviews_page(
        self,
        product_id: str,
        page_no: int = 1,
        sort: str = "MOST_RECENT",
        filter_by: Optional[str] = None,
        referer: Optional[str] = None,
    ) -> Tuple[List[Review], int, bool]:
        """
        Fetches a single page of reviews from Nykaa's reviews API.
        Returns:
            (reviews, total_count, is_partition_exhausted)
        """
        self._ensure_session()
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

        reviews: List[Review] = []
        total_count = 0

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(api_url, params=params, headers=headers, timeout=self.timeout)

                if resp.status_code == 200:
                    data = resp.json()
                    total_count = data.get("reviewCount", 0)

                    resp_obj = data.get("response", {})
                    raw_items = resp_obj.get("reviewData", []) or resp_obj.get("data", []) or data.get("reviews", [])

                    for item in raw_items:
                        rev = Review(
                            id=str(item.get("id", "")),
                            child_id=str(item.get("childId", "")) if item.get("childId") else None,
                            title=clean_text(item.get("title", "")),
                            description=clean_text(item.get("description", "")),
                            reviewer_name=clean_text(item.get("name", "Anonymous")),
                            rating=int(item.get("rating", 5)),
                            created_on=str(item.get("createdOn", "")),
                            relative_date=str(item.get("reviewCreationTextV2") or item.get("reviewCreationText", "")),
                            is_buyer=bool(item.get("isBuyer", False)),
                            label=clean_text(item.get("label", "")),
                            like_count=int(item.get("likeCount", 0)),
                            images=item.get("images", []) or [],
                            customer_profile_url=item.get("customerProfileUrl", ""),
                            user_review_count=int(item.get("userReviewCount", 0)),
                            user_image_count=int(item.get("userImageCount", 0)),
                            pro_user=bool(item.get("proUser", False)),
                            aspects=item.get("aspects", []) or [],
                        )
                        reviews.append(rev)

                    # If empty list returned, partition has ended
                    is_exhausted = len(reviews) == 0
                    return reviews, total_count, is_exhausted

                elif resp.status_code == 400:
                    # Nykaa's API returns 400 when pageNo exceeds maximum allowed page (e.g. page > 40)
                    logger.debug(f"Reached API pagination ceiling at page {page_no} (Status 400)")
                    return reviews, total_count, True

                elif resp.status_code == 429:
                    wait_time = 1.8 * attempt + random.uniform(0.5, 1.5)
                    logger.warning(f"Rate limited (429) on page {page_no}. Retrying in {wait_time:.1f}s (Attempt {attempt}/{self.max_retries})...")
                    if attempt >= 3:
                        # Refresh cookies/session if repeatedly hitting rate limits
                        self._ensure_session(force_refresh=True)
                    time.sleep(wait_time)

                else:
                    logger.warning(f"Reviews API returned HTTP {resp.status_code} for page {page_no}")
                    time.sleep(1.5)

            except Exception as e:
                logger.warning(f"Request error on page {page_no} (Attempt {attempt}): {e}")
                time.sleep(1.5)

        return reviews, total_count, False

    def scrape(
        self,
        target: str,
        max_reviews: Optional[int] = 50,
        sort: str = "MOST_RECENT",
        filter_by: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int, int], None]] = None,
    ) -> ScrapeResult:
        """
        High-level scraping method with Multi-Partition Deep Scraping.
        Overcomes Nykaa's 40-page API ceiling by partitioning across star ratings
        and sort algorithms, deduplicating reviews by review ID.
        """
        self._ensure_session()

        # Step 1: Resolve Target to Product ID and Canonical URL
        primary_id, canonical_url, alt_id = extract_product_id(target)

        if not primary_id:
            logger.info(f"Target '{target}' is not a direct URL/ID. Searching Nykaa...")
            search_matches = self.search_products(target, limit=1)
            if not search_matches:
                raise ValueError(f"Could not find any product matching search term: '{target}'")
            first_match = search_matches[0]
            primary_id = first_match["id"]
            canonical_url = first_match["url"]
            logger.info(f"Resolved search to product: '{first_match['name']}' (ID: {primary_id})")

        logger.info(f"Fetching product information for ID: {primary_id}...")
        product_info = self.get_product_info(primary_id, canonical_url)

        # Step 2: Determine Scraping Strategy (Single Partition vs Deep Multi-Partition)
        active_id = primary_id
        seen_ids: Set[str] = set()
        all_reviews: List[Review] = []
        total_available = 0

        # Construct partition sequence
        # If user explicitly asked for a specific star rating (e.g. STAR_5), partition through all sorts for that rating
        # If user wants all ratings or large limit, partition through all star ratings and sorts
        partitions: List[Tuple[Optional[str], str]] = []

        if filter_by and filter_by != "ALL":
            # Specific filter requested by user
            for s in [sort, "MOST_HELPFUL", "MOST_RECENT", "POSITIVE_FIRST", "NEGATIVE_FIRST"]:
                if (filter_by, s) not in partitions:
                    partitions.append((filter_by, s))
        else:
            # Full deep crawl:
            # When scraping all or high volumes, start with MOST_HELPFUL (yields 20 reviews per page!)
            primary_sort = sort or "MOST_HELPFUL"
            if max_reviews is None or max_reviews > 100:
                partitions.append((None, "MOST_HELPFUL"))
                if primary_sort != "MOST_HELPFUL":
                    partitions.append((None, primary_sort))
            else:
                partitions.append((None, primary_sort))

            if (None, "NEGATIVE_FIRST") not in partitions:
                partitions.append((None, "NEGATIVE_FIRST"))
            if (None, "MOST_RECENT") not in partitions:
                partitions.append((None, "MOST_RECENT"))
            # 4. Each individual star rating (enables accessing reviews beyond the 40-page ceiling)
            for star in ["STAR_5", "STAR_4", "STAR_3", "STAR_2", "STAR_1"]:
                partitions.append((star, "MOST_RECENT"))
                partitions.append((star, "MOST_HELPFUL"))
            # 5. Verified buyers & with images partitions
            partitions.append(("VERIFIED_BUYER", "MOST_RECENT"))
            partitions.append(("WITH_IMAGES", "MOST_RECENT"))

        logger.info(f"Starting review scraping for '{product_info.name}' (Target limit: {max_reviews or 'All'})...")

        for partition_idx, (p_filter, p_sort) in enumerate(partitions):
            if max_reviews and len(all_reviews) >= max_reviews:
                break

            filter_name = p_filter or "ALL"
            logger.info(f"--- Crawling Partition {partition_idx + 1}/{len(partitions)} (Filter: {filter_name}, Sort: {p_sort}) ---")

            page_no = 1
            consecutive_zero_pages = 0

            while page_no <= 45:  # Nykaa caps pageNo at ~40
                if max_reviews and len(all_reviews) >= max_reviews:
                    break

                reviews_page, total_count, is_exhausted = self.fetch_reviews_page(
                    product_id=active_id,
                    page_no=page_no,
                    sort=p_sort,
                    filter_by=p_filter,
                    referer=product_info.url,
                )

                # Check alternate ID fallback on first page if primary ID returns nothing
                if page_no == 1 and not reviews_page and alt_id and alt_id != primary_id:
                    logger.info(f"Primary ID {primary_id} returned 0 reviews. Trying alternate ID {alt_id}...")
                    reviews_page, total_count, is_exhausted = self.fetch_reviews_page(
                        product_id=alt_id,
                        page_no=page_no,
                        sort=p_sort,
                        filter_by=p_filter,
                        referer=product_info.url,
                    )
                    if reviews_page:
                        active_id = alt_id
                        product_info.id = active_id

                if total_count and not total_available:
                    total_available = total_count
                    product_info.total_reviews_count = total_count

                if is_exhausted or not reviews_page:
                    consecutive_zero_pages += 1
                    if consecutive_zero_pages >= 2:
                        break
                    page_no += 1
                    continue
                else:
                    consecutive_zero_pages = 0

                # Deduplicate by unique review ID
                new_added = 0
                for rev in reviews_page:
                    if rev.id not in seen_ids:
                        seen_ids.add(rev.id)
                        all_reviews.append(rev)
                        new_added += 1

                logger.info(f"Page {page_no}: +{len(reviews_page)} fetched ({new_added} unique new). Total collected: {len(all_reviews)}/{total_available or '?'}")

                if progress_callback:
                    progress_callback(len(all_reviews), total_available, page_no)

                if max_reviews and len(all_reviews) >= max_reviews:
                    all_reviews = all_reviews[:max_reviews]
                    break

                page_no += 1
                time.sleep(random.uniform(self.delay_range[0], self.delay_range[1]))

        # Step 3: Compute Summary Statistics
        total_scraped = len(all_reviews)
        star_counts = {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0}
        verified_count = 0
        with_images_count = 0
        rating_sum = 0

        for r in all_reviews:
            star_key = str(r.rating)
            if star_key in star_counts:
                star_counts[star_key] += 1
            rating_sum += r.rating
            if r.is_buyer:
                verified_count += 1
            if r.images:
                with_images_count += 1

        avg_rating = round(rating_sum / total_scraped, 2) if total_scraped > 0 else 0.0

        summary = RatingSummary(
            total_scraped=total_scraped,
            average_rating=avg_rating,
            star_distribution=star_counts,
            verified_buyer_count=verified_count,
            with_images_count=with_images_count,
        )

        logger.info(f"Scraping completed! Successfully extracted {total_scraped} unique reviews for '{product_info.name}'.")

        return ScrapeResult(
            product=product_info,
            summary=summary,
            reviews=all_reviews,
            scraped_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
