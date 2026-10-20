"""
Utility functions for URL parsing, ID extraction, and text cleaning.
"""

import re
import html
import logging
from urllib.parse import urlparse, parse_qs
from typing import Tuple, Optional, Any

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
    # Accept links pasted without a scheme, e.g. "www.nykaa.com/.../p/123"
    if not re.match(r"^[a-z][a-z0-9+.-]*://", input_str, re.I) and re.match(r"^[\w.-]+\.[a-z]{2,}/", input_str, re.I):
        input_str = "https://" + input_str
    parsed = urlparse(input_str)
    query_params = parse_qs(parsed.query)

    query_product_id = None
    if "productId" in query_params:
        query_product_id = query_params["productId"][0]

    path_match = re.search(r"/p/(\d+)(?:/|$)", parsed.path)
    path_product_id = path_match.group(1) if path_match else None

    # Determine canonical clean URL
    base_url = f"https://{parsed.netloc or 'www.nykaa.com'}{parsed.path}"

    if query_product_id and path_product_id:
        # If query productId differs from path, query_product_id is often the parent product
        return query_product_id, base_url, path_product_id
    elif path_product_id:
        return path_product_id, base_url, None
    elif query_product_id:
        return query_product_id, base_url, None

    # Case 3: Fallback search for any string of 5-10 digits in input
    # (only for non-URLs: short/share links are resolved by following redirects)
    if parsed.netloc:
        return None, None, None
    digit_match = re.search(r"\b(\d{5,10})\b", input_str)
    if digit_match:
        found_id = digit_match.group(1)
        return found_id, f"https://www.nykaa.com/product/p/{found_id}", None

    return None, None, None


_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>|</p>", re.I)
# Control characters break Excel export (openpyxl IllegalCharacterError); keep \t and \n
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(text: Any) -> str:
    """Cleans up text: unescapes HTML entities, strips tags/control chars and surrounding quotes/spaces."""
    if text is None:
        return ""
    text = str(text)
    if not text:
        return ""
    text = html.unescape(text)
    text = _BR_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL_RE.sub("", text)
    text = text.strip()
    if len(text) >= 2 and ((text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'"))):
        text = text[1:-1].strip()
    return text


def safe_int(value: Any, default: int = 0) -> int:
    """int() that tolerates None, floats-as-strings ("4.0") and garbage."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default
