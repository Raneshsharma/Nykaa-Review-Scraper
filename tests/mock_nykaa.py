"""
A local stand-in for Nykaa's reviews API, used by the tests.

It mimics what the scraper relies on: page-numbered JSON pages, a page ceiling
(400 beyond it), per-filter review counts, four sort orders, and optional
behaviours (bigger page sizes, combined filters, rate limiting, flaky errors,
Akamai-style HTML challenges, malformed fields).
"""

import json
import random
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


class MockNykaa:
    def __init__(
        self,
        n_reviews=300,
        page_size=20,
        ceiling=40,
        star_weights=(0.62, 0.18, 0.09, 0.04, 0.07),  # 5..1 stars
        page_size_param=None,      # e.g. "pageSize": API honours this param (max 100)
        combined_filters=False,    # API understands "STAR_5,VERIFIED_BUYER"
        fail_every=0,              # every Nth reviews request -> HTTP 500
        rate_limit_every=0,        # every Nth reviews request -> HTTP 429
        challenge_every=0,         # every Nth reviews request -> HTML page with status 200
        seed=1,
    ):
        rnd = random.Random(seed)
        self.page_size = page_size
        self.ceiling = ceiling
        self.page_size_param = page_size_param
        self.combined_filters = combined_filters
        self.fail_every = fail_every
        self.rate_limit_every = rate_limit_every
        self.challenge_every = challenge_every
        self.requests = 0
        self.lock = threading.Lock()
        self.reviews = []
        for i in range(n_reviews):
            rating = rnd.choices([5, 4, 3, 2, 1], star_weights)[0]
            self.reviews.append({
                "id": 100000 + i,
                # Malformed / odd fields the real API has been seen to send
                "rating": f"{rating}.0" if i % 7 == 0 else rating,
                "title": f"Title {i}",
                "description": f"Line one<br/>line two &amp; more {i}\x0b",
                "name": None if i % 11 == 0 else f"User {i}",
                "createdOn": f"2024-01-{(i % 28) + 1:02d}",
                "_ts": i,
                "likeCount": None if i % 5 == 0 else rnd.randint(0, 50),
                "isBuyer": rnd.random() < 0.7,
            })

    @property
    def ids(self):
        return {str(r["id"]) for r in self.reviews}

    # -- request handling -------------------------------------------------
    def _filtered(self, filters):
        items = self.reviews
        parts = filters.split(",") if filters else []
        if len(parts) > 1 and not self.combined_filters:
            parts = parts[:1]  # unsupported combos: API only applies the first filter
        for f in parts:
            if f in ("STAR_1", "STAR_2", "STAR_3", "STAR_4", "STAR_5"):
                items = [x for x in items if int(float(x["rating"])) == int(f[-1])]
            elif f == "VERIFIED_BUYER":
                items = [x for x in items if x["isBuyer"]]
            else:
                raise ValueError(f"unknown filter {f}")
        return items

    def handle(self, path, query):
        if "/reviews" not in path:
            return 200, "text/html", "<html><title>Mock Serum | Nykaa</title></html>"
        with self.lock:
            self.requests += 1
            n = self.requests
        if self.rate_limit_every and n % self.rate_limit_every == 0:
            return 429, "text/plain", "Too Many Requests"
        if self.fail_every and n % self.fail_every == 0:
            return 500, "text/plain", "Server Error"
        if self.challenge_every and n % self.challenge_every == 0:
            return 200, "text/html", "<html>Access Denied</html>"

        page = int(query.get("pageNo", "1"))
        if page > self.ceiling:
            return 400, "application/json", json.dumps({"error": "invalid page"})

        size = self.page_size
        if self.page_size_param and self.page_size_param in query:
            size = max(1, min(int(query[self.page_size_param]), 100))

        try:
            items = self._filtered(query.get("filters"))
        except ValueError as e:
            return 400, "application/json", json.dumps({"error": str(e)})
        sort = query.get("sort", "MOST_RECENT")
        key = {
            "MOST_RECENT": lambda x: -x["_ts"],
            "MOST_HELPFUL": lambda x: (-(x["likeCount"] or 0), -x["_ts"]),
            "POSITIVE_FIRST": lambda x: (-float(x["rating"]), -x["_ts"]),
            "NEGATIVE_FIRST": lambda x: (float(x["rating"]), -x["_ts"]),
        }.get(sort)
        if key is None:
            return 400, "application/json", json.dumps({"error": "invalid sort"})
        items = sorted(items, key=key)
        chunk = [{k: v for k, v in r.items() if not k.startswith("_")} for r in items[(page - 1) * size: page * size]]
        body = {"reviewCount": len(items), "response": {"reviewData": chunk}}
        return 200, "application/json", json.dumps(body)

    # -- server -----------------------------------------------------------
    def start(self):
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                status, ctype, body = mock.handle(u.path, q)
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.port}"
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
