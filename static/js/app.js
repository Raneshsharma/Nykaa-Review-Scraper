// ==========================================================================
// Nykaa Review Scraper — Client Application Logic
// ==========================================================================

document.addEventListener("DOMContentLoaded", () => {
    // DOM Elements
    const scrapeForm = document.getElementById("scrapeForm");
    const targetInput = document.getElementById("targetInput");
    const clearBtn = document.getElementById("clearBtn");
    const searchSuggestions = document.getElementById("searchSuggestions");
    const reviewLimit = document.getElementById("reviewLimit");
    const sortOrder = document.getElementById("sortOrder");
    const filterRating = document.getElementById("filterRating");
    const startScrapeBtn = document.getElementById("startScrapeBtn");

    const loadingSection = document.getElementById("loadingSection");
    const loaderTitle = document.getElementById("loaderTitle");
    const liveTicker = document.getElementById("liveTicker");
    const progressBar = document.getElementById("progressBar");
    const stopScrapeBtn = document.getElementById("stopScrapeBtn");

    const resultsSection = document.getElementById("resultsSection");
    const productImage = document.getElementById("productImage");
    const productTitle = document.getElementById("productTitle");
    const productBrand = document.getElementById("productBrand");
    const productAvailability = document.getElementById("productAvailability");
    const productIdBadge = document.getElementById("productIdBadge");
    const productPrice = document.getElementById("productPrice");
    const productRating = document.getElementById("productRating");
    const productLink = document.getElementById("productLink");

    const downloadCsvBtn = document.getElementById("downloadCsvBtn");
    const downloadExcelBtn = document.getElementById("downloadExcelBtn");
    const downloadJsonBtn = document.getElementById("downloadJsonBtn");

    const statTotalScraped = document.getElementById("statTotalScraped");
    const statAvgRating = document.getElementById("statAvgRating");
    const statVerifiedPct = document.getElementById("statVerifiedPct");
    const statCoverage = document.getElementById("statCoverage");
    const resultNotice = document.getElementById("resultNotice");
    const starBarsContainer = document.getElementById("starBarsContainer");

    const visibleReviewsCount = document.getElementById("visibleReviewsCount");
    const reviewSearchInput = document.getElementById("reviewSearchInput");
    const reviewStarFilter = document.getElementById("reviewStarFilter");
    const reviewsGrid = document.getElementById("reviewsGrid");
    const showMoreBtn = document.getElementById("showMoreBtn");

    // Rendering thousands of cards at once freezes the page; render in chunks
    const RENDER_CHUNK = 200;

    // State
    let currentResult = null;
    let currentResultId = null;
    let currentJobId = null;
    let searchDebounce = null;
    let filteredReviews = [];
    let renderedCount = 0;

    // --- Input Clear Button & Presets ---
    targetInput.addEventListener("input", () => {
        clearBtn.style.display = targetInput.value.trim() ? "block" : "none";
        handleLiveSearch();
    });

    clearBtn.addEventListener("click", () => {
        targetInput.value = "";
        clearBtn.style.display = "none";
        searchSuggestions.style.display = "none";
        targetInput.focus();
    });

    document.querySelectorAll(".preset-chip").forEach(chip => {
        chip.addEventListener("click", () => {
            targetInput.value = chip.dataset.target;
            clearBtn.style.display = "block";
            searchSuggestions.style.display = "none";
            scrapeForm.requestSubmit();
        });
    });

    // Close suggestions if clicked outside
    document.addEventListener("click", (e) => {
        if (!e.target.closest(".search-input-wrapper")) {
            searchSuggestions.style.display = "none";
        }
    });

    // --- Live Product Search Suggestions ---
    function handleLiveSearch() {
        clearTimeout(searchDebounce);
        const query = targetInput.value.trim();

        // If it looks like a URL or ID, do not query search API
        if (!query || query.length < 3 || query.startsWith("http") || /^\d+$/.test(query)) {
            searchSuggestions.style.display = "none";
            return;
        }

        searchDebounce = setTimeout(async () => {
            try {
                const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
                const json = await res.json();
                if (json.success && json.products && json.products.length > 0) {
                    renderSuggestions(json.products);
                } else {
                    searchSuggestions.style.display = "none";
                }
            } catch (err) {
                console.warn("Search suggestion error:", err);
            }
        }, 350);
    }

    function renderSuggestions(products) {
        searchSuggestions.innerHTML = "";
        products.forEach(p => {
            const item = document.createElement("div");
            item.className = "suggestion-item";
            item.innerHTML = `
                <span class="suggestion-name">${escapeHtml(p.name)}</span>
                <span class="suggestion-id">ID: ${escapeHtml(p.id)}</span>
            `;
            item.addEventListener("click", () => {
                targetInput.value = p.url || p.id;
                searchSuggestions.style.display = "none";
                scrapeForm.requestSubmit();
            });
            searchSuggestions.appendChild(item);
        });
        searchSuggestions.style.display = "block";
    }

    // --- Form Submission & Scraping Execution ---
    scrapeForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        // Preset chips / suggestions call requestSubmit(), which ignores the disabled button
        if (currentJobId || startScrapeBtn.disabled) return;
        const target = targetInput.value.trim();
        if (!target) return;

        searchSuggestions.style.display = "none";
        startScrapingUI();

        const payload = {
            target: target,
            limit: parseInt(reviewLimit.value, 10),
            sort: sortOrder.value,
            filter_by: filterRating.value
        };

        try {
            const resp = await fetch("/api/scrape", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            const res = await resp.json();
            if (!res.success) {
                stopScrapingUI();
                alert("Scraping Error: " + (res.error || res.detail || "Failed to start scraping."));
                return;
            }
            currentJobId = res.job_id;
            await pollJob(res.job_id);
        } catch (err) {
            stopScrapingUI();
            alert("Network error: " + err.message);
        }
    });

    async function pollJob(jobId) {
        let networkFailures = 0;
        while (true) {
            await new Promise(r => setTimeout(r, 1000));
            let job;
            try {
                const resp = await fetch(`/api/jobs/${jobId}`);
                if (resp.status === 404) {
                    stopScrapingUI();
                    alert("This scrape is no longer known to the server (it may have restarted). Please start again.");
                    return;
                }
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                job = await resp.json();
                networkFailures = 0;
            } catch (err) {
                // Tolerate brief hiccups while a long scrape is running
                if (++networkFailures >= 10) {
                    stopScrapingUI();
                    alert("Lost connection to the scraper server: " + err.message);
                    return;
                }
                continue;
            }

            if (job.status === "running") {
                updateProgress(job.collected, job.total, job.pages);
                continue;
            }

            stopScrapingUI();
            if (job.status === "cancelled") {
                return;  // stopped before any reviews were collected
            }
            if (job.status === "error" || !job.data) {
                alert("Scraping Error: " + (job.error || "Failed to fetch reviews."));
                return;
            }
            currentResult = job.data;
            currentResultId = job.result_id;
            displayResults(currentResult, currentResultId);
            return;
        }
    }

    stopScrapeBtn.addEventListener("click", async () => {
        if (!currentJobId) return;
        stopScrapeBtn.disabled = true;
        liveTicker.textContent = "Stopping... saving the reviews collected so far.";
        try {
            await fetch(`/api/jobs/${currentJobId}/cancel`, { method: "POST" });
        } catch (err) {
            console.warn("Cancel failed:", err);
        }
    });

    function updateProgress(collected, total, pages) {
        if (total > 0) {
            const pct = Math.min(100, Math.round((collected / total) * 100));
            progressBar.className = "progress-bar-fill determinate";
            progressBar.style.width = `${pct}%`;
            loaderTitle.textContent = `Scraping reviews... ${pct}%`;
            liveTicker.textContent = `Collected ${collected.toLocaleString()} of ${total.toLocaleString()} reviews (${pages} pages fetched)`;
        } else {
            liveTicker.textContent = `Collected ${collected.toLocaleString()} reviews (${pages} pages fetched)`;
        }
    }

    function startScrapingUI() {
        startScrapeBtn.disabled = true;
        stopScrapeBtn.disabled = false;
        resultsSection.style.display = "none";
        loadingSection.style.display = "block";
        progressBar.className = "progress-bar-fill indeterminate";
        progressBar.style.width = "";
        loaderTitle.textContent = "Connecting to Nykaa...";
        liveTicker.textContent = "Resolving product and fetching the first page of reviews...";
        loadingSection.scrollIntoView({ behavior: "smooth" });
    }

    function stopScrapingUI() {
        startScrapeBtn.disabled = false;
        loadingSection.style.display = "none";
        currentJobId = null;
    }

    // --- Display Scraped Results ---
    function displayResults(data, resultId) {
        const product = data.product;
        const summary = data.summary;
        const reviews = data.reviews;

        // 1. Product Card
        productTitle.textContent = product.name || "Nykaa Product";
        productBrand.textContent = product.brand || "Nykaa";
        productAvailability.textContent = product.availability || "In Stock";
        productIdBadge.textContent = `ID: ${product.id}`;
        productPrice.textContent = product.price ? `₹${product.price}` : "Price on Nykaa";
        productRating.textContent = `★ ${summary.average_rating || product.average_rating || '4.5'}`;
        productLink.href = product.url || `https://www.nykaa.com/product/p/${product.id}`;

        if (product.image_url) {
            productImage.src = product.image_url;
            productImage.style.display = "block";
        } else {
            productImage.src = "https://images-static.nykaa.com/media/wysiwyg/default_avatar.png";
        }

        // 2. Download Buttons
        downloadCsvBtn.onclick = () => window.location.href = `/api/download/${resultId}/csv`;
        downloadExcelBtn.onclick = () => window.location.href = `/api/download/${resultId}/xlsx`;
        downloadJsonBtn.onclick = () => window.location.href = `/api/download/${resultId}/json`;

        // 3. Analytics
        statTotalScraped.textContent = summary.total_scraped;
        statAvgRating.textContent = summary.average_rating;
        const verPct = summary.total_scraped > 0 
            ? Math.round((summary.verified_buyer_count / summary.total_scraped) * 100) 
            : 0;
        statVerifiedPct.textContent = `${verPct}%`;
        resultNotice.textContent = summary.warning || "";
        resultNotice.style.display = summary.warning ? "block" : "none";
        statCoverage.textContent = summary.total_available > 0
            ? `${Math.min(100, Math.round((summary.total_scraped / summary.total_available) * 1000) / 10)}%`
            : "–";

        // 4. Rating Distribution Bars
        renderStarBars(summary.star_distribution, summary.total_scraped);

        // 5. Reviews Grid
        renderReviewsList(reviews);

        // Show section
        resultsSection.style.display = "flex";
        resultsSection.scrollIntoView({ behavior: "smooth" });
    }

    function renderStarBars(distribution, total) {
        starBarsContainer.innerHTML = "";
        const stars = ["5", "4", "3", "2", "1"];

        stars.forEach(star => {
            const count = distribution[star] || 0;
            const pct = total > 0 ? Math.round((count / total) * 100) : 0;

            const row = document.createElement("div");
            row.className = "star-bar-row";
            row.innerHTML = `
                <span class="star-label">${star} Star</span>
                <div class="star-track">
                    <div class="star-fill" style="width: 0%"></div>
                </div>
                <span class="star-count">${count} (${pct}%)</span>
            `;
            starBarsContainer.appendChild(row);

            // Animate width
            setTimeout(() => {
                row.querySelector(".star-fill").style.width = `${pct}%`;
            }, 50);
        });
    }

    function renderReviewsList(reviews) {
        reviewsGrid.innerHTML = "";
        visibleReviewsCount.textContent = reviews.length;
        filteredReviews = reviews;
        renderedCount = 0;
        showMoreBtn.style.display = "none";

        if (reviews.length === 0) {
            reviewsGrid.innerHTML = `
                <div style="grid-column: 1/-1; text-align: center; padding: 2rem; color: var(--text-muted);">
                    No reviews matching your criteria.
                </div>
            `;
            return;
        }

        renderNextChunk();
    }

    function renderNextChunk() {
        const chunk = filteredReviews.slice(renderedCount, renderedCount + RENDER_CHUNK);
        renderedCount += chunk.length;

        chunk.forEach(rev => {
            const card = document.createElement("div");
            card.className = "review-card";

            // Initials
            const initials = (rev.reviewer_name || "")
                .split(" ")
                .filter(Boolean)
                .slice(0, 2)
                .map(w => w[0].toUpperCase())
                .join("") || "U";

            // Stars string
            const fullStars = "★".repeat(Math.max(0, Math.min(5, rev.rating)));
            const emptyStars = "☆".repeat(Math.max(0, 5 - rev.rating));

            card.innerHTML = `
                <div>
                    <div class="review-top-row">
                        <div class="reviewer-meta">
                            <div class="avatar-circle">${escapeHtml(initials)}</div>
                            <span class="reviewer-name">${escapeHtml(rev.reviewer_name)}</span>
                        </div>
                        <div class="rating-stars">${fullStars}${emptyStars}</div>
                    </div>

                    ${rev.title ? `<h4 class="review-title">${escapeHtml(rev.title)}</h4>` : ""}
                    <p class="review-text">${escapeHtml(rev.description || "No written text provided.")}</p>
                </div>
            `;

            reviewsGrid.appendChild(card);
        });

        const remaining = filteredReviews.length - renderedCount;
        if (remaining > 0) {
            showMoreBtn.textContent = `Show more reviews (${remaining.toLocaleString()} remaining)`;
            showMoreBtn.style.display = "block";
        } else {
            showMoreBtn.style.display = "none";
        }
    }

    showMoreBtn.addEventListener("click", renderNextChunk);

    // --- Search & Filter Reviews within Scraped Results ---
    function filterReviews() {
        if (!currentResult || !currentResult.reviews) return;

        const query = reviewSearchInput.value.toLowerCase().trim();
        const star = reviewStarFilter.value;

        const filtered = currentResult.reviews.filter(rev => {
            const matchesText = !query || 
                (rev.description && rev.description.toLowerCase().includes(query)) ||
                (rev.title && rev.title.toLowerCase().includes(query)) ||
                (rev.reviewer_name && rev.reviewer_name.toLowerCase().includes(query));

            const matchesStar = star === "ALL" || String(rev.rating) === star;

            return matchesText && matchesStar;
        });

        renderReviewsList(filtered);
    }

    reviewSearchInput.addEventListener("input", filterReviews);
    reviewStarFilter.addEventListener("change", filterReviews);

    function escapeHtml(text) {
        if (!text) return "";
        const div = document.createElement("div");
        div.textContent = text;
        return div.innerHTML;
    }
});
