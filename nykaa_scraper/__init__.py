"""
Nykaa Review Scraper Package
~~~~~~~~~~~~~~~~~~~~~~~~~~~
A robust, high-performance Python package to scrape customer reviews, ratings,
and product details for any product on Nykaa.
"""

from .scraper import NykaaScraper
from .models import Review, ProductInfo, RatingSummary, ScrapeResult

__all__ = ["NykaaScraper", "Review", "ProductInfo", "RatingSummary", "ScrapeResult"]
__version__ = "1.0.0"
