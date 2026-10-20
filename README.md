# Nykaa Review Scraper 💄✨

A powerful, high-performance Python package, CLI tool, and web application to scrape customer reviews, ratings, product metadata, and customer photos for **any product on Nykaa**.

Nykaa protects its platform with **Akamai Bot Manager & Edge WAF**. Standard HTTP clients like `requests` or `urllib` receive `403 Forbidden` or `429 Too Many Requests`. This scraper uses browser TLS fingerprint impersonation (`curl_cffi`, current Chrome/Safari/Edge fingerprints) and automated session cookie initialization to get past Akamai's bot checks. Outdated fingerprints such as Chrome 120 are now blocked, so the scraper picks the first fingerprint Akamai accepts.

---

## 🌟 Key Features

- **Akamai WAF & Bot Mitigation Bypass**: Uses TLS Client Hello impersonation (current browser fingerprints, with automatic fallback) and automated cookie handling.
- **Scrape Any Product**:
  - Full Nykaa Product URL (e.g. `https://www.nykaa.com/.../p/27905374`)
  - Numeric Product ID (e.g. `27905374`)
  - Search Query (e.g. `"Kay Beauty Matte Lipstick"`, `"Dot & Key Vitamin C"`)
- **Rich Data Fields**:
  - Review ID & Variant ID
  - Star Rating (1–5)
  - Review Title & Full Comment Text
  - Reviewer Name & Verified Buyer status
  - Review Date & Relative Date (e.g., *"Today"*, *"5 days ago"*)
  - Helpful Upvotes Count
  - Reviewer Total Reviews & Pro User status
- **Analytics & Summary**:
  - Average rating calculation
  - Star distribution breakdown (5★, 4★, 3★, 2★, 1★)
  - Verified Buyer percentage
  - Coverage: scraped reviews vs. the total Nykaa reports
- **Multi-Format Export**:
  - **CSV**: Encoded in `utf-8-sig` for seamless opening in Microsoft Excel without garbled Unicode/emojis.
  - **Excel (.xlsx)**: Formatted workbook with styled Nykaa pink header fill, borders, and a dedicated **Product Summary** sheet.
  - **JSON**: Structured JSON dataset ready for NLP/LLM sentiment analysis.
- **Sorting & Filtering**:
  - Sort: Most Recent, Most Helpful, Most Useful, Highest Rated, Lowest Rated
  - Filter: All, 5 Stars, 4 Stars, 3 Stars, 2 Stars, 1 Star
- **Modern Web Dashboard**:
  - Interactive web UI built with FastAPI and Vanilla CSS.
  - Real-time scraper progress (reviews collected / total), product preview card, star breakdown charts, searchable review cards, and one-click export downloads.

### How "scrape everything" works

Nykaa's reviews API (verified against the live site) serves 20 reviews per page and **stops at page 40**, so a single query can never return more than 800 reviews. To collect everything, the scraper splits the product into buckets small enough to crawl completely:

1. **All reviews**: if the product has ≤ 800 reviews, one pass collects them.
2. **Per star rating** (`filters=STAR_5` …): used when the product is bigger.
3. **Per star rating per variant** (`childIds=<shade/size>`): used when one star rating alone exceeds 800. Variants come from the product page *and* from every review collected, so discontinued shades are found too.

Each bucket reports its own review count, so the scraper knows when a bucket is complete and doesn't waste requests on it. Very large buckets get a full pass in each sort order that reaches a different window of reviews (Most Useful, Most Helpful, Most Recent).

- **Fast**: pages download in parallel (`--workers`, default 12), each worker with its own browser-impersonated session. Random `429` rate limits are retried on the spot.
- **No silent gaps**: failed pages are retried with back-off and a fresh session, then again in a final recovery pass; if Nykaa keeps refusing, scraping stops early with a warning instead of hanging.
- **Honest coverage**: Nykaa only serves verified-buyer reviews, and its review count includes reviews it never shows (about 10% on the products tested; the website can't show them either). The summary shows `scraped / reported` and explains the gap.

Measured on the live site (October 2026, 12 workers):

| Product | Nykaa's count | Collected | Time |
|---|---|---|---|
| Lakme Absolute Kohl Kajal (294157) | 428 | 385 (all Nykaa serves) | 5 s |
| Dot & Key Vitamin C serum (7329368) | 2,332 | 1,584 (matches an exhaustive brute-force crawl) | 22 s |
| Lakme Eyeconic Kajal (18430) | 5,428 | 4,920 (matches an exhaustive crawl) | 2 min |
| Maybelline Super Stay Matte Ink (229917) | 16,418 | 14,967 | 3 min |

---

## 🚀 Quick Start

### 1. Installation

Activate the virtual environment:

```powershell
# On Windows
.venv\Scripts\activate

# Or install dependencies
pip install -r requirements.txt
```

### 2. Command Line Usage (CLI)

#### Direct Scrape with URL or Product ID:
```bash
# Scrape ALL reviews (default) from a product URL and export to CSV, Excel, and JSON:
python main.py "https://www.nykaa.com/plum-black-rice-niacinamide-txa-10percent-night-clarity-jelly-serum/p/27905374"

# Only the first 100 reviews, by product ID, CSV only, into a custom file:
python main.py "27905374" --limit 100 --format csv --output exports/plum.csv

# Fewer parallel downloads if you see many 429 rate-limit warnings:
python main.py "27905374" --workers 4
```

#### Scrape by Product Search Query:
```bash
python main.py --search "Maybelline Super Stay Matte Ink" --limit 30 --sort MOST_HELPFUL
```

#### Filter by Star Rating:
```bash
# Only scrape 5-star reviews:
python main.py "27905374" --filter STAR_5 --limit 50

# All 1-star reviews:
python main.py "27905374" --filter STAR_1
```

#### Troubleshooting: diagnose the API
If a product's coverage is below 100%, or scraping fails, run:
```bash
python main.py "<product link>" --diagnose
```
It probes Nykaa's reviews API for that product (page size, page ceiling, per-filter counts, how much the sort orders overlap, accepted parameters) and saves the report to `output/diagnose_<id>.json`. Share that file when reporting a problem.

#### Interactive CLI Mode:
Run without arguments to enter the guided step-by-step CLI:
```bash
python main.py
```

---

## 🌐 Web Dashboard UI

Launch the modern browser dashboard:

```bash
python app.py
# or
python main.py --ui
```

Then open your browser to **`http://localhost:8000`**.

Features available in the UI:
- **Instant Product Search**: Type any brand or product name for live auto-suggestions.
- **Live Scraper Progress**: Real progress bar (reviews collected vs. Nykaa's total) and a Stop button that keeps the reviews collected so far. Scrapes run in the background, so large products don't time out.
- **Interactive Review Browser**: Live search and filter within scraped reviews.
- **One-Click Downloads**: Direct download buttons for CSV, Excel (.xlsx), and JSON.

---

## 🐍 Python Library Usage

You can also use the scraper directly in your Python code:

```python
from nykaa_scraper import NykaaScraper
from nykaa_scraper.exporter import export_to_csv, export_to_excel, export_to_json

# Initialize scraper
scraper = NykaaScraper()

# Scrape reviews for any product
result = scraper.scrape(
    target="https://www.nykaa.com/plum-black-rice-niacinamide-txa-10percent-night-clarity-jelly-serum/p/27905374",
    max_reviews=None,  # None = all reviews
    sort="MOST_RECENT"
)
print(f"Scraped {result.summary.total_scraped} of {result.summary.total_available} reviews")

# Product metadata
print("Product:", result.product.name)
print("Brand:", result.product.brand)
print("Price:", result.product.price)
print("Average Rating:", result.summary.average_rating)
print("Star Breakdown:", result.summary.star_distribution)

# Iterate through reviews
for review in result.reviews[:5]:
    print(f"[{review.rating}★] {review.reviewer_name} ({review.relative_date}): {review.title}")
    print(f"Comment: {review.description}\n")

# Export to files
export_to_csv(result, "reviews.csv")
export_to_excel(result, "reviews.xlsx")
export_to_json(result, "reviews.json")
```

---

## 🧪 Tests

The test suite runs the scraper against a local mock of Nykaa's reviews API (page ceiling, per-filter counts, rate limits, flaky errors, HTML challenge pages, malformed fields), so it needs no network access:

```bash
pip install pytest httpx
python -m pytest tests
```

---

## 📂 Project Structure

```
Nykaa Scrapper/
├── nykaa_scraper/
│   ├── __init__.py           # Package exports
│   ├── scraper.py            # Core NykaaScraper engine (WAF bypass & pagination)
│   ├── models.py             # Review, ProductInfo, RatingSummary dataclasses
│   ├── exporter.py           # CSV, Excel, and JSON exporters
│   └── utils.py              # URL extraction, text cleaning, and logging
├── static/
│   ├── css/
│   │   └── style.css         # Modern dark glassmorphism stylesheet
│   └── js/
│       └── app.js            # Frontend application logic
├── templates/
│   └── index.html            # Web dashboard HTML5 template
├── tests/                    # Pytest suite + mock Nykaa API
├── output/                   # Auto-generated scraped data exports
├── app.py                    # FastAPI web application
├── main.py                   # CLI entrypoint with flags & interactive mode
├── requirements.txt          # Python dependencies
└── README.md                 # Project documentation
```

---

## ⚙️ Configuration & Options

| Parameter | Options | Description |
|---|---|---|
| `sort` | `MOST_RECENT`, `MOST_HELPFUL`, `MOST_USEFUL`, `POSITIVE_FIRST`, `NEGATIVE_FIRST` | Order of the returned reviews |
| `filter_by` | `ALL`, `STAR_5`, `STAR_4`, `STAR_3`, `STAR_2`, `STAR_1` | Filter criteria |
| `max_reviews` / `--limit` | Integer, or `None` / `0` for all (default) | Review count ceiling |
| `workers` / `--workers` | Integer (default 12) | Parallel page downloads |
| `format` | `csv`, `excel`, `json`, `all` | Export file types |
| `--output` | Directory or file path | Where exports are written (default `output/`) |
