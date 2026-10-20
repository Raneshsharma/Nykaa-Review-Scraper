# Nykaa Review Scraper 💄✨

A powerful, high-performance Python package, CLI tool, and web application to scrape customer reviews, ratings, product metadata, and customer photos for **any product on Nykaa**.

Nykaa protects its platform with **Akamai Bot Manager & Edge WAF**. Standard HTTP clients like `requests` or `urllib` receive `403 Forbidden` or `429 Too Many Requests`. This scraper uses browser TLS fingerprint impersonation (`curl_cffi` Chrome 120) and automated session cookie initialization to seamlessly bypass Akamai anti-bot mitigations.

---

## 🌟 Key Features

- **Akamai WAF & Bot Mitigation Bypass**: Uses TLS Client Hello impersonation (Chrome 120) and automated cookie handling.
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
  - Customer Uploaded Review Photos
  - Reviewer Total Reviews & Pro User status
- **Analytics & Summary**:
  - Average rating calculation
  - Star distribution breakdown (5★, 4★, 3★, 2★, 1★)
  - Verified Buyer percentage
  - Total reviews with photos
- **Multi-Format Export**:
  - **CSV**: Encoded in `utf-8-sig` for seamless opening in Microsoft Excel without garbled Unicode/emojis.
  - **Excel (.xlsx)**: Formatted workbook with styled Nykaa pink header fill, borders, and a dedicated **Product Summary** sheet.
  - **JSON**: Structured JSON dataset ready for NLP/LLM sentiment analysis.
- **Sorting & Filtering**:
  - Sort: Most Recent, Most Useful, Highest Rated, Lowest Rated
  - Filter: All, 5 Stars, 4 Stars, 3 Stars, 2 Stars, 1 Star, Verified Buyers, With Images
- **Modern Web Dashboard**:
  - Interactive web UI built with FastAPI and Vanilla CSS.
  - Real-time live scraper progress, product preview card, star breakdown charts, searchable review cards, photo lightbox, and one-click export downloads.

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
# Scrape 50 reviews by product ID and export to CSV, Excel, and JSON:
python main.py "27905374" --limit 50 --format all

# Scrape using full Nykaa product URL:
python main.py "https://www.nykaa.com/plum-black-rice-niacinamide-txa-10percent-night-clarity-jelly-serum/p/27905374" --limit 100
```

#### Scrape by Product Search Query:
```bash
python main.py --search "Maybelline Super Stay Matte Ink" --limit 30 --sort MOST_HELPFUL
```

#### Filter by Star Rating:
```bash
# Only scrape 5-star reviews:
python main.py "27905374" --filter STAR_5 --limit 50

# Only scrape reviews from verified buyers:
python main.py "27905374" --filter VERIFIED_BUYER --limit 50
```

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
- **Live Scraper Progress**: Watch the scraper bypass WAF, fetch pages, and extract data.
- **Interactive Review Browser**: Live search and filter within scraped reviews.
- **Customer Photo Lightbox**: Click any customer review thumbnail to zoom.
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
    max_reviews=50,
    sort="MOST_RECENT"
)

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
| `sort` | `MOST_RECENT`, `MOST_HELPFUL`, `POSITIVE_FIRST`, `NEGATIVE_FIRST` | Review ordering |
| `filter_by` | `ALL`, `STAR_5`, `STAR_4`, `STAR_3`, `STAR_2`, `STAR_1`, `VERIFIED_BUYER`, `WITH_IMAGES` | Filter criteria |
| `max_reviews` | Integer or `None` (0 for all) | Review count ceiling |
| `format` | `csv`, `excel`, `json`, `all` | Export file types |
