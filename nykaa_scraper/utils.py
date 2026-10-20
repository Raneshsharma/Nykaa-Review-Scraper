"""
Utility functions for URL parsing, ID extraction, and text cleaning.
"""

import re
import html
import logging
from urllib.parse import urlparse, parse_qs
from typing import Tuple, Optional

# Setup standard logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("NykaaScraper")


def extract_product_id(input_str: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Extracts product ID and query product ID from a Nykaa URL or numeric ID string.

    Returns:
        (primary_id, canonical_url, alternate_id)
        Where primary_id is usually the path ID or query productId,
        canonical_url is the base product page,
        and alternate_id is an alternative ID to try if the first returns 429/404.
    """
    input_str = input_str.strip()

    # Case 1: Pure numeric ID provided
    if input_str.isdigit():
        return input_str, f"https://www.nykaa.com/product/p/{input_str}", None

    # Case 2: Nykaa URL
    # Examples:
    # https://www.nykaa.com/dot-key-vitamin-c-glow-serum/p/509378?skuId=509376
    # https://www.nykaa.com/minimalist-niacinamide-5-serum/p/1068001?productId=22050679&skuId=1068001
    parsed = urlparse(input_str)
    query_params = parse_qs(parsed.query)

    query_product_id = None
    if "productId" in query_params:
        query_product_id = query_params["productId"][0]

    path_match = re.search(r"/p/(\d+)", parsed.path)
    path_product_id = path_match.group(1) if path_match else None

    # Determine canonical clean URL
    base_url = f"{parsed.scheme or 'https'}://{parsed.netloc or 'www.nykaa.com'}{parsed.path}"

    if query_product_id and path_product_id:
        # If query productId differs from path, query_product_id is often the parent product
        return query_product_id, base_url, path_product_id
    elif path_product_id:
        return path_product_id, base_url, None
    elif query_product_id:
        return query_product_id, base_url, None

    # Case 3: Fallback search for any string of 5-10 digits in input
    digit_match = re.search(r"\b(\d{5,10})\b", input_str)
    if digit_match:
        found_id = digit_match.group(1)
        return found_id, f"https://www.nykaa.com/product/p/{found_id}", None

    return None, None, None


def clean_text(text: Optional[str]) -> str:
    """Cleans up text, unescapes HTML entities, and strips surrounding quotes/spaces."""
    if not text:
        return ""
    text = html.unescape(text)
    text = text.strip()
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
    return text
