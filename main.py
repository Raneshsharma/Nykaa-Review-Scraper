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
    print(f"Total Scraped:   {result.summary.total_scraped} reviews")
    print(f"Average Rating:  ★ {result.summary.average_rating} / 5.0")
    print(f"Verified Buyers: {result.summary.verified_buyer_count} ({round(result.summary.verified_buyer_count / max(result.summary.total_scraped, 1) * 100, 1)}%)")
    print(f"With Images:     {result.summary.with_images_count}")
    print("-" * 60)
    print("Star Rating Breakdown:")
    for star in ["5", "4", "3", "2", "1"]:
        count = result.summary.star_distribution.get(star, 0)
        pct = round(count / max(result.summary.total_scraped, 1) * 100)
        bar = "█" * (pct // 4)
        print(f"  {star} Star | {bar:<25} | {count:>4} ({pct:>2}%)")
    print("=" * 60)


def run_interactive():
    print_banner()
    print("Interactive Mode:")
    print("Enter a Nykaa Product URL, Product ID, or a product search term.")
    target = input("\nTarget (URL / ID / Search Query): ").strip()
    if not target:
        print("Error: Target cannot be empty. Exiting.")
        return

    limit_input = input("How many reviews to scrape? (default 50, enter 0 for all): ").strip()
    limit = 50
    if limit_input.isdigit():
        limit = int(limit_input)
        if limit == 0:
            limit = None

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
    print("  5) 1 Star Only")
    print("  6) Verified Buyers Only")
    print("  7) With Images Only")
    filter_choice = input("Select filter [1-7]: ").strip()
    filter_map = {
        "1": "ALL", "2": "STAR_5", "3": "STAR_4", "4": "STAR_3",
        "5": "STAR_1", "6": "VERIFIED_BUYER", "7": "WITH_IMAGES"
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

    def progress(scraped, total, page):
        tot_str = str(total) if total else "?"
        print(f"\r-> Scraped {scraped} reviews (Page {page}, Total Available: {tot_str})", end="", flush=True)

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

        # Export files
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("output")
        out_dir.mkdir(exist_ok=True)
        base_name = f"nykaa_reviews_{result.product.id}_{timestamp}"

        exported_paths = []
        if export_fmt in ("csv", "all"):
            path = export_to_csv(result, str(out_dir / f"{base_name}.csv"))
            exported_paths.append(path)
        if export_fmt in ("excel", "all"):
            path = export_to_excel(result, str(out_dir / f"{base_name}.xlsx"))
            exported_paths.append(path)
        if export_fmt in ("json", "all"):
            path = export_to_json(result, str(out_dir / f"{base_name}.json"))
            exported_paths.append(path)

        print("\nExported Files:")
        for p in exported_paths:
            print(f" -> {p}")

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
        default=50,
        help="Maximum reviews to scrape (default: 50. Use 0 for all).",
    )
    parser.add_argument(
        "--sort",
        choices=["MOST_RECENT", "MOST_HELPFUL", "POSITIVE_FIRST", "NEGATIVE_FIRST"],
        default="MOST_RECENT",
        help="Review sort order.",
    )
    parser.add_argument(
        "--filter",
        choices=["ALL", "STAR_5", "STAR_4", "STAR_3", "STAR_2", "STAR_1", "VERIFIED_BUYER", "WITH_IMAGES"],
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

    print_banner()
    scraper = NykaaScraper()
    limit = None if args.limit == 0 else args.limit

    def progress(scraped, total, page):
        tot_str = str(total) if total else "?"
        print(f"\r-> Scraped {scraped} reviews (Page {page}, Total Available: {tot_str})", end="", flush=True)

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

        # Handle outputs
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path("output")
        out_dir.mkdir(exist_ok=True)
        base_name = f"nykaa_reviews_{result.product.id}_{timestamp}"

        exported_paths = []
        if args.format in ("csv", "all"):
            path = export_to_csv(result, str(out_dir / f"{base_name}.csv"))
            exported_paths.append(path)
        if args.format in ("excel", "all"):
            path = export_to_excel(result, str(out_dir / f"{base_name}.xlsx"))
            exported_paths.append(path)
        if args.format in ("json", "all"):
            path = export_to_json(result, str(out_dir / f"{base_name}.json"))
            exported_paths.append(path)

        print("\nSaved files:")
        for p in exported_paths:
            print(f" -> {Path(p).resolve()}")

    except Exception as e:
        print(f"\n[Error] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
