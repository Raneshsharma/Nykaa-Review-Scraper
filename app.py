"""
FastAPI Web Application for Nykaa Review Scraper.
Provides a REST API and a modern web dashboard.
"""

import uuid
import shutil
import tempfile
import threading
from typing import Optional, Dict, Any
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.background import BackgroundTask

from nykaa_scraper import NykaaScraper, ScrapeResult, ScrapeCancelled
from nykaa_scraper.exporter import export_to_csv, export_to_excel, export_to_json
from nykaa_scraper.utils import logger

# Setup app
app = FastAPI(title="Nykaa Review Scraper", version="1.1.0")

# Setup paths
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# In-memory storage for finished results (by result_id) and running jobs (by job_id)
MAX_CACHED_RESULTS = 20
MAX_FINISHED_JOBS = 50
scrape_cache: Dict[str, ScrapeResult] = {}
jobs: Dict[str, Dict[str, Any]] = {}
jobs_lock = threading.Lock()
search_scraper = NykaaScraper()
search_lock = threading.Lock()


class ScrapeRequest(BaseModel):
    target: str
    limit: Optional[int] = None
    sort: Optional[str] = "MOST_RECENT"
    filter_by: Optional[str] = None


@app.get("/", response_class=FileResponse)
async def serve_home():
    """Serves the main web dashboard."""
    return FileResponse(TEMPLATES_DIR / "index.html")


@app.get("/api/search")
def search_nykaa(q: str = Query(..., min_length=2)):
    """Searches Nykaa for products by query text."""
    try:
        with search_lock:
            products = search_scraper.search_products(q, limit=6)
        return {"success": True, "query": q, "products": products}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


def _run_job(job_id: str, target: str, limit: Optional[int], sort: str, filter_val: Optional[str]):
    job = jobs[job_id]

    def progress(collected, total, pages):
        job.update(collected=collected, total=total, pages=pages)

    def prune_jobs():
        """Forget the oldest finished jobs (and their cached results) once there are too many."""
        with jobs_lock:
            finished = [jid for jid, j in jobs.items() if j["status"] != "running"]
            for jid in finished[:-MAX_FINISHED_JOBS]:
                jobs.pop(jid, None)
                scrape_cache.pop(jid, None)

    try:
        # A fresh scraper per job: sessions and cooldown state are not shared between scrapes
        result = NykaaScraper().scrape(
            target=target,
            max_reviews=limit,
            sort=sort,
            filter_by=filter_val,
            progress_callback=progress,
            cancel_event=job["cancel"],
        )

        result_id = job_id
        with jobs_lock:
            scrape_cache[result_id] = result
            while len(scrape_cache) > MAX_CACHED_RESULTS:
                scrape_cache.pop(next(iter(scrape_cache)))

        # Also save to output folder; a failed save must not throw away a finished scrape
        base_name = f"nykaa_reviews_{result.product.id}_{result_id}"
        try:
            export_to_csv(result, str(OUTPUT_DIR / f"{base_name}.csv"))
            export_to_excel(result, str(OUTPUT_DIR / f"{base_name}.xlsx"))
            export_to_json(result, str(OUTPUT_DIR / f"{base_name}.json"))
        except Exception:
            logger.exception("Could not save exports to the output folder")

        job.update(status="done", result_id=result_id, collected=result.summary.total_scraped)
    except ScrapeCancelled:
        job.update(status="cancelled")
    except Exception as e:
        logger.exception("Scrape job failed")
        job.update(status="error", error=str(e) or e.__class__.__name__)
    finally:
        prune_jobs()


@app.post("/api/scrape")
def scrape_reviews(payload: ScrapeRequest):
    """Starts a background scrape job for a Nykaa product URL, ID, or query. Poll /api/jobs/{job_id}."""
    target = payload.target.strip()
    if not target:
        raise HTTPException(status_code=400, detail="Target product is required.")

    limit = payload.limit
    if limit is not None and limit <= 0:
        limit = None  # None indicates all reviews

    sort_order = payload.sort or "MOST_RECENT"
    filter_val = payload.filter_by if payload.filter_by and payload.filter_by != "ALL" else None

    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "status": "running", "collected": 0, "total": 0, "pages": 0,
        "result_id": None, "error": None, "cancel": threading.Event(),
    }
    threading.Thread(
        target=_run_job, args=(job_id, target, limit, sort_order, filter_val), daemon=True
    ).start()
    return {"success": True, "job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    """Progress of a scrape job; includes the full result once finished."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    body = {k: v for k, v in job.items() if k != "cancel"}
    if job["status"] == "done":
        if job["result_id"] in scrape_cache:
            body["data"] = scrape_cache[job["result_id"]].to_dict()
        else:
            body.update(status="error", error="This result has expired. Please scrape again.")
    return body


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Stops a running job early; the reviews collected so far are kept."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    job["cancel"].set()
    return {"success": True}


@app.get("/api/download/{result_id}/{file_format}")
def download_file(result_id: str, file_format: str):
    """Downloads scraped reviews in CSV, Excel, or JSON format."""
    if result_id not in scrape_cache:
        raise HTTPException(status_code=404, detail="Scrape result not found or expired.")

    result = scrape_cache[result_id]
    fmt = file_format.lower().replace(".", "")
    if fmt not in ("csv", "xlsx", "excel", "json"):
        raise HTTPException(status_code=400, detail="Invalid format. Supported: csv, excel, json.")

    # A unique folder per download, so concurrent downloads never overwrite each other's file
    temp_dir = Path(tempfile.mkdtemp(prefix="nykaa_dl_"))
    sanitized_name = "".join(c for c in result.product.name if c.isalnum() or c in (" ", "_", "-")).rstrip()[:40]
    filename_base = f"nykaa_{result.product.id}_{sanitized_name}".replace(" ", "_")

    if fmt == "csv":
        out_file = temp_dir / f"{filename_base}.csv"
        export_to_csv(result, str(out_file))
        return FileResponse(
            out_file,
            background=BackgroundTask(shutil.rmtree, temp_dir, True),
            filename=f"{filename_base}.csv",
            media_type="text/csv",
        )
    elif fmt in ("xlsx", "excel"):
        out_file = temp_dir / f"{filename_base}.xlsx"
        export_to_excel(result, str(out_file))
        return FileResponse(
            out_file,
            background=BackgroundTask(shutil.rmtree, temp_dir, True),
            filename=f"{filename_base}.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    elif fmt == "json":
        out_file = temp_dir / f"{filename_base}.json"
        export_to_json(result, str(out_file))
        return FileResponse(
            out_file,
            background=BackgroundTask(shutil.rmtree, temp_dir, True),
            filename=f"{filename_base}.json",
            media_type="application/json",
        )
    else:
        raise HTTPException(status_code=400, detail="Invalid format. Supported: csv, excel, json.")


def _bind(family: int, host: str, port: int):
    import socket
    sock = socket.socket(family, socket.SOCK_STREAM)
    if family == socket.AF_INET6:
        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
    sock.bind((host, port))
    return sock


def run_dashboard(port: int = 8000, open_browser: bool = True):
    """
    Starts the dashboard on this computer. Listens on both 127.0.0.1 and ::1, because browsers
    often try "localhost" over IPv6 first, and moves to the next free port if this one is taken.
    """
    import socket
    import webbrowser
    import uvicorn

    sockets = []
    for candidate in range(port, port + 20):
        try:
            sockets = [_bind(socket.AF_INET, "127.0.0.1", candidate)]
        except OSError:
            continue  # port in use: try the next one
        try:
            sockets.append(_bind(socket.AF_INET6, "::1", candidate))
        except OSError:
            pass  # no IPv6 on this machine: IPv4 is enough
        port = candidate
        break
    if not sockets:
        raise SystemExit(f"No free port between {port} and {port + 19}. Close other servers and try again.")

    url = f"http://localhost:{port}"
    print("\n" + "=" * 60)
    print(f"  Nykaa Review Scraper dashboard is running:  {url}")
    print(f"  (if that doesn't open, use http://127.0.0.1:{port})")
    print("  Press Ctrl+C to stop.")
    print("=" * 60 + "\n")
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    server.run(sockets=sockets)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nykaa Review Scraper web dashboard")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="Don't open the browser automatically")
    cli = parser.parse_args()
    run_dashboard(cli.port, open_browser=not cli.no_browser)
