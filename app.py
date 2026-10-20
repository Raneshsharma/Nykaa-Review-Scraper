"""
FastAPI Web Application for Nykaa Review Scraper.
Provides a REST API and a modern web dashboard.
"""

import os
import uuid
import tempfile
from typing import Optional, Dict, Any
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from nykaa_scraper import NykaaScraper, ScrapeResult
from nykaa_scraper.exporter import export_to_csv, export_to_excel, export_to_json

# Setup app
app = FastAPI(title="Nykaa Review Scraper", version="1.0.0")

# Setup paths
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# In-memory storage for active scraped results (by result_id)
scrape_cache: Dict[str, ScrapeResult] = {}
scraper_instance = NykaaScraper()


class ScrapeRequest(BaseModel):
    target: str
    limit: Optional[int] = 50
    sort: Optional[str] = "MOST_RECENT"
    filter_by: Optional[str] = None


@app.get("/", response_class=FileResponse)
async def serve_home():
    """Serves the main web dashboard."""
    return FileResponse(TEMPLATES_DIR / "index.html")


@app.get("/api/search")
async def search_nykaa(q: str = Query(..., min_length=2)):
    """Searches Nykaa for products by query text."""
    try:
        products = scraper_instance.search_products(q, limit=6)
        return {"success": True, "query": q, "products": products}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@app.post("/api/scrape")
async def scrape_reviews(payload: ScrapeRequest):
    """Scrapes reviews for a specified Nykaa product URL, ID, or query."""
    target = payload.target.strip()
    if not target:
        raise HTTPException(status_code=400, detail="Target product is required.")

    limit = payload.limit
    if limit is not None and limit <= 0:
        limit = None  # None indicates all reviews

    sort_order = payload.sort or "MOST_RECENT"
    filter_val = payload.filter_by if payload.filter_by and payload.filter_by != "ALL" else None

    try:
        result = scraper_instance.scrape(
            target=target,
            max_reviews=limit,
            sort=sort_order,
            filter_by=filter_val,
        )

        result_id = str(uuid.uuid4())[:8]
        scrape_cache[result_id] = result

        # Also save to output folder
        base_name = f"nykaa_reviews_{result.product.id}_{result_id}"
        export_to_csv(result, str(OUTPUT_DIR / f"{base_name}.csv"))
        export_to_excel(result, str(OUTPUT_DIR / f"{base_name}.xlsx"))
        export_to_json(result, str(OUTPUT_DIR / f"{base_name}.json"))

        return {
            "success": True,
            "result_id": result_id,
            "data": result.to_dict(),
        }

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)},
        )


@app.get("/api/download/{result_id}/{file_format}")
async def download_file(result_id: str, file_format: str):
    """Downloads scraped reviews in CSV, Excel, or JSON format."""
    if result_id not in scrape_cache:
        raise HTTPException(status_code=404, detail="Scrape result not found or expired.")

    result = scrape_cache[result_id]
    fmt = file_format.lower().replace(".", "")

    temp_dir = Path(tempfile.gettempdir())
    sanitized_name = "".join(c for c in result.product.name if c.isalnum() or c in (" ", "_", "-")).rstrip()[:40]
    filename_base = f"nykaa_{result.product.id}_{sanitized_name}".replace(" ", "_")

    if fmt == "csv":
        out_file = temp_dir / f"{filename_base}.csv"
        export_to_csv(result, str(out_file))
        return FileResponse(
            out_file,
            filename=f"{filename_base}.csv",
            media_type="text/csv",
        )
    elif fmt in ("xlsx", "excel"):
        out_file = temp_dir / f"{filename_base}.xlsx"
        export_to_excel(result, str(out_file))
        return FileResponse(
            out_file,
            filename=f"{filename_base}.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    elif fmt == "json":
        out_file = temp_dir / f"{filename_base}.json"
        export_to_json(result, str(out_file))
        return FileResponse(
            out_file,
            filename=f"{filename_base}.json",
            media_type="application/json",
        )
    else:
        raise HTTPException(status_code=400, detail="Invalid format. Supported: csv, excel, json.")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
