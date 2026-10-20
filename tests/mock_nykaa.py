"""
A local stand-in for Nykaa's reviews API, used by the tests.

It reproduces the behaviour observed on the live site:
  * 20 reviews per page, HTTP 400 beyond page 40 (the "page ceiling")
  * filters=STAR_1..STAR_5 (VERIFIED_BUYER is always on), childIds=<variant>, both combinable
  * reviewCount counts hidden reviews too, but hidden reviews are never served
    (pages come back shorter instead)
  * within one star rating, POSITIVE_FIRST / NEGATIVE_FIRST give the MOST_USEFUL order
  * unknown filters -> HTTP 400
Optional misbehaviour: rate limiting (429), server errors (500), Akamai-style HTML challenges.
"""

import json
import random
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

KNOWN_FILTERS = {"STAR_1", "STAR_2", "STAR_3", "STAR_4", "STAR_5", "VERIFIED_BUYER", "WITH_IMAGES"}


class MockNykaa:
    def __init__(
        self,
        n_reviews=300,
        page_size=20,
        ceiling=40,
        star_weights=(0.62, 0.18, 0.09, 0.04, 0.07),  # 5..1 stars
        variants=("1001",),        # childIds reviews are spread over
        hidden_fraction=0.0,       # share of reviews that are counted but never served
        fail_every=0,              # every Nth reviews request -> HTTP 500
        rate_limit_every=0,        # every Nth reviews request -> HTTP 429
        challenge_every=0,         # every Nth reviews request -> HTML page with status 200
        seed=1,
    ):
        rnd = random.Random(seed)
        self.page_size = page_size
        self.ceiling = ceiling
        self.fail_every = fail_every
        self.rate_limit_every = rate_limit_every
        self.challenge_every = challenge_every
        self.requests = 0
        self.lock = threading.Lock()
        self.reviews = []
        for i in range(n_reviews):
            rating = rnd.choices([5, 4, 3, 2, 1], star_weights)[0]
            hidden = rnd.random() < hidden_fraction
            self.reviews.append({
                "id": 100000 + i,
                # Malformed / odd fields the real API has been seen to send
                "rating": f"{rating}.0" if i % 7 == 0 else rating,
                "title": f"Title {i}",
                "description": f"Line one<br/>line two &amp; more {i}\x0b",
                "name": None if i % 11 == 0 else f"User {i}",
                "createdOn": f"2024-01-{(i % 28) + 1:02d} 10:00:{i % 60:02d}",
                "childId": int(rnd.choice(variants)),
                "likeCount": None if i % 5 == 0 else (0 if hidden else rnd.randint(0, 50)),
                "isBuyer": True,
                "_ts": i,
                "_helpful": rnd.random(),
                "_hidden": hidden,
            })

    @property
    def ids(self):
        """IDs of every review the API will actually serve."""
        return {str(r["id"]) for r in self.reviews if not r["_hidden"]}

    def rating_of(self, r):
        return int(float(r["rating"]))

    # -- request handling -------------------------------------------------
    def _filtered(self, filters, child_ids):
        items = self.reviews
        for f in [x for x in (filters or "").split(",") if x]:
            if f not in KNOWN_FILTERS:
                raise ValueError(f"unknown filter {f}")
            if f.startswith("STAR_"):
                items = [x for x in items if self.rating_of(x) == int(f[-1])]
        if child_ids:
            wanted = {int(c) for c in child_ids.split(",")}
            items = [x for x in items if x["childId"] in wanted]
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

        if "size" in query:
            return 400, "application/json", json.dumps({"error": "bad param"})
        page = int(query.get("pageNo", "1"))
        if page > self.ceiling:
            return 400, "application/json", json.dumps({"error": "invalid page"})
        try:
            items = self._filtered(query.get("filters"), query.get("childIds"))
        except ValueError as e:
            return 400, "application/json", json.dumps({"error": str(e)})

        useful = lambda x: (-(x["likeCount"] or 0), -x["_ts"])  # noqa: E731
        key = {
            "MOST_USEFUL": useful,
            "MOST_HELPFUL": lambda x: -x["_helpful"],
            "MOST_RECENT": lambda x: -x["_ts"],
            "POSITIVE_FIRST": lambda x: (-self.rating_of(x),) + useful(x),
            "NEGATIVE_FIRST": lambda x: (self.rating_of(x),) + useful(x),
        }.get(query.get("sort", "MOST_RECENT"))
        if key is None:
            return 400, "application/json", json.dumps({"error": "invalid sort"})
        items = sorted(items, key=key)
        window = items[(page - 1) * self.page_size: page * self.page_size]
        chunk = [{k: v for k, v in r.items() if not k.startswith("_")} for r in window if not r["_hidden"]]
        body = {"reviewCount": len(items), "status": "success", "response": {"page": page, "reviewData": chunk}}
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
