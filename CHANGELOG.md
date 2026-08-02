# Changelog

All notable changes to WebAudit are documented here.

## [Unreleased]

## [1.2.0] — 2025-07

### Added
- 90-day reassess queue: `not_interested` leads surface automatically for re-approach
- Browser audit via Playwright/Chromium (Core Web Vitals: LCP, CLS, TBT)
- Yellow Pages canary monitor — detects scraper breakage on startup

### Fixed
- `won` outcome missing from `OUTCOME_STAGE` mapping caused leads logged as won to remain in the Leads table indefinitely; added mapping and backfill migration

## [1.1.0] — 2025-05

### Added
- AI-generated cold call scripts via Claude API
- AI-generated follow-up emails via Claude API
- CASL-compliant blocklist (opted-out contacts permanently excluded)
- CSV import and export
- Manual business entry

### Changed
- Two-layer audit design: static audit (bulk) + browser audit (on-demand)
- Pipeline stage transitions now driven exclusively by call log outcomes

## [1.0.0] — 2025-03

### Initial release
- OpenStreetMap discovery via Overpass API
- Static website audit (SSL, SEO, mobile viewport, broken links)
- Lead dashboard with filter/sort/search
- Call logging with follow-up reminders
- Pipeline stages: new → contacted → interested → booked → won/lost
