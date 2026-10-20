"""
Nykaa Review Scraper CLI
~~~~~~~~~~~~~~~~~~~~~~~
Command-line interface to scrape reviews for any Nykaa product.
"""

import sys
import argparse
import subprocess
from datetime import datetime
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from nykaa_scraper import NykaaScraper
from nykaa_scraper.exporter import export_to_csv, export_to_excel, export_to_json


def print_banner():
    banner = """
===========================================================
              NYKAA PRODUCT REVIEW SCRAPER
   Bypasses Akamai WAF & Scrapes Reviews for Any Product
===========================================================
"""
    print(banner)


def print_summary(result):
    print("\n" + "=" * 60)
    print("                    SCRAPING SUMMARY")
    print("=" * 60)
    print(f"Product:         {result.product.name}")
    if result.product.brand:
        print(f"Brand:           {result.product.brand}")
    if result.product.price:
        print(f"Price:           ₹{result.product.price}")
    print(f"Product URL:     {result.product.url}")
    print("-" * 60)
    total = result.summary.total_available
    coverage = f" of {total} on Nykaa ({result.summary.total_scraped / total * 100:.1f}%)" if total else ""
    print(f"Total Scraped:   {result.summary.total_scraped} reviews{coverage}")
    print(f"Average Rating:  ★ {result.summary.average_rating} / 5.0")
    print(f"Verified Buyers: {result.summary.verified_buyer_count} ({round(result.summary.verified_buyer_count / max(result.summary.total_scraped, 1) * 100, 1)}%)")
    print("-" * 60)
    print("Star Rating Breakdown:")
    for star in ["5", "4", "3", "2", "1"]:
        count = result.summary.star_distribution.get(star, 0)
        pct = round(count / max(result.summary.total_scraped, 1) * 100)
        bar = "█" * (pct // 4)
        print(f"  {star} Star | {bar:<25} | {count:>4} ({pct:>2}%)")
    print("=" * 60)


def export_result(result, export_fmt, output=None):
    """
    Writes the result in the chosen format(s). `output` may be a directory or a file path
    (its extension is replaced per format); defaults to ./output/nykaa_reviews_<id>_<timestamp>.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_name = f"nykaa_reviews_{result.product.id}_{timestamp}"
    if output:
        out = Path(output)
        if out.suffix:
            out_dir, base_name = out.parent, out.stem
        else:
            out_dir, base_name = out, default_name
    else:
        out_dir, base_name = Path("output"), default_name
    out_dir.mkdir(parents=True, exist_ok=True)

    exported_paths = []
    if export_fmt in ("csv", "all"):
        exported_paths.append(export_to_csv(result, str(out_dir / f"{base_name}.csv")))
    if export_fmt in ("excel", "all"):
        exported_paths.append(export_to_excel(result, str(out_dir / f"{base_name}.xlsx")))
    if export_fmt in ("json", "all"):
        exported_paths.append(export_to_json(result, str(out_dir / f"{base_name}.json")))
    return exported_paths


def run_interactive():
    print_banner()
    print("Interactive Mode:")
    print("Enter a Nykaa Product URL, Product ID, or a product search term.")
    target = input("\nTarget (URL / ID / Search Query): ").strip()
    if not target:
        print("Error: Target cannot be empty. Exiting.")
        return

    limit_input = input("How many reviews to scrape? (press Enter for all): ").strip()
    limit = None
    if limit_input.isdigit() and int(limit_input) > 0:
        limit = int(limit_input)

    print("\nSort options:")
    print("  1) Most Recent (default)")
    print("  2) Most Useful / Helpful")
    print("  3) Highest Rated First")
    print("  4) Lowest Rated First")
    sort_choice = input("Select sort option [1-4]: ").strip()
    sort_map = {"1": "MOST_RECENT", "2": "MOST_HELPFUL", "3": "POSITIVE_FIRST", "4": "NEGATIVE_FIRST"}
    sort = sort_map.get(sort_choice, "MOST_RECENT")

    print("\nFilter options:")
    print("  1) All Reviews (default)")
    print("  2) 5 Stars Only")
    print("  3) 4 Stars Only")
    print("  4) 3 Stars Only")
    print("  5) 2 Stars Only")
    print("  6) 1 Star Only")
    print("  7) Verified Buyers Only")
    filter_choice = input("Select filter [1-7]: ").strip()
    filter_map = {
        "1": "ALL", "2": "STAR_5", "3": "STAR_4", "4": "STAR_3",
        "5": "STAR_2", "6": "STAR_1", "7": "VERIFIED_BUYER"
    }
    filter_by = filter_map.get(filter_choice, "ALL")

    print("\nExport formats:")
    print("  1) CSV & Excel & JSON (All)")
    print("  2) CSV only (default)")
    print("  3) Excel (.xlsx) only")
    print("  4) JSON only")
    fmt_choice = input("Select export format [1-4]: ").strip()
    fmt_map = {"1": "all", "2": "csv", "3": "excel", "4": "json"}
    export_fmt = fmt_map.get(fmt_choice, "csv")

    scraper = NykaaScraper()
    print("\nFetching reviews, please wait...\n")

    def progress(scraped, total, pages):
        tot_str = str(total) if total else "?"
        print(f"\r-> Scraped {scraped}/{tot_str} reviews ({pages} pages fetched)", end="", flush=True)

    try:
        result = scraper.scrape(
            target=target,
            max_reviews=limit,
            sort=sort,
            filter_by=filter_by if filter_by != "ALL" else None,
            progress_callback=progress,
        )
        print()
        print_summary(result)

        exported_paths = export_result(result, export_fmt)
        print("\nExported Files:")
        for p in exported_paths:
            print(f" -> {Path(p).resolve()}")

    except Exception as e:
        print(f"\n[Error] {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Nykaa Review Scraper - Extract customer reviews from any Nykaa product."
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Nykaa Product URL, numeric Product ID, or search query.",
    )
    parser.add_argument(
        "--search", "-s",
        type=str,
        help="Explicit search query to look up on Nykaa.",
    )
    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=0,
        help="Maximum reviews to scrape (default: 0 = all reviews).",
    )
    parser.add_argument(
        "--sort",
        choices=["MOST_RECENT", "MOST_HELPFUL", "POSITIVE_FIRST", "NEGATIVE_FIRST"],
        default="MOST_RECENT",
        help="Review sort order.",
    )
    parser.add_argument(
        "--filter",
        choices=["ALL", "STAR_5", "STAR_4", "STAR_3", "STAR_2", "STAR_1", "VERIFIED_BUYER"],
        default="ALL",
        help="Filter reviews by star rating or condition.",
    )
    parser.add_argument(
        "--format", "-f",
        choices=["csv", "excel", "json", "all"],
        default="all",
        help="Export format (default: all).",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Custom output file path or directory.",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=4,
        help="Parallel page downloads (default: 4). Lower it if you see many 429 rate-limit warnings.",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Probe how Nykaa's reviews API behaves for this product and save a report (for troubleshooting).",
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="Launch the interactive web UI dashboard.",
    )

    args = parser.parse_args()

    if args.ui:
        print("Starting Nykaa Scraper Web Dashboard on http://localhost:8000 ...")
        import uvicorn
        uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
        return

    # If no target or search provided, run interactive CLI
    target = args.search or args.target
    if not target:
        run_interactive()
        return

    if args.diagnose:
        import json
        report = NykaaScraper().diagnose(target)
        text = json.dumps(report, indent=2, ensure_ascii=False)
        print(text)
        out = Path("output") / f"diagnose_{report['resolved']['product_id']}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"\nSaved diagnostic report to {out.resolve()}")
        return

    print_banner()
    scraper = NykaaScraper(workers=args.workers)
    limit = None if args.limit <= 0 else args.limit

    def progress(scraped, total, pages):
        tot_str = str(total) if total else "?"
        print(f"\r-> Scraped {scraped}/{tot_str} reviews ({pages} pages fetched)", end="", flush=True)

    try:
        result = scraper.scrape(
            target=target,
            max_reviews=limit,
            sort=args.sort,
            filter_by=args.filter if args.filter != "ALL" else None,
            progress_callback=progress,
        )
        print()
        print_summary(result)

        exported_paths = export_result(result, args.format, args.output)
        print("\nSaved files:")
        for p in exported_paths:
            print(f" -> {Path(p).resolve()}")

    except Exception as e:
        print(f"\n[Error] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
