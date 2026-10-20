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
    const statWithImages = document.getElementById("statWithImages");
    const starBarsContainer = document.getElementById("starBarsContainer");

    const visibleReviewsCount = document.getElementById("visibleReviewsCount");
    const reviewSearchInput = document.getElementById("reviewSearchInput");
    const reviewStarFilter = document.getElementById("reviewStarFilter");
    const reviewsGrid = document.getElementById("reviewsGrid");

    const imageLightbox = document.getElementById("imageLightbox");
    const lightboxImg = document.getElementById("lightboxImg");
    const lightboxClose = document.getElementById("lightboxClose");

    // State
    let currentResult = null;
    let currentResultId = null;
    let tickerInterval = null;
    let searchDebounce = null;

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
                <span class="suggestion-name">${p.name}</span>
                <span class="suggestion-id">ID: ${p.id}</span>
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
            stopScrapingUI();

            if (!res.success) {
                alert("Scraping Error: " + (res.error || "Failed to fetch reviews."));
                return;
            }

            currentResult = res.data;
            currentResultId = res.result_id;
            displayResults(currentResult, currentResultId);

        } catch (err) {
            stopScrapingUI();
            alert("Network error: " + err.message);
        }
    });

    function startScrapingUI() {
        startScrapeBtn.disabled = true;
        resultsSection.style.display = "none";
        loadingSection.style.display = "block";

        const tickerMessages = [
            "Initiating browser-grade TLS handshake (Chrome 120)...",
            "Bypassing Akamai Bot Manager and WAF...",
            "Fetching product metadata and specifications...",
            "Resolving internal Nykaa Gateway reviews endpoint...",
            "Streaming reviews batch and decrypting reviewer labels...",
            "Extracting customer review photos and verified badges...",
            "Computing star distribution and sentiment analytics..."
        ];

        let msgIdx = 0;
        liveTicker.textContent = tickerMessages[0];
        tickerInterval = setInterval(() => {
            msgIdx = (msgIdx + 1) % tickerMessages.length;
            liveTicker.textContent = tickerMessages[msgIdx];
        }, 1200);

        loadingSection.scrollIntoView({ behavior: "smooth" });
    }

    function stopScrapingUI() {
        startScrapeBtn.disabled = false;
        loadingSection.style.display = "none";
        clearInterval(tickerInterval);
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
        statWithImages.textContent = summary.with_images_count;

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

        if (reviews.length === 0) {
            reviewsGrid.innerHTML = `
                <div style="grid-column: 1/-1; text-align: center; padding: 2rem; color: var(--text-muted);">
                    No reviews matching your criteria.
                </div>
            `;
            return;
        }

        reviews.forEach(rev => {
            const card = document.createElement("div");
            card.className = "review-card";

            // Initials
            const initials = rev.reviewer_name
                .split(" ")
                .filter(Boolean)
                .slice(0, 2)
                .map(w => w[0].toUpperCase())
                .join("") || "U";

            // Stars string
            const fullStars = "★".repeat(rev.rating);
            const emptyStars = "☆".repeat(Math.max(0, 5 - rev.rating));

            card.innerHTML = `
                <div>
                    <div class="review-top-row">
                        <div class="reviewer-meta">
                            <div class="avatar-circle">${initials}</div>
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
    }

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

    // --- Lightbox Modal ---
    window.openLightbox = function(url) {
        lightboxImg.src = url;
        imageLightbox.style.display = "flex";
    };

    lightboxClose.addEventListener("click", () => {
        imageLightbox.style.display = "none";
    });

    imageLightbox.addEventListener("click", (e) => {
        if (e.target === imageLightbox) {
            imageLightbox.style.display = "none";
        }
    });

    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && imageLightbox.style.display === "flex") {
            imageLightbox.style.display = "none";
        }
    });

    function escapeHtml(text) {
        if (!text) return "";
        const div = document.createElement("div");
        div.textContent = text;
        return div.innerHTML;
    }
});
