"""
Data models for Nykaa Review Scraper.
"""

from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any


@dataclass
class Review:
    id: str
    child_id: Optional[str] = None
    title: str = ""
    description: str = ""
    reviewer_name: str = "Anonymous"
    rating: int = 5
    created_on: str = ""
    relative_date: str = ""
    is_buyer: bool = False
    label: str = ""
    like_count: int = 0
    customer_profile_url: str = ""
    user_review_count: int = 0
    pro_user: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProductInfo:
    id: str
    name: str = ""
    brand: str = ""
    url: str = ""
    image_url: str = ""
    price: Optional[float] = None
    currency: str = "INR"
    availability: str = ""
    total_reviews_count: int = 0
    total_ratings_count: int = 0
    average_rating: Optional[float] = None
    parent_id: Optional[str] = None
    variant_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RatingSummary:
    total_scraped: int = 0
    total_available: int = 0
    average_rating: float = 0.0
    star_distribution: Dict[str, int] = field(default_factory=lambda: {
        "5": 0, "4": 0, "3": 0, "2": 0, "1": 0
    })
    verified_buyer_count: int = 0
    warning: str = ""  # set when the scrape ended early or pages could not be fetched

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScrapeResult:
    product: ProductInfo
    summary: RatingSummary
    reviews: List[Review] = field(default_factory=list)
    scraped_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "product": self.product.to_dict(),
            "summary": self.summary.to_dict(),
            "reviews": [r.to_dict() for r in self.reviews],
            "scraped_at": self.scraped_at,
        }
