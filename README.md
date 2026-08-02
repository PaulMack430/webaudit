# WebAudit — Ethical Lead Generation Pipeline

Find local businesses with broken websites, audit them for errors, generate cold call scripts, and manage your entire outreach pipeline — all from a local dashboard in your browser. No paid APIs. No subscriptions. No cloud.

---

## What it does

1. **Discovers businesses** in any city using OpenStreetMap and Yellow Pages (free, no API key)
2. **Audits their websites** for errors — broken SSL, missing mobile viewport, 404s, missing meta tags, broken images, slow load times, and more
3. **Browser audits** individual leads using real Chrome — measures Core Web Vitals, desktop/mobile scores, JavaScript errors
4. **Generates a full report card** with letter grades (A-F) across 6 categories
5. **Generates cold call scripts** tailored to each business's specific errors
6. **Generates follow-up emails** based on the site's issues
7. **Logs your calls** — track outcomes, notes, and follow-up dates
8. **Reminds you** of follow-ups due today with a banner at the top of the dashboard
9. **Exports to CSV** — take your leads anywhere
10. **Monitors Yellow Pages** health automatically on startup — self-heals if the layout changes

---

## Requirements

- Python 3.8 or higher
- Internet connection
- Any modern browser (Firefox, Chrome, Edge)

No Docker. No cloud account. No credit card.

---

## Setup (one time only)

**Step 1 — Check Python is installed**
```
python3 --version
```
If you get "command not found", download Python from https://python.org/downloads

**Step 2 — Install dependencies**
```
pip install requests beautifulsoup4 flask playwright
playwright install chromium
```

Note: The Playwright/Chromium download is about 300MB and only needed for Browser Audit. If you skip it, everything else still works — Browser Audit just won't be available.

**Step 3 — Run the app**

Navigate to the folder where `webaudit.py` is saved, then run:
```
python3 webaudit.py
```

**Step 4 — Open the dashboard**
```
http://localhost:5000
```

---

## How to use it

### Finding businesses

**Automatic discovery:**
1. Type a **city name** in the sidebar (e.g. Toronto, Mississauga, Hamilton)
2. Pick a **category** (restaurants, dental, auto repair, etc.) or leave on "All with websites"
3. Set **max results** (30 is a good starting point)
4. Tick your **sources** — OpenStreetMap, Yellow Pages, or both
5. Click **Find businesses**

Chains and franchises are automatically filtered out — you only see independent local businesses.

**Manual entry:**
Use the **Add manually** form in the sidebar to add a specific business by name, URL, phone, and address.

**CSV import:**
Click **📂 Click to select CSV file** to bulk import from a spreadsheet. Columns auto-detected — any combination of `name`, `website`, `url`, `phone`, `address` works.

---

### Auditing websites

There are three audit types — each serves a different purpose:

| Audit type | How it works | Speed | Best for |
|------------|-------------|-------|----------|
| **Quick** | Reads HTML code, homepage only | ~5s per site, 10 concurrent | Mass scanning large batches |
| **Deep** | Same as Quick + follows internal links | ~60s per site | Thorough check before calling |
| **Browser Audit** | Loads site in real Chrome browser | ~15s per site | Core Web Vitals on warm leads |

**Typical workflow:**
1. Run **Quick audit** on your full batch — find who has problems
2. On warm leads, click **View → 🌐 Browser Audit** to get real performance scores
3. Click **📊 Full Report** to see the complete report card before calling

---

### The Full Report card

Click **View** on any audited lead, then **📊 Full Report** to see a visual report card with letter grades (A-F) across 6 sections:

| Section | What's checked |
|---------|---------------|
| 🔒 **Security** | HTTPS/SSL, robots.txt, server errors |
| 🔍 **SEO** | Title, meta description, H1 heading, canonical tag, Open Graph, sitemap.xml |
| 📱 **Mobile** | Viewport tag, HTTPS on mobile |
| ⚡ **Performance** | Load time, page size, timeout + Core Web Vitals if Browser Audit has been run |
| ♿ **Accessibility** | Image alt text, favicon, heading structure |
| ✨ **User Experience** | Contact form, broken links, social media presence |

Each section shows ✅/❌ per check with a plain English description. An overall grade appears in the top right corner.

**Performance section after Browser Audit** also shows:
- Desktop score /100
- Mobile score /100
- LCP, FCP, TTFB times
- CLS (layout shift) score
- JavaScript errors
- Render-blocking resources

---

### Working leads

**Error modal** — click **View** on any lead to see what's broken:
- 🔒 SSL issues · ❌ 404/down · 📱 No mobile viewport · 🔍 SEO issues
- ♿ Alt text · 🔗 Broken links · 📅 Outdated copyright · 📝 No contact form · ⏱ Slow/timeout

**Call script** — click **Script** to get a personalised cold call pitch based on that site's specific errors. Includes opening, pitch, objection handling, and CASL compliance reminder. Click **Copy script** to copy.

**Follow-up email** — click **Email** for a personalised follow-up email. Click **Copy email** to copy.

**Log a call** — click **Call** to record outcome and set a follow-up date:
- No answer / Voicemail / Interested / Meeting booked / Not interested / Opted out
- Follow-up date triggers a yellow reminder banner on the dashboard

**Filter and sort:**
- Search by business name or website
- Filter: All / Has errors / Critical errors / Clean / Not audited
- Sort: Newest / Most errors / Fewest errors / Name A-Z

**Export** — click **Export CSV** to download your full leads list.

---

## Yellow Pages health monitor

Every time the server starts, WebAudit automatically checks whether Yellow Pages scraping is working. Status appears as a pill in the top right:

- ✅ **YP healthy** — fades after 5 seconds
- 🔧 **YP self-healed** — layout changed, new selectors found. Restart server to apply.
- ❌ **YP broken** — needs attention. Check terminal for details.
- 🚫 **YP blocked** — CloudFront blocking. Try again later.

---

## Compliance

- **robots.txt** checked before every request
- **Crawl-delay** respected per domain (min 1.5s)
- **User-Agent** identifies the bot as `WebAuditBot/1.0`
- Only publicly available business data collected
- Data stored locally only — nothing sent externally

**Cold calling — Canada (CASL):**
- B2B cold calls permitted without prior consent
- Identify your company by name
- State the purpose of your call
- Honour opt-out requests immediately
- Check DNCL before calling: https://lnnte-dncl.gc.ca

---

## Files

| File | Purpose |
|------|---------|
| `webaudit.py` | The entire application |
| `webaudit.db` | SQLite database — created automatically. Contains all leads, audits, and call logs |
| `README.md` | This file |

To share with a colleague, send all three files. They drop them in the same folder and run `python3 webaudit.py`.

---

## Tips

- Run city scans one at a time — each adds to the same database
- Use **Quick** for large batches, **Browser Audit** only on warm leads
- **Sort by "Most errors first"** to find your best leads immediately
- The **Script** button generates a different pitch for each error type — always use it before calling
- Chains are filtered automatically — only independent local businesses appear
- Re-audit leads to collect new metrics after updating the app
- The database never loses data between restarts

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Button not working | Hard refresh: Ctrl+Shift+R |
| 0 results from city search | Try a different city name or category |
| Audit stalls | Use Quick mode |
| Yellow Pages returns 0 | Check canary pill — restart server if self-healed |
| Browser Audit fails | Run: `pip install playwright && playwright install chromium` |
| Server won't start | `cd ~/WebAudit && python3 webaudit.py` |
| On Windows | Use `python` instead of `python3` |

---

## Coming soon

- Canada411 integration (phone number enrichment)
- Social media presence auditing
- Multi-user shared database
