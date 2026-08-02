# Architecture

WebAudit is a local-first desktop application. The Flask backend serves both the REST API and the single-page frontend HTML. pywebview wraps the Flask server in a native OS window — no browser required, no internet connection needed except for the Anthropic API and discovery sources.

## Data model

```
businesses          audits                calls
──────────          ──────                ─────
id                  id                    id
name                business_id ──┐       business_id ──┐
website             errors (JSON) │       called_at      │
address             created_at    │       outcome        │
category                          │       notes          │
pipeline_stage ◄───────────────── ┘       followup_date  │
                                          └──────────────┘

clients
───────
id
business_id ──► businesses.id
client_since
monthly_value
notes
```

`pipeline_stage` is the single source of truth for where a lead sits in the funnel. All transitions are written by `api_log_call` via the `OUTCOME_STAGE` mapping — there are no direct stage-update endpoints exposed to the UI. This makes every state change auditable via the `calls` table.

## Audit pipeline

### Static audit (BeautifulSoup4)

Runs in bulk. For each business with a website:

1. `requests.get(url)` with a 10s timeout and a realistic user-agent
2. Parse response with BeautifulSoup4
3. Check a fixed set of signals: SSL, viewport meta, title/description length, H1 presence, Open Graph tags, sampled internal link validity, robots.txt, sitemap.xml
4. Serialize findings as a JSON array to `audits.errors`

Bulk audit across 200 leads takes ~2–3 minutes on a typical connection.

### Browser audit (Playwright)

Runs on demand per lead. Launches a headless Chromium session:

1. Navigate to the URL with network idle wait
2. Capture: LCP via PerformanceObserver, CLS score, TBT as FID proxy, JS console errors, screenshot at 375px viewport (mobile)
3. Compare render time with JS enabled vs. static load time — the delta flags JS bloat
4. Results merged into the audit record

Takes ~8–12 seconds per site. Intentionally not run in bulk to avoid hammering target sites.

## Discovery pipeline

### OpenStreetMap (Overpass API)

Queries the Overpass API with city + category filters using Overpass QL. Returns structured JSON — no scraping, no rate limit concerns, no API key. Best for established businesses with physical presence.

### Yellow Pages scraper

BeautifulSoup4 scrape of yellowpages.ca search results. Includes a **canary monitor** that runs on app startup:

1. Scrape a known-good business listing
2. Assert expected fields are present and non-empty
3. If the canary fails → alert displayed on dashboard, scraper disabled until next startup

This catches CSS/layout changes before they cause silent data corruption.

### Deduplication

Incoming records are normalized by domain (strip `www.`, lowercase, strip trailing slash) and checked against existing `businesses.website` before insert.

## AI integration

See [`ai-integration.md`](ai-integration.md) for full prompt design notes.

The Claude API is called synchronously per lead. Both endpoints (`/api/script`, `/api/email`) follow the same pattern:

1. Fetch business record + latest audit errors from SQLite
2. Format errors in business-impact language (not technical jargon)
3. POST to `https://api.anthropic.com/v1/messages` with structured system + user prompt
4. Validate response structure
5. If validation fails → retry once with a stricter output format instruction
6. Return to frontend

## Frontend

Single-page HTML/CSS/JS served directly from the Flask app (embedded in `webaudit.py`). No build step, no bundler. All state lives in `_allLeads`, `_allClients` JS globals, refreshed on every meaningful action via `loadLeads()` / `loadPipeline()`.

Tab switching, filtering, sorting, and modal management are all vanilla JS. This keeps the app dependency-free on the frontend side.
