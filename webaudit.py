#!/usr/bin/env python3
"""
WebAudit — Ethical Lead Generation Pipeline
============================================
Free, open-source. No API keys. No subscriptions.

Business discovery : OpenStreetMap Overpass API (free, anonymous)
Compliance         : urllib.robotparser (Python built-in)
Error detection    : requests + beautifulsoup4
Storage            : SQLite (Python built-in)
Dashboard          : Flask (runs at http://localhost:5000)

Install deps once:
    pip install requests beautifulsoup4 flask

Run:
    python webaudit.py
"""

import sqlite3
import requests
import time
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.robotparser import RobotFileParser
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, Response

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

BOT_NAME    = "WebAuditBot/1.0"
CONTACT_URL = "https://yourwebsite.com/bot-info"   # <-- update this
USER_AGENT  = f"{BOT_NAME} (+{CONTACT_URL})"
CRAWL_DELAY = 1.5    # seconds between requests to same domain
REQUEST_TIMEOUT = 6   # seconds per individual HTTP request
SITE_TIMEOUT = 20    # max seconds for entire site audit before giving up

# ── Discovery source API keys ──
YELP_API_KEY = ""    # Paste your free Yelp API key here
                     # Get one free at: https://www.yelp.com/developers/v3/manage_app

# Chains/franchises to skip — not worth cold calling, websites managed centrally
CHAIN_DOMAINS = {
    "timhortons.ca", "mcdonalds.ca", "subway.com", "kfc.ca", "pizzahut.ca",
    "burgerking.ca", "wendys.com", "starbucks.com", "dunkindonuts.ca",
    "esso.ca", "petro-canada.ca", "shell.ca", "husky.ca", "ultramar.ca",
    "shoppersdrugmart.ca", "londondrugs.com", "rexall.ca", "pharmasave.com",
    "canadiantire.ca", "homedepot.ca", "lowes.ca", "rona.ca", "bestbuy.ca",
    "staples.ca", "walmart.ca", "costco.ca", "target.ca", "dollarama.ca",
    "7-eleven.ca", "circlek.ca", "macs.ca", "hastymarket.com",
    "scotiabank.com", "td.com", "rbc.com", "cibc.com", "bmo.com", "hsbc.ca",
    "cineplex.com", "chapter.indigo.ca", "sportchek.ca", "reitmans.ca",
    "royalbank.com", "nationalbank.ca", "laurentianbank.ca",
}
DB_PATH = "webaudit.db"

# ─────────────────────────────────────────────
# DATABASE SETUP
# ─────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS businesses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT,
            website     TEXT UNIQUE,
            phone       TEXT,
            address     TEXT,
            category    TEXT,
            source      TEXT,
            discovered  TEXT
        );

        CREATE TABLE IF NOT EXISTS audits (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id INTEGER,
            audited_at  TEXT,
            robots_ok   INTEGER,
            errors      TEXT,
            metrics     TEXT,
            FOREIGN KEY (business_id) REFERENCES businesses(id)
        );

        CREATE TABLE IF NOT EXISTS calls (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id     INTEGER,
            called_at       TEXT,
            outcome         TEXT,
            notes           TEXT,
            followup_date   TEXT,
            FOREIGN KEY (business_id) REFERENCES businesses(id)
        );
        CREATE TABLE IF NOT EXISTS clients (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id     INTEGER UNIQUE,
            client_since    TEXT,
            monthly_value   REAL DEFAULT 0,
            notes           TEXT,
            FOREIGN KEY (business_id) REFERENCES businesses(id)
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id     INTEGER,
            title           TEXT,
            description     TEXT,
            due_date        TEXT,
            completed       INTEGER DEFAULT 0,
            completed_at    TEXT,
            created_at      TEXT,
            FOREIGN KEY (business_id) REFERENCES businesses(id)
        );
        CREATE TABLE IF NOT EXISTS improvements (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            business_id     INTEGER,
            title           TEXT,
            description     TEXT,
            before_score    INTEGER,
            after_score     INTEGER,
            logged_at       TEXT,
            FOREIGN KEY (business_id) REFERENCES businesses(id)
        );
        CREATE TABLE IF NOT EXISTS _migrations (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
    """)
    con.commit()
    con.close()

def run_migrations():
    """Apply any schema upgrades needed."""
    con = get_db()
    cur = con.cursor()
    try:
        cur.execute("ALTER TABLE calls ADD COLUMN followup_date TEXT")
        print("[DB] Migration: added followup_date column")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE audits ADD COLUMN metrics TEXT")
        print("[DB] Migration: added metrics column")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE businesses ADD COLUMN pipeline_stage TEXT DEFAULT 'new'")
        print("[DB] Migration: added pipeline_stage column")
    except Exception:
        pass
    con.commit()
    con.close()

def run_canary_check() -> dict:
    """
    Quick health check of Yellow Pages scraper.
    Runs on startup. If broken, attempts to self-heal by finding new selectors.
    """
    TEST_URL = "https://www.yellowpages.ca/search/si/1/restaurants/Toronto"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-CA,en;q=0.9",
    }

    print("[Canary] Checking Yellow Pages scraper health...")
    try:
        resp = requests.get(TEST_URL, headers=HEADERS, timeout=12)
    except Exception as e:
        print(f"[Canary] ⚠️  Could not reach Yellow Pages: {e}")
        return {"status": "unreachable", "message": str(e)}

    if resp.status_code == 403:
        print("[Canary] ⚠️  Yellow Pages returned 403 — bot detection active")
        return {"status": "blocked", "message": "HTTP 403"}

    if resp.status_code != 200 or len(resp.text) < 10000:
        print(f"[Canary] ⚠️  Unexpected response: HTTP {resp.status_code}, {len(resp.text)} bytes")
        return {"status": "error", "message": f"HTTP {resp.status_code}"}

    soup = BeautifulSoup(resp.text, "html.parser")
    name_els = soup.find_all("a", class_="listing__name--link")

    if len(name_els) >= 3:
        print(f"[Canary] ✅ Yellow Pages healthy — {len(name_els)} listings found")
        return {"status": "ok", "count": len(name_els)}

    # ── Self-healing ──
    print("[Canary] ⚠️  listing__name--link not found — attempting self-heal...")

    # Find all classes on the page
    all_classes = set()
    for tag in soup.find_all(class_=True):
        for c in tag.get("class", []):
            all_classes.add(c)

    # Strategy 1: find a class that appears on 5+ anchor tags (likely listing names)
    from collections import Counter
    anchor_classes = Counter()
    for a in soup.find_all("a", class_=True):
        for c in a.get("class", []):
            anchor_classes[c] += 1

    # Candidate name class: appears on many anchors, contains 'name' or 'listing' or 'title'
    candidates = [
        (cls, count) for cls, count in anchor_classes.items()
        if count >= 5 and any(x in cls.lower() for x in ["name","listing","title","merchant","biz"])
    ]
    candidates.sort(key=lambda x: -x[1])

    if candidates:
        new_name_class = candidates[0][0]
        print(f"[Canary] 🔧 Auto-heal: found candidate name class '{new_name_class}' ({candidates[0][1]} occurrences)")

        # Verify it actually has text content that looks like business names
        sample = soup.find_all("a", class_=new_name_class)[:5]
        sample_names = [el.get_text(strip=True) for el in sample if el.get_text(strip=True)]

        if len(sample_names) >= 3:
            print(f"[Canary] 🔧 Sample names: {sample_names[:3]}")

            # Update the selector in webaudit.py
            script_path = __file__
            with open(script_path, "r") as f:
                script = f.read()

            old_selector = 'name_links = soup.find_all("a", class_="listing__name--link")'
            new_selector = f'name_links = soup.find_all("a", class_="{new_name_class}")  # auto-healed by canary'

            if old_selector in script:
                script = script.replace(old_selector, new_selector, 1)
                with open(script_path, "w") as f:
                    f.write(script)
                print(f"[Canary] ✅ Self-healed! Updated selector to '{new_name_class}'")
                print("[Canary] ⚠️  Restart the server to apply the fix")
                return {"status": "healed", "new_class": new_name_class, "samples": sample_names}
            else:
                print("[Canary] Could not find selector to replace in script")
        else:
            print(f"[Canary] Candidate class '{new_name_class}' didn't yield enough business names")

    # Strategy 2: look for any repeated link pattern near address elements
    addr_els = soup.find_all(class_="address")
    if addr_els:
        parent = addr_els[0].find_parent()
        if parent:
            nearby_links = parent.find_all("a", class_=True)
            nearby_classes = [c for a in nearby_links for c in a.get("class", [])]
            if nearby_classes:
                print(f"[Canary] Alternative: classes near address elements: {list(set(nearby_classes))[:5]}")

    print("[Canary] ❌ Could not self-heal — manual fix needed")
    print("[Canary] Share the diagnostic output with Claude to fix:")
    print("[Canary] Run: python3 canary.py for detailed diagnostics")
    return {"status": "broken", "message": "Could not find listing name selector"}

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

# ─────────────────────────────────────────────
# COMPLIANCE — robots.txt
# ─────────────────────────────────────────────

_robots_cache = {}
_domain_last_hit = {}  # tracks last request time per domain for smart delay

def is_crawl_allowed(url: str) -> tuple[bool, float]:
    """
    Returns (allowed: bool, crawl_delay: float).
    Respects robots.txt Disallow rules and Crawl-delay.
    Results are cached per domain to avoid re-fetching.
    """
    parsed = urlparse(url)
    domain = f"{parsed.scheme}://{parsed.netloc}"

    if domain not in _robots_cache:
        rp = RobotFileParser()
        rp.set_url(f"{domain}/robots.txt")
        try:
            # Set a short timeout for robots.txt fetch
            import urllib.request
            urllib.request.urlopen(f"{domain}/robots.txt", timeout=4)
            rp.read()
        except Exception:
            # If robots.txt is unreachable, assume allowed
            _robots_cache[domain] = (True, CRAWL_DELAY, rp)
            return True, CRAWL_DELAY
        delay = rp.crawl_delay(USER_AGENT) or CRAWL_DELAY
        _robots_cache[domain] = (True, delay, rp)

    _, delay, rp = _robots_cache[domain]
    allowed = rp.can_fetch(USER_AGENT, url)
    return allowed, delay

# ─────────────────────────────────────────────
# BUSINESS DISCOVERY — OpenStreetMap Overpass API
# ─────────────────────────────────────────────

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

OSM_CATEGORIES = {
    "restaurant":   '["amenity"="restaurant"]',
    "cafe":         '["amenity"="cafe"]',
    "shop":         '["shop"]',
    "hotel":        '["tourism"="hotel"]',
    "office":       '["office"]',
    "gym":          '["leisure"="fitness_centre"]',
    "beauty":       '["shop"="beauty"]',
    "dental":       '["amenity"="dentist"]',
    "auto":         '["shop"="car_repair"]',
    "plumber":      '["craft"="plumber"]',
    "electrician":  '["craft"="electrician"]',
    "all":          '["website"]',
}

def geocode_city(city: str):
    """Use Nominatim (free, no key) to get a bounding box for a city."""
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": city, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=10
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            print(f"[Discovery] Nominatim: no results for '{city}'")
            return None
        bb = results[0].get("boundingbox")
        if not bb:
            return None
        s, n, w, e = float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
        print(f"[Discovery] Geocoded '{city}' -> bbox ({s:.3f},{w:.3f},{n:.3f},{e:.3f})")
        return s, w, n, e
    except Exception as ex:
        print(f"[Discovery] Geocode error: {ex}")
        return None

def discover_businesses(city: str, category: str = "all", max_results: int = 50) -> list[dict]:
    """
    Query OpenStreetMap for businesses with websites in a city.
    Uses Nominatim to geocode the city to a bounding box first,
    then queries Overpass within that box. Fully free, no API key.
    """
    tag_filter = OSM_CATEGORIES.get(category, '["website"]')

    # Step 1: geocode city to bounding box
    bbox = geocode_city(city)
    if not bbox:
        print(f"[Discovery] Could not geocode '{city}'. Try a more specific name.")
        return []
    s, w, n, e = bbox

    # Step 2: query Overpass within that bounding box
    # Build query with string concatenation to avoid f-string/brace conflicts
    query = (
        "[out:json][timeout:40];\n"
        "(\n"
        "  node" + tag_filter + '["website"](' + str(s) + "," + str(w) + "," + str(n) + "," + str(e) + ");\n"
        "  way"  + tag_filter + '["website"](' + str(s) + "," + str(w) + "," + str(n) + "," + str(e) + ");\n"
        ");\n"
        "out body center " + str(max_results) + ";\n"
    )

    print(f"[Discovery] Querying OpenStreetMap for '{category}' in '{city}'...")
    data = None
    for attempt in range(3):
        try:
            resp = requests.post(
                OVERPASS_URL,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=45
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            print(f"[Discovery] Overpass API error (attempt {attempt+1}/3): {e}")
            if attempt < 2:
                print(f"[Discovery] Retrying in 10 seconds...")
                time.sleep(10)
    if data is None:
        print("[Discovery] Overpass API unavailable after 3 attempts. Try again later.")
        return []

    results = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        website = tags.get("website", "").strip()
        if not website:
            continue
        if not website.startswith("http"):
            website = "https://" + website

        # Skip known chains/franchises
        domain = website.lower().replace("https://","").replace("http://","").replace("www.","").split("/")[0]
        if domain in CHAIN_DOMAINS:
            print(f"[Discovery] Skipping chain: {tags.get('name','?')} ({domain})")
            continue

        results.append({
            "name":     tags.get("name", "Unknown"),
            "website":  website,
            "phone":    tags.get("phone", tags.get("contact:phone", "")),
            "address":  ", ".join(filter(None, [
                            tags.get("addr:housenumber", ""),
                            tags.get("addr:street", ""),
                            tags.get("addr:city", city),
                        ])),
            "category": category,
            "source":   "OpenStreetMap",
        })

    print(f"[Discovery] Found {len(results)} businesses with websites.")
    return results

def discover_yellowpages(city: str, category: str = "restaurants", max_results: int = 40) -> list[dict]:
    """
    Scrape Yellow Pages Canada for businesses.
    Slow and polite — respects robots.txt, 2s delay between requests.
    Only collects publicly visible business data for personal lead generation use.
    """
    import re
    results = []
    
    # Check robots.txt first
    yp_allowed, _ = is_crawl_allowed("https://www.yellowpages.ca/search/si/1/" + category + "/" + city)
    if not yp_allowed:
        print("[YellowPages] robots.txt disallows scraping — skipping")
        return []

    # Map common categories to Yellow Pages search terms
    cat_map = {
        "restaurant": "restaurants", "cafe": "cafes", "shop": "retail+stores",
        "hotel": "hotels", "dental": "dentists", "auto": "auto+repair",
        "plumber": "plumbers", "electrician": "electricians", "gym": "gyms",
        "beauty": "beauty+salons", "all": "businesses"
    }
    search_term = cat_map.get(category, category.replace(" ", "+"))
    city_slug = city.replace(" ", "+")

    page = 1
    while len(results) < max_results:
        url = f"https://www.yellowpages.ca/search/si/{page}/{search_term}/{city_slug}"
        print(f"[YellowPages] Fetching page {page}: {url}")
        time.sleep(2)  # Polite delay
        try:
            resp = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-CA,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            }, timeout=15)
            if resp.status_code != 200:
                print(f"[YellowPages] HTTP {resp.status_code} — skipping")
                break
        except Exception as e:
            print(f"[YellowPages] Error: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        # Find listings using confirmed class names from Yellow Pages HTML
        name_links = soup.find_all("a", class_="listing__name--link")

        if not name_links:
            print(f"[YellowPages] No listings found on page {page} — layout may have changed")
            break

        for name_el in name_links:
            if len(results) >= max_results:
                break
            try:
                name = name_el.get_text(strip=True)
                if not name:
                    continue

                # Walk up to find the listing container (li or parent div)
                container = name_el.parent
                for _ in range(6):
                    if container is None:
                        break
                    tag = container.name
                    cls = " ".join(container.get("class", []))
                    if tag in ["li","article"] or "listing" in cls:
                        break
                    container = container.parent
                if container is None:
                    continue

                # Website — mlr__item--website contains the external link
                website = ""
                website_wrap = container.find(class_=lambda c: c and "mlr__item--website" in c if c else False)
                if website_wrap:
                    a = website_wrap.find("a")
                    if a:
                        website = a.get("href", "")
                if not website:
                    # Fallback: any external link in container
                    for a in container.find_all("a", href=True):
                        h = a.get("href","")
                        if h.startswith("http") and "yellowpages" not in h.lower() and "yp.ca" not in h.lower():
                            website = h
                            break

                if not website or not website.startswith("http"):
                    continue

                # Phone
                phone_el = container.find(class_=lambda c: c and "phone" in c.lower() if c else False)
                phone = phone_el.get_text(strip=True) if phone_el else ""

                # Address
                addr_el = container.find(class_="address")
                address = addr_el.get_text(strip=True).replace("\n"," ").strip() if addr_el else city

                # Skip chains
                domain = website.lower().replace("https://","").replace("http://","").replace("www.","").split("/")[0]
                if domain in CHAIN_DOMAINS:
                    continue

                results.append({
                    "name": name, "website": website, "phone": phone,
                    "address": address, "category": category, "source": "YellowPages"
                })
            except Exception:
                continue

        # Next page
        next_btn = soup.find("a", class_=lambda c: c and "next" in c.lower() if c else False)
        if not next_btn:
            break
        page += 1

    print(f"[YellowPages] Found {len(results)} businesses")
    return results


def discover_canada411(city: str, category: str = "all", max_results: int = 40) -> list[dict]:
    """
    Scrape Canada411 for businesses with websites.
    Polite scraping — respects robots.txt, 2s delay.
    """
    results = []

    allowed, _ = is_crawl_allowed("https://www.canada411.ca/search/")
    if not allowed:
        print("[Canada411] robots.txt disallows scraping — skipping")
        return []

    cat_map = {
        "restaurant": "restaurant", "cafe": "cafe", "dental": "dentist",
        "auto": "auto+repair", "plumber": "plumber", "electrician": "electrician",
        "gym": "gym", "beauty": "hair+salon", "hotel": "hotel", "all": "business"
    }
    search_term = cat_map.get(category, "business")
    city_enc = city.replace(" ", "+")

    page = 1
    while len(results) < max_results:
        url = f"https://www.canada411.ca/search/si/{page}/b/{search_term}/{city_enc}/"
        print(f"[Canada411] Fetching page {page}: {url}")

        time.sleep(2)
        try:
            resp = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-CA,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            }, timeout=15)
            if resp.status_code != 200:
                print(f"[Canada411] HTTP {resp.status_code} — skipping")
                break
        except Exception as e:
            print(f"[Canada411] Error: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        listings = (
            soup.find_all("div", class_=lambda c: c and "result" in c.lower() if c else False) or
            soup.find_all("div", class_=lambda c: c and "listing" in c.lower() if c else False) or
            soup.find_all("article", class_=True) or
            soup.find_all("div", attrs={"data-id": True}) or
            soup.find_all("li", class_=lambda c: c and any(x in c.lower() for x in ["result","listing","business"]) if c else False)
        )

        if not listings:
            print(f"[Canada411] No listings on page {page} — may need selector update")
            break

        for listing in listings:
            if len(results) >= max_results:
                break
            try:
                name_el = listing.find(["h3","h2","a"])
                name = name_el.get_text(strip=True) if name_el else ""
                if not name:
                    continue

                website_el = listing.find("a", href=lambda h: h and h.startswith("http") and "canada411" not in h if h else False)
                website = website_el.get("href","") if website_el else ""
                if not website:
                    continue

                phone_el = listing.find(class_=lambda c: c and "phone" in c.lower() if c else False)
                phone = phone_el.get_text(strip=True) if phone_el else ""

                domain = website.lower().replace("https://","").replace("http://","").replace("www.","").split("/")[0]
                if domain in CHAIN_DOMAINS:
                    continue

                results.append({
                    "name": name, "website": website, "phone": phone,
                    "address": city, "category": category, "source": "Canada411"
                })
            except Exception:
                continue

        next_btn = soup.find("a", string=lambda s: s and "next" in s.lower() if s else False)
        if not next_btn:
            break
        page += 1

    print(f"[Canada411] Found {len(results)} businesses")
    return results


def save_businesses(businesses: list[dict]) -> int:
    """Save discovered businesses to DB, skip duplicates. Returns count added."""
    con = get_db()
    cur = con.cursor()
    added = 0
    for b in businesses:
        try:
            cur.execute("""
                INSERT OR IGNORE INTO businesses
                (name, website, phone, address, category, source, discovered)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (b["name"], b["website"], b["phone"], b["address"],
                  b["category"], b["source"], datetime.now().isoformat()))
            if cur.rowcount:
                added += 1
        except Exception as e:
            print(f"[DB] Save error for {b.get('website')}: {e}")
    con.commit()
    con.close()
    return added

# ─────────────────────────────────────────────
# ERROR DETECTION ENGINE
# ─────────────────────────────────────────────

def audit_website(url: str, mode: str = 'deep') -> dict:
    """
    Audits a website for errors and collects performance metrics.
    mode='quick' checks homepage only (~5s per site).
    mode='deep' also crawls internal links (~60s per site).
    Always checks robots.txt first.
    """
    errors = []
    metrics = {
        "load_time_ms":    None,
        "page_size_kb":    None,
        "h1_count":        None,
        "h2_count":        None,
        "favicon":         None,
        "sitemap":         None,
        "canonical":       None,
        "open_graph":      None,
        "schema_markup":   None,
        "social_links":    [],
        "image_count":     None,
        "images_no_alt":   None,
        "https":           None,
        "robots_txt":      None,
    }
    result = {
        "url":        url,
        "robots_ok":  False,
        "errors":     errors,
        "metrics":    metrics,
        "audited_at": datetime.now().isoformat(),
    }

    # 1. Check robots.txt first — always
    allowed, delay = is_crawl_allowed(url)
    result["robots_ok"] = allowed

    if not allowed:
        errors.append({
            "type":     "compliance",
            "severity": "info",
            "message":  "robots.txt Disallow — skipped per compliance rules"
        })
        return result

    # Smart per-domain delay — only wait if we recently hit this domain
    parsed_url = urlparse(url)
    domain_key = parsed_url.netloc
    now = time.time()
    last = _domain_last_hit.get(domain_key, 0)
    wait = delay - (now - last)
    if wait > 0:
        time.sleep(wait)
    _domain_last_hit[domain_key] = time.time()

    # 2. Fetch the page
    try:
        import time as _time
        _t0 = _time.time()
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=(5, REQUEST_TIMEOUT),  # (connect timeout, read timeout)
            allow_redirects=True
        )
        metrics["load_time_ms"] = int((_time.time() - _t0) * 1000)
        metrics["page_size_kb"] = round(len(resp.content) / 1024, 1)
    except requests.exceptions.SSLError:
        errors.append({"type": "ssl", "severity": "critical",
                        "message": "SSL certificate error — site may show 'Not Secure' to visitors"})
        return result
    except requests.exceptions.ConnectionError:
        errors.append({"type": "down", "severity": "critical",
                        "message": "Site unreachable — connection refused or DNS failure"})
        return result
    except requests.exceptions.Timeout:
        errors.append({"type": "timeout", "severity": "warning",
                        "message": "Site took too long to respond (>8s) — likely causing user drop-off"})
        return result
    except Exception as e:
        errors.append({"type": "unknown", "severity": "warning",
                        "message": f"Unexpected error: {str(e)}"})
        return result

    # 3. HTTP status
    if resp.status_code >= 500:
        errors.append({"type": "server_error", "severity": "critical",
                        "message": f"Server error ({resp.status_code}) — site is broken for all visitors"})
    elif resp.status_code == 404:
        errors.append({"type": "not_found", "severity": "critical",
                        "message": "Homepage returns 404 — site is effectively down"})
    elif resp.status_code >= 400:
        errors.append({"type": "client_error", "severity": "warning",
                        "message": f"HTTP {resp.status_code} on homepage"})

    # 4. Check SSL (is the final URL https?)
    metrics["https"] = resp.url.startswith("https://")
    if not metrics["https"]:
        errors.append({"type": "ssl", "severity": "critical",
                        "message": "Site does not use HTTPS — browsers warn visitors 'Not Secure'"})

    # 5. Parse HTML
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        errors.append({"type": "parse", "severity": "warning",
                        "message": "Could not parse HTML — page may be malformed"})
        return result

    # 6. Mobile viewport
    viewport = soup.find("meta", attrs={"name": "viewport"})
    if not viewport:
        errors.append({"type": "mobile", "severity": "warning",
                        "message": "No mobile viewport meta tag — site likely broken on phones"})

    # 7. Title tag
    title = soup.find("title")
    if not title or not title.text.strip():
        errors.append({"type": "seo", "severity": "warning",
                        "message": "Missing or empty <title> tag — hurts SEO and browser tab clarity"})

    # 8. Meta description
    meta_desc = soup.find("meta", attrs={"name": "description"})
    if not meta_desc or not meta_desc.get("content", "").strip():
        errors.append({"type": "seo", "severity": "warning",
                        "message": "Missing meta description — reduces click-through rate in Google results"})

    # 9. Images without alt text
    images = soup.find_all("img")
    missing_alt = [img for img in images if not img.get("alt")]
    if missing_alt:
        errors.append({"type": "accessibility", "severity": "warning",
                        "message": f"{len(missing_alt)} image(s) missing alt text — accessibility and SEO issue"})

    # 10. Broken internal links — deep mode only
    if mode == 'deep':
        internal_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/") or href.startswith(url):
                full = urljoin(url, href)
                internal_links.append(full)
        broken = []
        for link in internal_links[:10]:
            link_allowed, link_delay = is_crawl_allowed(link)
            if not link_allowed:
                continue
            domain_key2 = urlparse(link).netloc
            now2 = time.time()
            last2 = _domain_last_hit.get(domain_key2, 0)
            wait2 = link_delay - (now2 - last2)
            if wait2 > 0:
                time.sleep(wait2)
            _domain_last_hit[domain_key2] = time.time()
            try:
                link_resp = requests.head(link, headers={"User-Agent": USER_AGENT},
                                          timeout=(3, 5), allow_redirects=True)
                if link_resp.status_code >= 400:
                    broken.append(link)
            except Exception:
                broken.append(link)
        if broken:
            errors.append({"type": "broken_links", "severity": "warning",
                            "message": f"{len(broken)} broken internal link(s) found"})

    # 11. Copyright year check
    text = soup.get_text()
    import re
    years = re.findall(r'©\s*(\d{4})', text)
    current_year = datetime.now().year
    if years:
        latest = max(int(y) for y in years)
        if latest < current_year - 1:
            errors.append({"type": "outdated", "severity": "info",
                            "message": f"Copyright shows © {latest} — site may be neglected or unmaintained"})

    # 12. Contact form check
    forms = soup.find_all("form")
    if not forms:
        errors.append({"type": "ux", "severity": "info",
                        "message": "No contact form detected — customers may struggle to reach them"})

    # 13. H1 / H2 headings
    h1s = soup.find_all("h1")
    h2s = soup.find_all("h2")
    metrics["h1_count"] = len(h1s)
    metrics["h2_count"] = len(h2s)
    if len(h1s) == 0:
        errors.append({"type": "seo", "severity": "warning",
                        "message": "No H1 heading found — every page should have one clear main heading for SEO"})
    elif len(h1s) > 1:
        errors.append({"type": "seo", "severity": "info",
                        "message": f"{len(h1s)} H1 tags found — best practice is exactly one per page"})

    # 14. Favicon
    favicon = (
        soup.find("link", rel=lambda r: r and "icon" in " ".join(r).lower() if r else False) or
        soup.find("link", attrs={"rel": "shortcut icon"})
    )
    metrics["favicon"] = favicon is not None
    if not favicon:
        errors.append({"type": "ux", "severity": "info",
                        "message": "No favicon found — site shows a blank tab icon in browsers"})

    # 15. Canonical tag
    canonical = soup.find("link", attrs={"rel": "canonical"})
    metrics["canonical"] = canonical is not None
    if not canonical:
        errors.append({"type": "seo", "severity": "info",
                        "message": "No canonical tag — can cause duplicate content issues in Google"})

    # 16. Open Graph tags
    og_title = soup.find("meta", property="og:title")
    og_image = soup.find("meta", property="og:image")
    metrics["open_graph"] = og_title is not None
    if not og_title or not og_image:
        errors.append({"type": "seo", "severity": "info",
                        "message": "Missing Open Graph tags — site won't show a preview when shared on social media"})

    # 17. Schema / structured data
    schema = soup.find("script", attrs={"type": "application/ld+json"})
    metrics["schema_markup"] = schema is not None

    # 18. Social media links
    social_domains = ["facebook.com", "instagram.com", "twitter.com", "x.com",
                      "linkedin.com", "youtube.com", "tiktok.com"]
    social_found = []
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        for s in social_domains:
            if s in href and s not in [x.split(".")[0] for x in social_found]:
                social_found.append(s.replace(".com",""))
    metrics["social_links"] = social_found

    # 19. Sitemap check (quick HEAD request)
    try:
        from urllib.parse import urlparse as _urlparse
        _base = f"{_urlparse(url).scheme}://{_urlparse(url).netloc}"
        _sm = requests.head(f"{_base}/sitemap.xml",
                            headers={"User-Agent": USER_AGENT}, timeout=4)
        metrics["sitemap"] = _sm.status_code == 200
        if not metrics["sitemap"]:
            errors.append({"type": "seo", "severity": "info",
                            "message": "No sitemap.xml found — makes it harder for Google to index the site"})
    except Exception:
        metrics["sitemap"] = False

    # 20. robots.txt metric
    metrics["robots_txt"] = result["robots_ok"]

    # 21. Image metrics
    all_imgs = soup.find_all("img")
    no_alt = [i for i in all_imgs if not i.get("alt")]
    metrics["image_count"] = len(all_imgs)
    metrics["images_no_alt"] = len(no_alt)

    return result

def save_audit(business_id: int, audit: dict):
    con = get_db()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO audits (business_id, audited_at, robots_ok, errors, metrics)
        VALUES (?, ?, ?, ?, ?)
    """, (business_id, audit["audited_at"], int(audit["robots_ok"]),
          json.dumps(audit["errors"]),
          json.dumps(audit.get("metrics", {}))))
    con.commit()
    con.close()

def audit_concurrent(rows: list, mode: str = 'quick', max_workers: int = 10) -> tuple[int, int]:
    """
    Audit multiple businesses concurrently using a thread pool.
    Each site has a hard timeout of SITE_TIMEOUT seconds.
    Deduplicates by website URL so same site is never audited twice in one batch.
    Returns (audited_count, total_errors).
    """
    # Deduplicate by normalized website URL
    # Strip protocol, www., trailing slashes to catch all variants
    def normalize_url(url):
        u = url.lower().rstrip("/")
        u = u.replace("https://", "").replace("http://", "")
        u = u.replace("www.", "")
        return u.split("/")[0]  # domain only

    seen_urls = set()
    unique_rows = []
    for row in rows:
        normalized = normalize_url(row["website"])
        if normalized not in seen_urls:
            seen_urls.add(normalized)
            unique_rows.append(row)
        else:
            print(f"[Audit] Skipping duplicate: {row['website']}")
    
    dupes = len(rows) - len(unique_rows)
    if dupes:
        print(f"[Audit] Removed {dupes} duplicate site(s) from batch")
    
    rows = unique_rows
    total_errors = 0
    audited = 0
    total = len(rows)

    def _audit_one(row):
        audit = audit_website(row["website"], mode=mode)
        save_audit(row["id"], audit)
        return len(audit["errors"])

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_audit_one, row): row for row in rows}
        for i, future in enumerate(as_completed(futures), 1):
            row = futures[future]
            domain = row["website"].replace("https://","").replace("http://","").split("/")[0]
            try:
                # Hard timeout per site — if it takes longer than SITE_TIMEOUT, skip it
                errors = future.result(timeout=SITE_TIMEOUT + REQUEST_TIMEOUT)
                total_errors += errors
                audited += 1
                print(f"[Audit] {i}/{total} — {domain} — {errors} error(s)")
            except TimeoutError:
                print(f"[Audit] {i}/{total} — {domain} — TIMED OUT (skipped)")
                save_audit(row["id"], {
                    "audited_at": datetime.now().isoformat(),
                    "robots_ok": True,
                    "timed_out": True,
                    "errors": [{"type": "timed_out", "severity": "warning",
                                "message": f"Site took longer than {SITE_TIMEOUT}s — try retrying at a different time"}]
                })
            except Exception as e:
                print(f"[Audit] {i}/{total} — {domain} — ERROR: {e}")

    print(f"[Audit] Complete — {audited}/{total} sites audited, {total_errors} errors found.")
    return audited, total_errors

# ─────────────────────────────────────────────
# FLASK DASHBOARD
# ─────────────────────────────────────────────

app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WebAudit</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'Inter',system-ui,sans-serif;background:#f7f7f5;color:#1a1a18;font-size:13px;line-height:1.5;}

/* ── Layout ── */
.topbar{background:#fff;border-bottom:.5px solid #e2e2dc;padding:0 1.5rem;display:flex;align-items:center;height:52px;gap:10px;position:sticky;top:0;z-index:10;}
.logo-wrap{display:flex;align-items:center;gap:9px;}
.logo-icon{width:32px;height:32px;background:#e8f0fe;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:16px;}
.logo-name{font-size:15px;font-weight:600;color:#111;}
.logo-sub{font-size:11px;color:#999;}
.status-pill{margin-left:auto;display:flex;align-items:center;gap:5px;font-size:11px;font-weight:500;background:#eaf6f0;color:#1a7a50;border:.5px solid #b6dfc9;border-radius:20px;padding:3px 10px;}
.dot{width:6px;height:6px;border-radius:50%;background:#1a7a50;}
.dot.orange{background:#e07820;}.dot.blue{background:#2563eb;}
.status-pill.orange{background:#fff3e0;color:#8a4a00;border-color:#f0c070;}
.status-pill.blue{background:#eff6ff;color:#1d4ed8;border-color:#bfdbfe;}

.layout{display:grid;grid-template-columns:220px 1fr;min-height:calc(100vh - 52px);}
.sidebar{background:#fff;border-right:.5px solid #e2e2dc;padding:.9rem .9rem;display:flex;flex-direction:column;gap:.9rem;min-width:0;}
.sidebar-section-label{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:#bbb;margin-bottom:8px;}
.field-label{font-size:11px;color:#666;margin-bottom:3px;display:block;}
.sidebar input,.sidebar select{width:100%;padding:7px 9px;border:.5px solid #d8d8d2;border-radius:6px;font-size:12px;font-family:inherit;background:#fafafa;color:#111;margin-bottom:10px;outline:none;}
.sidebar input:focus,.sidebar select:focus{border-color:#2563eb;background:#fff;}
.btn{width:100%;padding:8px 12px;border-radius:6px;border:none;cursor:pointer;font-size:12px;font-weight:500;font-family:inherit;transition:opacity .15s;}
.btn-primary{background:#2563eb;color:#fff;}.btn-primary:hover{opacity:.88;}
.btn-ghost{background:#f2f2ee;color:#333;border:.5px solid #ddd;margin-top:5px;}.btn-ghost:hover{background:#e8e8e4;}
.compliance-mini{background:#fffbeb;border:.5px solid #f0d060;border-radius:7px;padding:10px 11px;font-size:11px;color:#7a5000;line-height:1.65;}
.compliance-mini strong{display:block;font-size:11px;font-weight:600;margin-bottom:5px;color:#5a3a00;}
.compliance-check{display:flex;flex-direction:column;gap:3px;font-size:11px;color:#555;}
.compliance-check span{display:flex;align-items:center;gap:5px;}
.ck{color:#1a7a50;font-size:13px;font-weight:600;}
.cw{color:#e07820;font-size:13px;}

.main{padding:1.25rem 1.5rem;}
.casl-banner{background:#fffbeb;border:.5px solid #f0d060;border-radius:8px;padding:10px 14px;font-size:11px;color:#6a4c00;margin-bottom:1.25rem;display:flex;gap:8px;align-items:flex-start;}
.casl-banner strong{display:block;font-size:11px;font-weight:600;margin-bottom:3px;}
.casl-banner a{color:#7a5000;}

/* ── Metrics ── */
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:1.25rem;}
.metric{background:#fff;border:.5px solid #e2e2dc;border-radius:10px;padding:14px 16px;}
.metric-label{font-size:10px;color:#999;margin-bottom:5px;font-weight:500;text-transform:uppercase;letter-spacing:.05em;}
.metric-value{font-size:26px;font-weight:600;color:#111;letter-spacing:-.5px;}
.metric-value.red{color:#c0392b;}.metric-value.blue{color:#2563eb;}.metric-value.green{color:#187a4c;}
.metric-sub{font-size:10px;color:#bbb;margin-top:2px;}

/* ── Two column panels ── */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;}
.panel{background:#fff;border:.5px solid #e2e2dc;border-radius:10px;padding:1rem 1.1rem;}
.panel-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;}
.panel-title{font-size:12px;font-weight:600;color:#111;display:flex;align-items:center;gap:6px;}
.panel-title svg{width:14px;height:14px;stroke:#666;fill:none;stroke-width:1.8;}
.panel-link{font-size:11px;color:#2563eb;cursor:pointer;}

/* ── Lead rows ── */
.lead-row{display:flex;align-items:center;gap:9px;padding:8px 0;border-bottom:.5px solid #f2f2ee;}
.lead-row:last-child{border-bottom:none;padding-bottom:0;}
.avatar{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;flex-shrink:0;}
.av-blue{background:#e8f0fe;color:#1d4ed8;}
.av-amber{background:#fff3e0;color:#8a4a00;}
.av-green{background:#eaf6f0;color:#187a4c;}
.av-purple{background:#f3e8fd;color:#6d28d9;}
.lead-meta{flex:1;min-width:0;}
.lead-name{font-size:12px;font-weight:500;color:#111;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.lead-url{font-size:10px;color:#bbb;}
.badges{display:flex;gap:3px;flex-wrap:wrap;margin-top:3px;}
.badge{font-size:9px;font-weight:600;padding:2px 7px;border-radius:20px;white-space:nowrap;}
.b-red{background:#fdeaea;color:#9b2020;border:.5px solid #f5c0c0;}
.b-yellow{background:#fff8e0;color:#7a5000;border:.5px solid #f0d070;}
.b-green{background:#eaf6f0;color:#187a4c;border:.5px solid #b6dfc9;}
.b-gray{background:#f2f2ee;color:#666;border:.5px solid #ddd;}
.b-blue{background:#eff6ff;color:#1d4ed8;border:.5px solid #bfdbfe;}

.call-btn{font-size:10px;padding:4px 9px;border-radius:5px;border:.5px solid #d8d8d2;background:#fff;color:#333;cursor:pointer;white-space:nowrap;font-family:inherit;}
.call-btn:hover{background:#f2f2ee;}

/* ── Error type list ── */
.error-item{display:flex;align-items:flex-start;gap:8px;padding:7px;background:#f7f7f5;border-radius:7px;margin-bottom:6px;}
.error-item:last-child{margin-bottom:0;}
.error-icon{width:26px;height:26px;border-radius:6px;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:13px;}
.ei-red{background:#fdeaea;}.ei-yellow{background:#fff8e0;}.ei-blue{background:#eff6ff;}
.error-title{font-size:11px;font-weight:600;color:#111;}
.error-desc{font-size:10px;color:#888;}

/* ── Compliance panel ── */
.comp-row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:.5px solid #f2f2ee;font-size:11px;}
.comp-row:last-child{border-bottom:none;}
.comp-label{flex:1;color:#555;}
.comp-ok{color:#187a4c;font-weight:500;}.comp-warn{color:#e07820;font-weight:500;}
.progress-track{background:#f2f2ee;border-radius:20px;height:4px;overflow:hidden;margin-top:10px;}
.progress-fill{height:4px;border-radius:20px;background:#187a4c;}
.progress-meta{display:flex;justify-content:space-between;margin-top:5px;font-size:10px;color:#bbb;}

/* ── Log ── */
.log-wrap{background:#fff;border:.5px solid #e2e2dc;border-radius:10px;padding:12px 14px;max-height:160px;overflow-y:auto;font-size:11px;font-family:'Courier New',monospace;margin-top:12px;color:#555;}
.log-wrap p{margin-bottom:2px;}.l-ok{color:#187a4c;}.l-err{color:#c0392b;}.l-info{color:#2563eb;}

.tab-btn{padding:5px 12px;border-radius:6px;border:.5px solid #ddd;background:#f7f7f5;font-size:11px;cursor:pointer;color:#555;font-family:inherit;}
.tab-btn.active{background:#2563eb;color:#fff;border-color:#2563eb;}
.tab-btn:hover:not(.active){background:#e8e8e4;}
.tab-content{display:none;}.tab-content.active{display:block;}
.kanban{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:12px;}
.rea-row{display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:.5px solid #f2f2ee;background:#fff;}
.rea-row:last-child{border-bottom:none;}
.blk-row{display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:.5px solid #f2f2ee;background:#fff;opacity:.7;}
/* ── Toast notification ── */
.toast{position:fixed;bottom:1.5rem;right:1.5rem;background:#fff;border:.5px solid #e2e2dc;
       border-radius:10px;padding:12px 16px;box-shadow:0 4px 20px rgba(0,0,0,.1);
       display:flex;align-items:center;gap:10px;font-size:12px;color:#111;
       transform:translateY(80px);opacity:0;transition:all .3s ease;z-index:999;min-width:220px;}
.toast.show{transform:translateY(0);opacity:1;}
.toast-icon{font-size:18px;flex-shrink:0;}
.toast-title{font-weight:600;font-size:12px;margin-bottom:2px;}
.toast-body{font-size:11px;color:#666;}
.toast.success{border-color:#b6dfc9;}
.toast.warning{border-color:#f0d060;}
.toast.error{border-color:#f5c0c0;}

/* ── Full leads table ── */
.leads-panel{background:#fff;border:.5px solid #e2e2dc;border-radius:10px;overflow:hidden;margin-bottom:12px;}
table{width:100%;border-collapse:collapse;}
thead th{background:#fafafa;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:#999;padding:9px 13px;text-align:left;border-bottom:.5px solid #e2e2dc;}
tbody td{padding:9px 13px;border-bottom:.5px solid #f2f2ee;font-size:11px;vertical-align:middle;}
tbody tr:last-child td{border-bottom:none;}
tbody tr:hover{background:#fafaf8;}

/* ── Modals ── */
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:50;align-items:center;justify-content:center;}
.overlay.show{display:flex;}
.modal{background:#fff;border-radius:12px;padding:1.5rem;width:500px;max-height:80vh;overflow-y:auto;border:.5px solid #e2e2dc;}
.modal h2{font-size:14px;font-weight:600;margin-bottom:12px;}
.modal textarea{width:100%;border:.5px solid #ddd;border-radius:6px;padding:8px;font-size:12px;font-family:inherit;height:80px;resize:vertical;margin-top:4px;outline:none;}
.modal textarea:focus{border-color:#2563eb;}
.modal select{padding:6px 9px;border:.5px solid #ddd;border-radius:6px;font-size:12px;font-family:inherit;}
.modal-footer{display:flex;gap:8px;margin-top:12px;justify-content:flex-end;}
.modal-footer button{padding:6px 14px;border-radius:6px;font-size:12px;font-family:inherit;cursor:pointer;font-weight:500;}
.btn-cancel{background:#f2f2ee;border:.5px solid #ddd;color:#333;}
.btn-save{background:#2563eb;border:none;color:#fff;}
.err-row{display:flex;gap:8px;align-items:flex-start;padding:7px 0;border-bottom:.5px solid #f2f2ee;}
.err-row:last-child{border-bottom:none;}
.sev-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;margin-top:4px;}
.s-critical{background:#c0392b;}.s-warning{background:#e07820;}.s-info{background:#2563eb;}
</style>
</head>
<body>

<div class="topbar">
  <div class="logo-wrap">
    <div class="logo-icon">🔍</div>
    <div>
      <div class="logo-name">WebAudit</div>
      <div class="logo-sub">Lead generation &amp; site error detection</div>
    </div>
  </div>
  <div class="status-pill" id="status-pill"><div class="dot"></div><span id="status-text">Idle</span></div>
  <div id="canary-pill" style="display:none;margin-left:8px;display:flex;align-items:center;gap:5px;font-size:11px;font-weight:500;border-radius:20px;padding:3px 10px;border:.5px solid #ddd;background:#f7f7f5;color:#888;">
    <span id="canary-icon">🔍</span>
    <span id="canary-text">Checking YP...</span>
  </div>
  <div style="margin-left:auto;display:flex;gap:4px;">
    <button class="tab-btn active" onclick="showTab('leads',this)">Leads</button>
    <button class="tab-btn" onclick="showTab('pipeline',this)">Clients</button>
    <button class="tab-btn" onclick="showTab('reassess',this)">Reassess</button>
    <button class="tab-btn" onclick="showTab('blocked',this)">Blocked</button>
  </div>
</div>

<div class="layout">

  <!-- SIDEBAR -->
  <div class="sidebar">
    <div>
      <div class="sidebar-section-label">Find businesses</div>
      <label class="field-label">City or area</label>
      <input type="text" id="city" placeholder="e.g. Toronto" value="Toronto">
      <label class="field-label">Category</label>
      <select id="category">
        <option value="all">All with websites</option>
        <option value="restaurant">Restaurants</option>
        <option value="cafe">Cafes</option>
        <option value="shop">Retail shops</option>
        <option value="hotel">Hotels</option>
        <option value="office">Offices</option>
        <option value="gym">Gyms</option>
        <option value="beauty">Beauty &amp; salons</option>
        <option value="dental">Dental</option>
        <option value="auto">Auto repair</option>
        <option value="plumber">Plumbers</option>
        <option value="electrician">Electricians</option>
      </select>
      <label class="field-label">Max results</label>
      <input type="number" id="max_results" value="30" min="5" max="100">
      <div style="margin-bottom:6px;">
        <div class="sidebar-section-label" style="margin-bottom:5px;">Sources</div>
        <select id="sources-select" style="width:100%;padding:5px 8px;border:.5px solid #d8d8d2;border-radius:5px;font-size:11px;font-family:inherit;background:#fff;">
          <option value="all">All sources</option>
          <option value="osm">OpenStreetMap only</option>
          <option value="yellowpages">Yellow Pages only</option>
        </select>
      </div>
      <button class="btn btn-primary" onclick="runDiscover()">Find businesses</button>
      <div style="height:6px;"></div>
      <button class="btn btn-ghost" onclick="runAuditAll()">Audit all unaudited</button>
      <div style="background:#f7f7f5;border:.5px solid #e2e2dc;border-radius:6px;padding:8px 10px;margin-top:2px;">
        <div style="font-size:10px;color:#999;font-weight:600;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px;">Audit mode</div>
        <select name="auditmode" id="auditmode-select" onchange="updateModeHint()" style="width:100%;padding:5px 8px;border:.5px solid #d8d8d2;border-radius:5px;font-size:11px;font-family:inherit;background:#fff;">
          <option value="quick">Quick — homepage only (fast)</option>
          <option value="deep">Deep — full link crawl (slow)</option>
        </select>
      </div>
      <button class="btn btn-ghost" id="retry-btn" onclick="runRetryTimedOut()" style="display:none;border-color:#e07820;color:#8a4a00;">Retry timed out (<span id="timed-out-count">0</span>)</button>

    </div>
    <div>
      <div class="sidebar-section-label">Add manually</div>
      <label class="field-label">Business name *</label>
      <input type="text" id="manual-name" placeholder="e.g. Joe's Pizza">
      <label class="field-label">Website URL *</label>
      <input type="text" id="manual-website" placeholder="e.g. joespizza.ca">
      <label class="field-label">Phone (optional)</label>
      <input type="text" id="manual-phone" placeholder="e.g. 416-555-0123">
      <label class="field-label">Address (optional)</label>
      <input type="text" id="manual-address" placeholder="e.g. 123 Main St, Toronto">
      <button class="btn btn-primary" onclick="addManual()">Add business</button>

    </div>
    <div>
      <div class="sidebar-section-label">Import CSV</div>
      <div style="font-size:10px;color:#999;margin-bottom:8px;line-height:1.6;">
        Columns auto-detected:<br>
        <code style="font-size:9px;">name, website, phone, address</code>
      </div>
      <label style="display:block;padding:8px;border:.5px dashed #d8d8d2;border-radius:6px;text-align:center;cursor:pointer;font-size:11px;color:#888;background:#fafafa;margin-bottom:6px;" id="csv-drop-label">
        📂 Click to select CSV file
        <input type="file" id="csv-file" accept=".csv" style="display:none;" onchange="handleCSV(this)">
      </label>
      <div id="csv-status" style="font-size:10px;color:#888;text-align:center;min-height:16px;"></div>
    </div>

    <div>
      <div class="sidebar-section-label">Compliance status</div>
      <div class="compliance-check">
        <span><span class="ck">✓</span> robots.txt enforced</span>
        <span><span class="ck">✓</span> Crawl-delay respected</span>
        <span><span class="ck">✓</span> Bot identified honestly</span>
        <span><span class="ck">✓</span> Public OSM data only</span>
        <span><span class="cw">⚠</span> Review CASL before calling</span>
      </div>
    </div>

    <div class="compliance-mini">
      <strong>CASL — B2B cold calling</strong>
      Calling businesses is permitted without prior consent. Identify your company, state your purpose,
      and honour opt-outs immediately. Check the
      <a href="https://lnnte-dncl.gc.ca" target="_blank" style="color:#7a5000;">DNCL registry</a> before dialling.
    </div>
  </div>

  <!-- MAIN -->
  <div class="main">

    <!-- LEADS TAB -->
    <div id="tab-leads" class="tab-content active">

    <!-- Metrics -->
    <!-- Follow-ups due today -->
    <div id="followups-banner" style="display:none;background:#fff8e0;border:.5px solid #f0d060;border-radius:8px;padding:10px 14px;margin-bottom:12px;">
      <div style="font-size:12px;font-weight:600;color:#7a5000;margin-bottom:6px;">📅 Follow-ups due today</div>
      <div id="followups-list" style="font-size:11px;color:#6a4000;"></div>
    </div>

    <div class="metrics">
      <div class="metric">
        <div class="metric-label">Sites scanned</div>
        <div class="metric-value" id="m-total">—</div>
        <div class="metric-sub">Total businesses</div>
      </div>
      <div class="metric">
        <div class="metric-label">Errors found</div>
        <div class="metric-value red" id="m-errors">—</div>
        <div class="metric-sub">Warm leads</div>
      </div>
      <div class="metric">
        <div class="metric-label">Audited</div>
        <div class="metric-value blue" id="m-audited">—</div>
        <div class="metric-sub">Sites checked</div>
      </div>
      <div class="metric">
        <div class="metric-label">Calls logged</div>
        <div class="metric-value green" id="m-calls">—</div>
        <div class="metric-sub">This session</div>
      </div>
    </div>

    <!-- Two column: leads + error types -->
    <div class="two-col">

      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <svg viewBox="0 0 24 24"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/></svg>
            Recent leads
          </div>
          <span class="panel-link" onclick="exportCSV()">Export CSV</span>
        </div>
        <div id="leads-preview">
          <div style="color:#bbb;font-size:11px;padding:1rem 0;text-align:center;">Run a scan to see leads here</div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <div class="panel-title">
            <svg viewBox="0 0 24 24"><path d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>
            Error types detected
          </div>
        </div>
        <div class="error-item">
          <div class="error-icon ei-red">🔒</div>
          <div><div class="error-title">SSL / HTTPS issues</div><div class="error-desc">Certificate expired — browsers show "Not Secure"</div></div>
        </div>
        <div class="error-item">
          <div class="error-icon ei-red">🔗</div>
          <div><div class="error-title">Broken links (4xx/5xx)</div><div class="error-desc">Pages returning error responses</div></div>
        </div>
        <div class="error-item">
          <div class="error-icon ei-yellow">📱</div>
          <div><div class="error-title">No mobile viewport</div><div class="error-desc">Missing responsive meta tag</div></div>
        </div>
        <div class="error-item">
          <div class="error-icon ei-yellow">🖼</div>
          <div><div class="error-title">Missing alt text / broken images</div><div class="error-desc">Accessibility and SEO penalty</div></div>
        </div>
        <div class="error-item">
          <div class="error-icon ei-blue">⏱</div>
          <div><div class="error-title">Slow page load</div><div class="error-desc">Core Web Vitals failure — hurts Google ranking</div></div>
        </div>
      </div>
    </div>

    <!-- Full leads table -->
    <!-- Filter & Sort bar -->
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap;">
      <input type="text" id="filter-search" placeholder="Search business name..." 
             oninput="filterLeads()"
             style="padding:6px 10px;border:.5px solid #d8d8d2;border-radius:6px;font-size:12px;font-family:inherit;background:#fff;flex:1;min-width:160px;outline:none;">
      <select id="filter-errors" onchange="filterLeads()"
              style="padding:6px 10px;border:.5px solid #d8d8d2;border-radius:6px;font-size:12px;font-family:inherit;background:#fff;outline:none;">
        <option value="all">All leads</option>
        <option value="errors">Has errors only</option>
        <option value="critical">Critical errors only</option>
        <option value="clean">Clean sites</option>
        <option value="unaudited">Not audited</option>
      </select>
      <select id="filter-sort" onchange="filterLeads()"
              style="padding:6px 10px;border:.5px solid #d8d8d2;border-radius:6px;font-size:12px;font-family:inherit;background:#fff;outline:none;">
        <option value="newest">Newest first</option>
        <option value="errors_desc">Most errors first</option>
        <option value="errors_asc">Fewest errors first</option>
        <option value="name">Name A-Z</option>
      </select>
      <span id="leads-count" style="font-size:11px;color:#bbb;white-space:nowrap;"></span>
    </div>

    <div class="leads-panel">
      <table>
        <thead>
          <tr>
            <th>Business</th>
            <th>Website</th>
            <th>Category</th>
            <th>Errors</th>
            <th>Call status</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="leads-table">
          <tr><td colspan="6" style="color:#bbb;text-align:center;padding:2rem;">
            No leads yet — run a discovery scan to get started
          </td></tr>
        </tbody>
      </table>
    </div>

    <!-- Compliance detail + progress -->
    <div class="panel" style="margin-bottom:12px;">
      <div class="panel-header">
        <div class="panel-title">
          <svg viewBox="0 0 24 24"><path d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"/></svg>
          Compliance status
        </div>
      </div>
      <div class="comp-row"><span class="comp-label">robots.txt respected on every request</span><span class="comp-ok">✓ Enforced</span></div>
      <div class="comp-row"><span class="comp-label">Crawl-delay between requests</span><span class="comp-ok">✓ Min 1.5s</span></div>
      <div class="comp-row"><span class="comp-label">User-Agent disclosed (WebAuditBot/1.0)</span><span class="comp-ok">✓ Active</span></div>
      <div class="comp-row"><span class="comp-label">CASL B2B exemption applied</span><span class="comp-ok">✓ Business numbers only</span></div>
      <div class="comp-row"><span class="comp-label">Data retention (PIPEDA)</span><span class="comp-warn">⚠ Public data only — review policy</span></div>
      <div class="progress-track"><div class="progress-fill" style="width:80%;"></div></div>
      <div class="progress-meta"><span>Compliance score</span><span style="font-weight:600;color:#111;">80%</span></div>
    </div>

    <!-- Activity log -->
    <div class="log-wrap" id="log">
      <p class="l-info">[WebAudit] Ready. Enter a city and click "Find businesses" to begin.</p>
    </div>

    </div> <!-- end tab-leads -->

    <!-- CLIENTS TAB -->
    <div id="tab-pipeline" class="tab-content">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
        <div>
          <div style="font-size:13px;font-weight:600;">Clients</div>
          <div style="font-size:11px;color:#999;margin-top:2px;">Businesses you have won — click a card to open their full dashboard</div>
        </div>
        <button class="btn-small" onclick="loadPipeline()" style="font-size:11px;">Refresh</button>
      </div>
      <div id="clients-grid" style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;">
        <div style="color:#bbb;font-size:12px;text-align:center;grid-column:1/-1;padding:2rem;">Loading clients...</div>
      </div>
    </div>

    <!-- REASSESS TAB -->
    <div id="tab-reassess" class="tab-content">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
        <div style="font-size:13px;font-weight:600;">Reassessment queue</div>
        <button class="btn-small" id="reaudit-lost-btn" onclick="reauditLost()" style="font-size:11px;border-color:#2563eb;color:#2563eb;">🔄 Re-audit all lost leads</button>
      </div>
      <div style="font-size:11px;color:#888;margin-bottom:12px;">Leads that said "not interested" 90+ days ago — re-audited and showing new errors worth re-approaching.</div>
      <div style="background:#fff;border:.5px solid #e2e2dc;border-radius:10px;overflow:hidden;" id="reassess-list">
        <div style="color:#bbb;font-size:12px;text-align:center;padding:2rem;">Loading...</div>
      </div>
    </div>

    <!-- BLOCKED TAB -->
    <div id="tab-blocked" class="tab-content">
      <div style="font-size:13px;font-weight:600;margin-bottom:6px;">Blocked — Opted out</div>
      <div style="font-size:11px;color:#888;margin-bottom:12px;">These contacts have opted out. Do not call. Kept for CASL compliance record.</div>
      <div style="background:#fff;border:.5px solid #e2e2dc;border-radius:10px;overflow:hidden;" id="blocked-list">
        <div style="color:#bbb;font-size:12px;text-align:center;padding:2rem;">Loading...</div>
      </div>
    </div>

  </div>
</div>

<!-- Email modal -->
<div class="overlay" id="email-modal">
  <div class="modal" style="width:580px;">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
      <h2 id="email-modal-title">Follow-up email</h2>
      <button onclick="copyEmail()" style="padding:5px 12px;border-radius:6px;border:.5px solid #ddd;background:#f2f2ee;font-size:11px;cursor:pointer;font-family:inherit;">Copy email</button>
    </div>
    <div style="font-size:11px;color:#888;margin-bottom:8px;" id="email-subject-line"></div>
    <pre id="email-body" style="font-family:inherit;font-size:11px;color:#333;line-height:1.7;white-space:pre-wrap;background:#f7f7f5;border-radius:8px;padding:1rem;max-height:55vh;overflow-y:auto;border:.5px solid #e2e2dc;"></pre>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="document.getElementById('email-modal').classList.remove('show')">Close</button>
    </div>
  </div>
</div>

<!-- Script modal -->
<div class="overlay" id="script-modal">
  <div class="modal" style="width:580px;">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
      <h2 id="script-modal-title">Cold call script</h2>
      <button onclick="copyScript()" style="padding:5px 12px;border-radius:6px;border:.5px solid #ddd;background:#f2f2ee;font-size:11px;cursor:pointer;font-family:inherit;">Copy script</button>
    </div>
    <pre id="script-body" style="font-family:inherit;font-size:11px;color:#333;line-height:1.7;white-space:pre-wrap;background:#f7f7f5;border-radius:8px;padding:1rem;max-height:60vh;overflow-y:auto;border:.5px solid #e2e2dc;"></pre>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="document.getElementById('script-modal').classList.remove('show')">Close</button>
    </div>
  </div>
</div>

<!-- Toast notification -->
<div class="toast" id="toast">
  <div class="toast-icon" id="toast-icon">✅</div>
  <div>
    <div class="toast-title" id="toast-title">Done</div>
    <div class="toast-body" id="toast-body"></div>
  </div>
</div>

<!-- Call modal -->
<div class="overlay" id="call-modal">
  <div class="modal">
    <h2>Log a call</h2>
    <input type="hidden" id="modal-biz-id">
    <div style="font-size:12px;color:#888;margin-bottom:10px;" id="modal-biz-name"></div>
    <label style="font-size:11px;color:#666;">Outcome</label><br>
    <select id="modal-outcome" onchange="toggleFollowup()" style="margin-top:4px;margin-bottom:10px;width:100%;padding:6px 9px;border:.5px solid #ddd;border-radius:6px;font-size:12px;font-family:inherit;">
      <option value="no_answer">No answer</option>
      <option value="voicemail">Left voicemail</option>
      <option value="interested">Interested — follow up</option>
      <option value="booked">Meeting booked ✓</option>
      <option value="won">Won — became a client 🎉</option>
      <option value="not_interested">Not interested</option>
      <option value="opted_out">Opted out — do not call again</option>
    </select>
    <div id="followup-row" style="display:none;margin-bottom:10px;">
      <label style="font-size:11px;color:#666;">Follow-up date</label><br>
      <input type="date" id="modal-followup" style="margin-top:4px;padding:6px 9px;border:.5px solid #ddd;border-radius:6px;font-size:12px;font-family:inherit;width:100%;">
    </div>
    <label style="font-size:11px;color:#666;">Notes</label>
    <textarea id="modal-notes" placeholder="e.g. Spoke with owner Maria, interested in SSL fix — call back Thursday..."></textarea>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="closeModal()">Cancel</button>
      <button class="btn-save" onclick="saveCall()">Save call</button>
    </div>
  </div>
</div>

<!-- Error detail modal -->
<div class="overlay" id="error-modal">
  <div class="modal">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
      <h2 id="error-modal-title">Site errors</h2>
      <div style="display:flex;gap:6px;">
        <button onclick="openReport()" style="padding:5px 12px;border-radius:6px;border:.5px solid #2563eb;background:#eff6ff;color:#1d4ed8;font-size:11px;cursor:pointer;font-family:inherit;font-weight:500;">📊 Full Report</button>
        <button id="browser-audit-btn" onclick="runBrowserAudit()" style="padding:5px 12px;border-radius:6px;border:.5px solid #187a4c;background:#eaf6f0;color:#187a4c;font-size:11px;cursor:pointer;font-family:inherit;font-weight:500;">🌐 Browser Audit</button>
      </div>
    </div>
    <div id="error-modal-body"></div>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="document.getElementById('error-modal').classList.remove('show')">Close</button>
    </div>
  </div>
</div>

<!-- Report card modal -->
<div class="overlay" id="report-modal">
  <div class="modal" style="width:620px;max-height:85vh;overflow-y:auto;">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
      <div>
        <h2 id="report-modal-title" style="font-size:15px;">Site Report</h2>
        <div id="report-modal-url" style="font-size:11px;color:#999;margin-top:2px;"></div>
      </div>
      <div style="text-align:center;">
        <div id="report-overall-grade" style="font-size:36px;font-weight:700;line-height:1;"></div>
        <div style="font-size:10px;color:#999;margin-top:2px;">Overall</div>
      </div>
    </div>
    <div id="report-sections" style="display:grid;grid-template-columns:1fr 1fr;gap:10px;"></div>
    <div class="modal-footer" style="margin-top:16px;">
      <button class="btn-cancel" onclick="document.getElementById('report-modal').classList.remove('show')">Close</button>
    </div>
  </div>
</div>

<script>
function log(msg, cls='') {
  const l = document.getElementById('log');
  const p = document.createElement('p');
  if(cls) p.className = 'l-'+cls;
  p.textContent = '[' + new Date().toLocaleTimeString() + '] ' + msg;
  l.appendChild(p);
  l.scrollTop = l.scrollHeight;
}

function showToast(title, body, type='success') {
  const toast = document.getElementById('toast');
  const icons = {success:'✅', warning:'⚠️', error:'❌', info:'ℹ️'};
  document.getElementById('toast-icon').textContent = icons[type] || '✅';
  document.getElementById('toast-title').textContent = title;
  document.getElementById('toast-body').textContent = body;
  toast.className = 'toast ' + type;
  toast.classList.add('show');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toast.classList.remove('show'), 5000);
}

function setStatus(txt, mode='idle') {
  const pill = document.getElementById('status-pill');
  const dot  = pill.querySelector('.dot');
  document.getElementById('status-text').textContent = txt;
  pill.className = 'status-pill ' + (mode==='busy' ? 'orange' : mode==='active' ? 'blue' : '');
  dot.className  = 'dot ' + (mode==='busy' ? 'orange' : mode==='active' ? 'blue' : '');
}

async function loadMetrics() {
  const d = await fetch('/api/metrics').then(r=>r.json());
  document.getElementById('m-total').textContent   = d.total   || '0';
  document.getElementById('m-audited').textContent = d.audited || '0';
  document.getElementById('m-errors').textContent  = d.errors  || '0';
  document.getElementById('m-calls').textContent   = d.calls   || '0';
  // Check for timed out sites and show retry button if any
  const t = await fetch('/api/count_timed_out').then(r=>r.json());
  const retryBtn = document.getElementById('retry-btn');
  if (t.count > 0) {
    document.getElementById('timed-out-count').textContent = t.count;
    retryBtn.style.display = 'block';
  } else {
    retryBtn.style.display = 'none';
  }
}

async function runRetryTimedOut() {
  const mode = document.getElementById('auditmode-select').value;
  setStatus('Retrying timed out sites...', 'busy');
  log('Retrying timed out sites in ' + mode + ' mode...', 'info');
  const r = await fetch('/api/retry_timed_out',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})}).then(r=>r.json());
  if (r.message) {
    log(r.message, 'ok');
    showToast('No timed out sites', 'All sites have been audited', 'info');
  } else {
    log('Retry done. ' + r.retried + ' sites re-audited, ' + r.total_errors + ' errors found.', 'ok');
    showToast('Retry complete', r.retried + ' sites re-audited — ' + r.total_errors + ' errors found', 'success');
  }
  setStatus('Idle'); await loadMetrics(); await loadLeads();
}

function avatarClass(i) {
  return ['av-blue','av-amber','av-green','av-purple'][i % 4];
}
function initials(name) {
  return name.split(' ').slice(0,2).map(w=>w[0]||'').join('').toUpperCase() || '?';
}

var _allLeads = [];
var _avClasses = ['av-blue','av-amber','av-green','av-purple'];
function av(i) { return _avClasses[i % 4]; }
function ini(name) { return (name||'').split(' ').slice(0,2).map(function(w){return w[0]||'';}).join('').toUpperCase() || '?'; }

function filterLeads() {
  const search  = (document.getElementById('filter-search').value || '').toLowerCase();
  const errFilt = document.getElementById('filter-errors').value;
  const sort    = document.getElementById('filter-sort').value;

  let leads = _allLeads.filter(function(l){ return l.pipeline_stage !== 'won' && l.pipeline_stage !== 'blocked'; });

  // Filter by search
  if (search) {
    leads = leads.filter(function(l) {
      return (l.name||'').toLowerCase().includes(search)
          || (l.website||'').toLowerCase().includes(search);
    });
  }

  // Filter by error status
  if (errFilt === 'errors')    leads = leads.filter(function(l){ return l.error_count > 0; });
  if (errFilt === 'critical')  leads = leads.filter(function(l){ return l.error_count >= 3; });
  if (errFilt === 'clean')     leads = leads.filter(function(l){ return l.audited && l.error_count === 0; });
  if (errFilt === 'unaudited') leads = leads.filter(function(l){ return !l.audited; });

  // Sort
  if (sort === 'errors_desc') leads.sort(function(a,b){ return (b.error_count||0) - (a.error_count||0); });
  if (sort === 'errors_asc')  leads.sort(function(a,b){ return (a.error_count||0) - (b.error_count||0); });
  if (sort === 'name')        leads.sort(function(a,b){ return (a.name||'').localeCompare(b.name||''); });

  document.getElementById('leads-count').textContent = leads.length + ' of ' + _allLeads.filter(function(l){ return l.pipeline_stage !== 'won' && l.pipeline_stage !== 'blocked'; }).length + ' leads';
  renderLeadsTable(leads);
}

async function loadLeads() {
  const leads = await fetch('/api/leads').then(r=>r.json());
  const tbody = document.getElementById('leads-table');
  const preview = document.getElementById('leads-preview');

  _allLeads = leads;
  if (!leads.length) {
    document.getElementById('leads-table').innerHTML = '<tr><td colspan="6" style="color:#bbb;text-align:center;padding:2rem;">No leads yet — run a discovery scan to get started</td></tr>';
    preview.innerHTML = '<div style="color:#bbb;font-size:11px;padding:1rem 0;text-align:center;">Run a scan to see leads here</div>';
    document.getElementById('leads-count').textContent = '';
    return;
  }

  preview.innerHTML = leads.slice(0,4).map(function(l,i) {
    var ec = l.error_count || 0;
    var badges = ec === 0
      ? (l.audited ? '<span class="badge b-green">Clean</span>' : '<span class="badge b-gray">Not audited</span>')
      : (ec >= 3 ? '<span class="badge b-red">'+ec+' errors</span>' : '<span class="badge b-yellow">'+ec+' warnings</span>');
    return '<div class="lead-row">'
      + '<div class="avatar ' + av(i) + '">' + ini(l.name) + '</div>'
      + '<div class="lead-meta">'
      + '<div class="lead-name">' + l.name + '</div>'
      + '<div class="lead-url">' + (l.website||'').replace(/https?:\/\//g, '').slice(0,40) + '</div>'
      + '<div class="badges">' + badges + '</div>'
      + '</div>'
      + '<button class="call-btn" onclick="openCallModal(' + l.id + ',&quot;' + encodeURIComponent(l.name) + '&quot;)">Call</button>'
      + '</div>';
  }).join('');

  filterLeads();
}

function renderLeadsTable(leads) {
  const tbody = document.getElementById('leads-table');
  if (!leads.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="color:#bbb;text-align:center;padding:1.5rem;">No leads match your filter</td></tr>';
    return;
  }
  tbody.innerHTML = leads.map(function(l,i) {
    var ec = l.error_count || 0;
    var errBadge = ec === 0
      ? (l.audited ? '<span class="badge b-green">Clean</span>' : '<span class="badge b-gray">Not audited</span>')
      : (ec >= 3 ? '<span class="badge b-red">'+ec+' errors</span>' : '<span class="badge b-yellow">'+ec+' warnings</span>');
    var callBadge = l.call_outcome
      ? '<span class="badge ' + (l.call_outcome==='booked'||l.call_outcome==='interested' ? 'b-green' : l.call_outcome==='opted_out' ? 'b-red' : 'b-gray') + '">' + l.call_outcome.replace('_',' ') + '</span>'
      : '<span style="color:#ccc;">-</span>';
    return '<tr>'
      + '<td><div style="display:flex;align-items:center;gap:7px;">'
      + '<div class="avatar ' + av(i) + '" style="width:26px;height:26px;font-size:9px;">' + ini(l.name) + '</div>'
      + '<div><div style="font-weight:500;color:#111;">' + l.name + '</div>'
      + '<div style="font-size:10px;color:#bbb;">' + (l.address||'') + '</div></div></div></td>'
      + '<td><a href="' + l.website + '" target="_blank" style="color:#2563eb;font-size:10px;">' + (l.website||'').replace(/https?:\/\//g, '').slice(0,35) + '</a></td>'
      + '<td><span style="color:#999;">' + l.category + '</span></td>'
      + '<td>' + errBadge + (ec > 0 ? '<button class="call-btn" style="margin-left:4px;" onclick="showErrors('+l.id+',&quot;'+encodeURIComponent(l.name)+'&quot;)">View</button>' : '') + '</td>'
      + '<td>' + callBadge + '</td>'
      + '<td style="display:flex;gap:4px;">'
      + (!l.audited ? '<button class="call-btn" onclick="auditOne('+l.id+')">Audit</button>' : '')
      + '<button class="call-btn" style="color:#2563eb;" onclick="openCallModal(' + l.id + ',&quot;' + encodeURIComponent(l.name) + '&quot;)">Call</button>'
      + (l.audited && l.error_count > 0 ? '<button class="call-btn" style="color:#187a4c;border-color:#b6dfc9;" onclick="showScript('+l.id+',&quot;'+encodeURIComponent(l.name)+'&quot;)">Script</button>' : '')
      + (l.audited && l.error_count > 0 ? '<button class="call-btn" style="color:#6d28d9;border-color:#ddd8fe;" onclick="showEmail('+l.id+',&quot;'+encodeURIComponent(l.name)+'&quot;)">Email</button>' : '')
      + '<button class="call-btn" style="color:#187a4c;background:#eaf6f0;border-color:#b6dfc9;font-weight:500;" data-wonid="' + l.id + '" data-wonname="' + encodeURIComponent(l.name) + '" onclick="handleWonClick(this)">' + (l.pipeline_stage === 'won' ? 'View client' : 'Won ✓') + '</button>'
      + '</td></tr>';
  }).join('');
}

async function handleCSV(input) {
  const file = input.files[0];
  if (!file) return;
  document.getElementById('csv-status').textContent = 'Uploading...';
  document.getElementById('csv-drop-label').textContent = '📂 ' + file.name;

  const formData = new FormData();
  formData.append('file', file);

  try {
    const r = await fetch('/api/import_csv', {
      method: 'POST',
      body: formData
    }).then(r=>r.json());

    if (r.error) {
      document.getElementById('csv-status').textContent = 'Error: ' + r.error;
      showToast('Import failed', r.error, 'error');
      return;
    }

    const msg = r.added + ' added, ' + r.skipped + ' skipped';
    document.getElementById('csv-status').textContent = msg;
    showToast('CSV imported', msg, r.added > 0 ? 'success' : 'info');
    await loadMetrics();
    await loadLeads();
  } catch(e) {
    document.getElementById('csv-status').textContent = 'Upload failed';
    showToast('Upload failed', e.message, 'error');
  }

  // Reset file input so same file can be re-uploaded
  input.value = '';
}

async function addManual() {
  const name    = document.getElementById('manual-name').value.trim();
  const website = document.getElementById('manual-website').value.trim();
  const phone   = document.getElementById('manual-phone').value.trim();
  const address = document.getElementById('manual-address').value.trim();

  if (!name || !website) {
    showToast('Missing fields', 'Name and website are required', 'error');
    return;
  }

  const r = await fetch('/api/add_manual', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name, website, phone, address})
  }).then(r=>r.json());

  if (r.error) {
    showToast('Error', r.error, 'error');
    return;
  }

  showToast(r.ok ? 'Business added' : 'Already exists', r.message, r.ok ? 'success' : 'info');

  if (r.ok) {
    // Clear form
    document.getElementById('manual-name').value = '';
    document.getElementById('manual-website').value = '';
    document.getElementById('manual-phone').value = '';
    document.getElementById('manual-address').value = '';
  }

  await loadMetrics();
  await loadLeads();
}

async function runDiscover() {
  const city = document.getElementById('city').value.trim();
  const cat  = document.getElementById('category').value;
  const max  = parseInt(document.getElementById('max_results').value);
  const srcVal = document.getElementById('sources-select').value;
  const sources = srcVal === 'all' ? ['osm','yellowpages'] : [srcVal];
  if (!city) { alert('Enter a city name.'); return; }
  setStatus('Discovering...', 'busy');
  log('Searching [' + sources.join(', ') + '] for [' + cat + '] in [' + city + ']...', 'info');
  const d = await fetch('/api/discover',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({city,category:cat,max_results:max,sources})}).then(r=>r.json());
  if (d.messages && d.messages.length) {
    d.messages.forEach(function(m) { log(m, 'info'); });
  }
  log('Total: ' + d.found + ' businesses found, ' + d.added + ' new leads saved.', d.added > 0 ? 'ok' : '');
  if (d.found === 0) showToast('No results', 'Overpass may be busy — try Yellow Pages source or try again in a minute', 'warning');
  showToast('Discovery complete', d.found + ' businesses found — ' + d.added + ' new leads saved', d.added > 0 ? 'success' : 'info');
  setStatus('Idle');
  await loadMetrics(); await loadLeads();
}

async function auditOne(id) {
  setStatus('Auditing...', 'busy');
  log('Auditing business #' + id + '...', 'info');
  const d = await fetch('/api/audit/'+id,{method:'POST'}).then(r=>r.json());
  log('Audit done: ' + d.error_count + ' error(s).', d.error_count > 0 ? 'err' : 'ok');
  setStatus('Idle'); await loadMetrics(); await loadLeads();
}

async function runAuditAll() {
  setStatus('Auditing all...', 'active');
  log('Auditing all unaudited sites — this may take a while...', 'info');
  const d = await fetch('/api/audit_all',{method:'POST'}).then(r=>r.json());
  log('Done. ' + d.audited + ' sites audited, ' + d.total_errors + ' errors found.', 'ok');
  setStatus('Idle'); await loadMetrics(); await loadLeads();
}

var _currentErrorUrl = '';
var _currentBizId = null;

async function runBrowserAudit() {
  if (!_currentBizId) return;
  const btn = document.getElementById('browser-audit-btn');
  btn.textContent = '⏳ Running...';
  btn.disabled = true;
  log('Running browser audit (Playwright)...', 'info');

  try {
    const r = await fetch('/api/browser_audit/' + _currentBizId, {method:'POST'}).then(r=>r.json());
    if (r.error) {
      showToast('Browser audit failed', r.error, 'error');
      log('Browser audit error: ' + r.error, 'err');
    } else {
      showToast('Browser audit complete',
        'Desktop: ' + (r.desktop_score||'?') + '/100 · Mobile: ' + (r.mobile_score||'?') + '/100',
        r.desktop_score >= 70 ? 'success' : 'warning');
      log('Browser audit done — desktop: ' + r.desktop_score + '/100, mobile: ' + r.mobile_score + '/100', 'ok');
    }
  } catch(e) {
    showToast('Browser audit failed', e.message, 'error');
  }

  btn.textContent = '🌐 Browser Audit';
  btn.disabled = false;
}

async function openReport() {
  if (!_currentBizId) return;
  document.getElementById('report-modal').classList.add('show');
  document.getElementById('report-sections').innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:2rem;color:#bbb;font-size:13px;">Loading report...</div>';

  const r = await fetch('/api/report/' + _currentBizId).then(r=>r.json());
  if (r.error) {
    document.getElementById('report-sections').innerHTML = '<div style="color:#c0392b;font-size:12px;padding:1rem;">Could not generate report — try re-auditing this site first.</div>';
    return;
  }

  document.getElementById('report-modal-title').textContent = 'Site Report — ' + r.name;
  document.getElementById('report-modal-url').textContent = r.website;
  const og = document.getElementById('report-overall-grade');
  og.textContent = r.overall;
  og.style.color = r.overall_color;

  document.getElementById('report-sections').innerHTML = r.sections.map(function(s) {
    const passCount = s.items.filter(function(i){ return i.pass; }).length;
    const items = s.items.map(function(item) {
      return '<div style="display:flex;align-items:flex-start;gap:6px;padding:4px 0;border-bottom:.5px solid #f2f2ee;">'
        + '<span style="flex-shrink:0;margin-top:1px;">' + (item.pass ? '✅' : '❌') + '</span>'
        + '<div><div style="font-size:11px;font-weight:500;color:#111;">' + item.label + '</div>'
        + '<div style="font-size:10px;color:#888;">' + item.detail + '</div></div>'
        + '</div>';
    }).join('');
    return '<div style="background:#f7f7f5;border-radius:8px;padding:12px;border:.5px solid #e2e2dc;">'
      + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">'
      + '<div style="font-size:13px;font-weight:600;">' + s.icon + ' ' + s.title + '</div>'
      + '<div style="font-size:20px;font-weight:700;color:' + s.color + ';">' + s.grade + '</div>'
      + '</div>'
      + '<div style="font-size:10px;color:#aaa;margin-bottom:6px;">' + passCount + '/' + s.items.length + ' checks passed</div>'
      + items
      + '</div>';
  }).join('');
}

async function showErrors(bizId, bizName) {
  const errors = await fetch('/api/errors/'+bizId).then(r=>r.json());
  document.getElementById('error-modal-title').textContent = 'Errors — ' + decodeURIComponent(bizName||'');
  // Store for report button
  const lead = _allLeads.find(function(l){ return l.id == bizId; });
  _currentErrorUrl = lead ? lead.website : '';
  _currentBizId = bizId;
  const body = document.getElementById('error-modal-body');
  const iconMap = {
    'ssl':'🔒','not_found':'❌','server_error':'💥','down':'🔌',
    'timeout':'⏱','timed_out':'⏱','mobile':'📱','seo':'🔍',
    'accessibility':'♿','broken_links':'🔗','outdated':'📅',
    'ux':'📝','client_error':'⚠️','compliance':'🤖','unknown':'❓','parse':'📄'
  };
  body.innerHTML = errors.length
    ? errors.map(function(e){
        var icon = iconMap[e.type] || '⚠️';
        var bg = e.severity==='critical' ? '#fdeaea' : e.severity==='warning' ? '#fff8e0' : '#eff6ff';
        var tc = e.severity==='critical' ? '#9b2020' : e.severity==='warning' ? '#7a5000' : '#1d4ed8';
        return '<div class="err-row">'
          + '<div style="width:32px;height:32px;border-radius:7px;background:'+bg+';display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0;">'+icon+'</div>'
          + '<div><div style="font-size:11px;font-weight:600;color:'+tc+';">'+e.type.replace(/_/g,' ')+'</div>'
          + '<div style="font-size:11px;color:#666;">'+e.message+'</div></div>'
          + '</div>';
      }).join('')
    : '<p style="color:#bbb;font-size:12px;padding:.5rem 0;">No errors recorded.</p>';
  document.getElementById('error-modal').classList.add('show');
}

async function showScript(bizId, bizName) {
  document.getElementById('script-modal-title').textContent = 'Cold call script — ' + decodeURIComponent(bizName||'');
  document.getElementById('script-body').textContent = 'Generating script...';
  document.getElementById('script-modal').classList.add('show');
  const r = await fetch('/api/script/' + bizId).then(r=>r.json());
  document.getElementById('script-body').textContent = r.script || 'Could not generate script.';
}

function copyScript() {
  const text = document.getElementById('script-body').textContent;
  navigator.clipboard.writeText(text).then(function() {
    showToast('Copied!', 'Script copied to clipboard', 'success');
  });
}

function openCallModal(id, name) {
  document.getElementById('modal-biz-id').value = id;
  document.getElementById('modal-biz-name').textContent = decodeURIComponent(name||'');
  document.getElementById('modal-notes').value = '';
  document.getElementById('call-modal').classList.add('show');
}
function closeModal() { document.getElementById('call-modal').classList.remove('show'); }

function toggleFollowup() {
  const outcome = document.getElementById('modal-outcome').value;
  const show = ['interested','booked','voicemail','no_answer'].includes(outcome);
  document.getElementById('followup-row').style.display = show ? 'block' : 'none';
}

async function showEmail(bizId, bizName) {
  document.getElementById('email-modal-title').textContent = 'Follow-up email — ' + decodeURIComponent(bizName||'');
  document.getElementById('email-body').textContent = 'Generating email...';
  document.getElementById('email-subject-line').textContent = '';
  document.getElementById('email-modal').classList.add('show');
  const r = await fetch('/api/email/' + bizId).then(r=>r.json());
  document.getElementById('email-body').textContent = r.email || 'Could not generate email.';
  document.getElementById('email-subject-line').textContent = 'Subject: ' + (r.subject || '');
}

function copyEmail() {
  const text = document.getElementById('email-body').textContent;
  navigator.clipboard.writeText(text).then(function() {
    showToast('Copied!', 'Email copied to clipboard', 'success');
  });
}

async function loadFollowups() {
  try {
    const r = await fetch('/api/followups').then(r=>r.json());
    const banner = document.getElementById('followups-banner');
    const list   = document.getElementById('followups-list');
    if (r.length === 0) {
      banner.style.display = 'none';
      return;
    }
    banner.style.display = 'block';
    list.innerHTML = r.map(function(f) {
      const overdue = f.followup_date < new Date().toISOString().slice(0,10);
      return '<div style="display:flex;align-items:center;gap:8px;padding:3px 0;">'
        + '<span>' + (overdue ? '🔴' : '🟡') + '</span>'
        + '<strong>' + f.name + '</strong>'
        + '<span style="color:#999;">—</span>'
        + '<span>' + f.followup_date + '</span>'
        + (f.notes ? '<span style="color:#aaa;">· ' + f.notes.slice(0,40) + '</span>' : '')
        + '<button class="call-btn" style="margin-left:auto;" onclick="openCallModal(' + f.id + ',&quot;'+encodeURIComponent(f.name)+'&quot;)">Log call</button>'
        + '</div>';
    }).join('');
  } catch(e) {}
}

async function saveCall() {
  const id           = document.getElementById('modal-biz-id').value;
  const outcome      = document.getElementById('modal-outcome').value;
  const notes        = document.getElementById('modal-notes').value;
  const followup     = document.getElementById('modal-followup').value;
  await fetch('/api/log_call',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({business_id:id, outcome, notes, followup_date: followup})});
  log('Call logged for #' + id + ': ' + outcome + (followup ? ' — follow-up: ' + followup : ''), 'ok');
  if (followup) showToast('Follow-up set', 'Reminder set for ' + followup, 'success');
  closeModal();
  await loadMetrics();
  await loadLeads();
  await loadFollowups();
}

async function exportCSV() {
  const leads = await fetch('/api/leads').then(r=>r.json());
  const rows = [['Name','Website','Category','Address','Errors','Call Status']];
  leads.forEach(l => rows.push([l.name, l.website, l.category, l.address||'', l.error_count, l.call_outcome||'']));
  const csv = rows.map(r=>r.map(v=>'"'+String(v).replace(/"/g,'""')+'"').join(',')).join('\\n');
  const a = document.createElement('a');
  a.href = 'data:text/csv,' + encodeURIComponent(csv);
  a.download = 'webaudit-leads.csv';
  a.click();
}

loadMetrics(); loadLeads(); loadFollowups();

// ── Tab switching ──
function showTab(name, btn) {
  document.querySelectorAll('.tab-content').forEach(function(el){ el.classList.remove('active'); });
  document.querySelectorAll('.tab-btn').forEach(function(b){ b.classList.remove('active'); });
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
  if (name === 'pipeline') loadPipeline();
  if (name === 'reassess') loadReassess();
  if (name === 'blocked')  loadBlocked();
}

// ── Pipeline / Kanban ──
const STAGE_META = {
  new:        {label:'New',        color:'#bbb',    cls:'stage-new'},
  contacted:  {label:'Contacted',  color:'#2563eb', cls:'stage-contacted'},
  interested: {label:'Interested', color:'#e07820', cls:'stage-interested'},
  booked:     {label:'Booked',     color:'#6d28d9', cls:'stage-booked'},
  won:        {label:'Won ✓',      color:'#187a4c', cls:'stage-won'},
  lost:       {label:'Lost',       color:'#c0392b', cls:'stage-lost'},
};

async function loadPipeline() {
  const grid = document.getElementById('clients-grid');
  if (!grid) return;
  grid.innerHTML = '<div style="color:#bbb;font-size:12px;text-align:center;grid-column:1/-1;padding:2rem;">Loading...</div>';
  let data;
  try {
    data = await fetch('/api/pipeline').then(r=>r.json());
  } catch(e) {
    grid.innerHTML = '<div style="color:#c0392b;font-size:12px;grid-column:1/-1;padding:2rem;">Error: ' + e.message + '</div>';
    return;
  }
  const clients = data['won'] || [];
  if (!clients.length) {
    grid.innerHTML = '<div style="color:#bbb;font-size:12px;text-align:center;grid-column:1/-1;padding:3rem;">'
      + '<div style="font-size:24px;margin-bottom:8px;">🎯</div>'
      + '<div style="font-weight:500;margin-bottom:4px;">No clients yet</div>'
      + '<div style="font-size:11px;">Mark a lead as Won in the Leads tab to see them here</div>'
      + '</div>';
    return;
  }
  grid.innerHTML = clients.map(function(c) {
    const ec = c.error_count || 0;
    const errBadge = ec > 0
      ? '<span style="font-size:9px;font-weight:600;padding:2px 7px;border-radius:20px;background:#fdeaea;color:#9b2020;">' + ec + ' errors</span>'
      : '<span style="font-size:9px;font-weight:600;padding:2px 7px;border-radius:20px;background:#eaf6f0;color:#187a4c;">Clean</span>';
    return '<div data-clientid="' + c.id + '" onclick="goToClient(this.dataset.clientid)" style="background:#fff;border:.5px solid #e2e2dc;border-left:3px solid #187a4c;border-radius:10px;padding:14px 16px;cursor:pointer;">'
      + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">'
      + '<div style="width:36px;height:36px;border-radius:50%;background:#eaf6f0;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;color:#187a4c;">' + (c.name||'').slice(0,2).toUpperCase() + '</div>'
      + errBadge
      + '</div>'
      + '<div style="font-size:13px;font-weight:600;color:#111;margin-bottom:3px;">' + c.name + '</div>'
      + '<div style="font-size:10px;color:#bbb;margin-bottom:10px;">' + (c.website||'').replace(/https?:\/\//, '').slice(0,40) + '</div>'
      + '<div style="margin-top:10px;padding-top:10px;border-top:.5px solid #f2f2ee;font-size:10px;color:#2563eb;font-weight:500;">View full dashboard →</div>'
      + '</div>';
  }).join('');
}

// Stage move modal
var _stageModalId = null;
function handleWonClick(el) {
  var id   = el.getAttribute('data-wonid');
  var name = el.getAttribute('data-wonname');
  var lead = _allLeads.find(function(l){ return String(l.id) === String(id); });
  if (lead && lead.pipeline_stage === 'won') {
    window.location.href = 'http://localhost:5000/client/' + id;
  } else {
    markAsWon(id, name);
  }
}

async function markAsWon(id, name) {
  if (!confirm('Mark ' + decodeURIComponent(name) + ' as a won client?')) return;
  await fetch('/api/set_stage/' + id, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({stage: 'won'})
  });
  showToast('Client won! 🎉', decodeURIComponent(name) + ' moved to Clients', 'success');
  log('Won: ' + decodeURIComponent(name) + ' — moved to Clients tab', 'ok');
  await loadLeads();
  await loadMetrics();
}

function goToClient(id) {
  // Open client dashboard - works in both browser and pywebview
  var w = window.open('http://localhost:5000/client/' + id, '_blank');
  if (!w) {
    // Fallback for pywebview
    window.location.href = 'http://localhost:5000/client/' + id;
  }
}

function handleKanbanClick(el) {
  var id    = el.getAttribute('data-id');
  var stage = el.getAttribute('data-stage');
  var name  = el.getAttribute('data-name');
  if (stage === 'won') {
    goToClient(id);
  } else {
    openStageModal(id, stage, name);
  }
}

function openStageModal(id, currentStage, name) {
  _stageModalId = id;
  var stages = Object.keys(STAGE_META).map(function(s) {
    return '<option value="' + s + '"' + (s === currentStage ? ' selected' : '') + '>' + STAGE_META[s].label + '</option>';
  }).join('');
  var modal = document.createElement('div');
  modal.id = 'stage-modal-overlay';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:200;display:flex;align-items:center;justify-content:center;';
  modal.innerHTML = '<div style="background:#fff;border-radius:10px;padding:1.5rem;width:320px;">'
    + '<h2 style="font-size:14px;font-weight:600;margin-bottom:12px;">Move ' + decodeURIComponent(name) + '</h2>'
    + '<select id="stage-select" style="width:100%;padding:7px 9px;border:.5px solid #ddd;border-radius:6px;font-size:12px;margin-bottom:12px;">' + stages + '</select>'
    + '<div style="display:flex;gap:8px;justify-content:flex-end;">'
    + '<button onclick="closeStageModal()" style="padding:6px 14px;border-radius:6px;border:.5px solid #ddd;background:#f7f7f5;font-size:12px;cursor:pointer;">Cancel</button>'
    + '<button onclick="moveStage()" style="padding:6px 14px;border-radius:6px;border:none;background:#2563eb;color:#fff;font-size:12px;cursor:pointer;">Move</button>'
    + '</div></div>';
  document.body.appendChild(modal);
}
function closeStageModal() {
  var el = document.getElementById('stage-modal-overlay');
  if (el) el.remove();
}
async function moveStage() {
  const stage = document.getElementById('stage-select').value;
  await fetch('/api/set_stage/' + _stageModalId, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({stage})
  });
  if (stage === 'won') {
    window.location.href = 'http://localhost:5000/client/' + _stageModalId;
    return;
  }
  closeStageModal();
  await loadPipeline();
  await loadLeads();
}

// ── Reassess tab ──
async function loadReassess() {
  const data = await fetch('/api/reassess').then(r=>r.json());
  const el = document.getElementById('reassess-list');
  if (!data.length) {
    el.innerHTML = '<div style="color:#bbb;font-size:12px;text-align:center;padding:2rem;">No leads ready for reassessment yet.<br><span style="font-size:10px;">Leads appear here when they said not interested 90+ days ago and now have new errors.</span></div>';
    return;
  }
  el.innerHTML = data.map(function(r) {
    return '<div class="rea-row">'
      + '<div style="flex:1;">'
      + '<div style="font-size:12px;font-weight:500;">' + r.name + '</div>'
      + '<div style="font-size:10px;color:#bbb;">' + (r.website||'').replace(/https?:\/\//, '') + '</div>'
      + '</div>'
      + '<span style="font-size:9px;font-weight:600;padding:2px 8px;border-radius:20px;background:#fdeaea;color:#9b2020;">' + r.error_count + ' errors</span>'
      + '<span style="font-size:10px;color:#bbb;">Last contacted: ' + (r.called_at||'').slice(0,10) + '</span>'
      + '<button class="btn-small" onclick="openCallModal(' + r.id + ',&quot;' + encodeURIComponent(r.name) + '&quot;)" style="font-size:10px;">📞 Re-approach</button>'
      + '</div>';
  }).join('');
}

// ── Blocked tab ──
async function reauditLost() {
  const btn = document.getElementById('reaudit-lost-btn');
  btn.textContent = '⏳ Re-auditing...';
  btn.disabled = true;
  log('Re-auditing all lost leads...', 'info');
  const mode = document.getElementById('auditmode-select').value;
  const r = await fetch('/api/reaudit_lost', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mode})
  }).then(r=>r.json());
  if (r.message) {
    log(r.message, 'info');
    showToast('Nothing to re-audit', r.message, 'info');
  } else {
    log('Re-audit done — ' + r.audited + ' sites checked, ' + r.total_errors + ' errors found.', 'ok');
    showToast('Re-audit complete', r.audited + ' lost leads re-audited — ' + r.total_errors + ' errors found', r.total_errors > 0 ? 'warning' : 'success');
  }
  btn.textContent = '🔄 Re-audit all lost leads';
  btn.disabled = false;
  await loadReassess();
  await loadMetrics();
}

async function loadBlocked() {
  const data = await fetch('/api/blocklist').then(r=>r.json());
  const el = document.getElementById('blocked-list');
  if (!data.length) {
    el.innerHTML = '<div style="color:#bbb;font-size:12px;text-align:center;padding:2rem;">No blocked contacts.</div>';
    return;
  }
  el.innerHTML = data.map(function(r) {
    return '<div class="blk-row">'
      + '<div style="flex:1;">'
      + '<div style="font-size:12px;font-weight:500;">' + r.name + '</div>'
      + '<div style="font-size:10px;color:#bbb;">' + (r.website||'').replace(/https?:\/\//, '') + '</div>'
      + '</div>'
      + '<span style="font-size:9px;padding:2px 8px;border-radius:20px;background:#fdeaea;color:#9b2020;">Opted out</span>'
      + '<span style="font-size:10px;color:#bbb;">' + (r.called_at||'').slice(0,10) + '</span>'
      + '</div>';
  }).join('');
}

// Run canary check on page load
async function checkCanary() {
  const pill = document.getElementById('canary-pill');
  const icon = document.getElementById('canary-icon');
  const text = document.getElementById('canary-text');
  pill.style.display = 'flex';
  try {
    const r = await fetch('/api/canary').then(r=>r.json());
    if (r.status === 'ok') {
      icon.textContent = '✅';
      text.textContent = 'YP healthy';
      pill.style.background = '#eaf6f0';
      pill.style.color = '#187a4c';
      pill.style.borderColor = '#b6dfc9';
      setTimeout(() => { pill.style.display = 'none'; }, 5000);
    } else if (r.status === 'healed') {
      icon.textContent = '🔧';
      text.textContent = 'YP self-healed — restart server';
      pill.style.background = '#fff8e0';
      pill.style.color = '#7a5000';
      pill.style.borderColor = '#f0d060';
      showToast('Canary self-healed', 'Yellow Pages selector updated — restart server to apply', 'warning');
    } else if (r.status === 'blocked') {
      icon.textContent = '🚫';
      text.textContent = 'YP blocked';
      pill.style.background = '#fdeaea';
      pill.style.color = '#9b2020';
      pill.style.borderColor = '#f5c0c0';
    } else if (r.status === 'unreachable') {
      icon.textContent = '📡';
      text.textContent = 'YP unreachable';
      pill.style.background = '#f7f7f5';
      pill.style.color = '#888';
    } else {
      icon.textContent = '❌';
      text.textContent = 'YP broken — needs fix';
      pill.style.background = '#fdeaea';
      pill.style.color = '#9b2020';
      pill.style.borderColor = '#f5c0c0';
      showToast('Yellow Pages broken', 'Scraper needs attention — check terminal for details', 'error');
    }
  } catch(e) {
    pill.style.display = 'none';
  }
}
checkCanary();
</script>
</body>
</html>
"""

def browser_audit(url: str) -> dict:
    """
    Full browser audit using Playwright/Chromium.
    Measures real Core Web Vitals, JS errors, render blocking resources.
    Takes 5-15 seconds per site. Run on individual warm leads only.
    """
    result = {
        "url": url,
        "lcp_ms": None,       # Largest Contentful Paint
        "cls_score": None,    # Cumulative Layout Shift
        "fid_ms": None,       # First Input Delay (approx via TBT)
        "ttfb_ms": None,      # Time to First Byte
        "fcp_ms": None,       # First Contentful Paint
        "desktop_score": None,
        "mobile_score": None,
        "js_errors": [],
        "render_blocking": [],
        "total_requests": None,
        "total_transfer_kb": None,
        "error": None,
    }

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            # ── Desktop audit ──
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            js_errors = []
            page.on("pageerror", lambda err: js_errors.append(str(err)))

            requests_made = []
            transfer_kb = [0]

            def on_response(response):
                try:
                    requests_made.append(response.url)
                    headers = response.headers
                    cl = headers.get("content-length")
                    if cl:
                        transfer_kb[0] += int(cl) / 1024
                except Exception:
                    pass

            page.on("response", on_response)

            # Navigate and wait for network idle
            try:
                page.goto(url, wait_until="networkidle", timeout=20000)
            except Exception:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                except Exception as e:
                    result["error"] = str(e)
                    browser.close()
                    return result

            # Collect Web Vitals via JS
            vitals = page.evaluate("""() => {
                return new Promise((resolve) => {
                    let lcp = 0, cls = 0, fcp = 0, ttfb = 0;
                    
                    // TTFB from Navigation Timing
                    const nav = performance.getEntriesByType('navigation')[0];
                    if (nav) ttfb = Math.round(nav.responseStart - nav.requestStart);
                    
                    // FCP from Paint Timing
                    const paints = performance.getEntriesByType('paint');
                    const fcpEntry = paints.find(p => p.name === 'first-contentful-paint');
                    if (fcpEntry) fcp = Math.round(fcpEntry.startTime);
                    
                    // LCP via PerformanceObserver
                    try {
                        new PerformanceObserver((list) => {
                            const entries = list.getEntries();
                            if (entries.length) lcp = Math.round(entries[entries.length-1].startTime);
                        }).observe({type: 'largest-contentful-paint', buffered: true});
                    } catch(e) {}
                    
                    // CLS via PerformanceObserver
                    try {
                        new PerformanceObserver((list) => {
                            list.getEntries().forEach(e => { cls += e.value; });
                        }).observe({type: 'layout-shift', buffered: true});
                    } catch(e) {}
                    
                    // Wait a moment for observers to collect
                    setTimeout(() => resolve({lcp, cls: Math.round(cls * 1000)/1000, fcp, ttfb}), 1500);
                });
            }""")

            result["lcp_ms"]    = vitals.get("lcp") or None
            result["cls_score"] = vitals.get("cls") or None
            result["fcp_ms"]    = vitals.get("fcp") or None
            result["ttfb_ms"]   = vitals.get("ttfb") or None
            result["js_errors"] = js_errors[:5]
            result["total_requests"] = len(requests_made)
            result["total_transfer_kb"] = round(transfer_kb[0], 1)

            # Find render-blocking resources
            blocking = page.evaluate("""() => {
                const entries = performance.getEntriesByType('resource');
                return entries
                    .filter(e => e.initiatorType === 'script' || e.initiatorType === 'link')
                    .filter(e => e.renderBlockingStatus === 'blocking')
                    .map(e => e.name.split('/').pop().split('?')[0])
                    .slice(0, 5);
            }""")
            result["render_blocking"] = blocking or []

            # Desktop score (simplified — based on LCP and CLS thresholds)
            lcp = result["lcp_ms"] or 9999
            cls = result["cls_score"] or 0
            score = 100
            if lcp > 4000:  score -= 40
            elif lcp > 2500: score -= 20
            elif lcp > 1200: score -= 10
            if cls > 0.25:  score -= 30
            elif cls > 0.1:  score -= 15
            elif cls > 0.05: score -= 5
            if js_errors:   score -= len(js_errors) * 5
            result["desktop_score"] = max(0, min(100, score))

            browser.close()

            # ── Mobile audit ──
            mobile_browser = p.chromium.launch(headless=True)
            mobile_context = mobile_browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
                device_scale_factor=3,
                is_mobile=True,
            )
            mobile_page = mobile_context.new_page()
            mobile_js_errors = []
            mobile_page.on("pageerror", lambda err: mobile_js_errors.append(str(err)))

            try:
                mobile_page.goto(url, wait_until="networkidle", timeout=20000)
            except Exception:
                try:
                    mobile_page.goto(url, wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    mobile_browser.close()
                    return result

            mobile_vitals = mobile_page.evaluate("""() => {
                return new Promise((resolve) => {
                    let lcp = 0, cls = 0;
                    try { new PerformanceObserver((l) => { const e = l.getEntries(); if(e.length) lcp = Math.round(e[e.length-1].startTime); }).observe({type:'largest-contentful-paint',buffered:true}); } catch(e){}
                    try { new PerformanceObserver((l) => { l.getEntries().forEach(e => cls += e.value); }).observe({type:'layout-shift',buffered:true}); } catch(e){}
                    setTimeout(() => resolve({lcp, cls: Math.round(cls*1000)/1000}), 1500);
                });
            }""")

            m_lcp = mobile_vitals.get("lcp") or 9999
            m_cls = mobile_vitals.get("cls") or 0
            m_score = 100
            if m_lcp > 4000:  m_score -= 40
            elif m_lcp > 2500: m_score -= 20
            elif m_lcp > 1200: m_score -= 10
            if m_cls > 0.25:  m_score -= 30
            elif m_cls > 0.1:  m_score -= 15
            if mobile_js_errors: m_score -= len(mobile_js_errors) * 5
            result["mobile_score"] = max(0, min(100, m_score))

            mobile_browser.close()

    except ImportError:
        result["error"] = "Playwright not installed. Run: pip install playwright && playwright install chromium"
    except Exception as e:
        result["error"] = str(e)

    return result

# ─────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────

@app.route("/")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype='text/html')

@app.route("/api/canary")
def api_canary():
    """Run canary check and return status."""
    result = run_canary_check()
    return jsonify(result)

@app.route("/api/metrics")
def api_metrics():
    con = get_db()
    cur = con.cursor()
    total   = cur.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
    audited = cur.execute("SELECT COUNT(DISTINCT business_id) FROM audits").fetchone()[0]
    errors  = cur.execute(
        "SELECT SUM(json_array_length(errors)) FROM audits WHERE errors != '[]'"
    ).fetchone()[0] or 0
    calls   = cur.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    con.close()
    return jsonify(total=total, audited=audited, errors=errors, calls=calls)

@app.route("/api/leads")
def api_leads():
    con = get_db()
    cur = con.cursor()
    rows = cur.execute("""
        SELECT
            b.id, b.name, b.website, b.address, b.category,
            COALESCE(b.pipeline_stage, 'new') as pipeline_stage,
            a.errors,
            (SELECT outcome FROM calls WHERE calls.business_id = b.id ORDER BY calls.id DESC LIMIT 1) as call_outcome,
            (a.id IS NOT NULL) as audited
        FROM businesses b
        LEFT JOIN (
            SELECT business_id, MAX(id) as id, errors
            FROM audits GROUP BY business_id
        ) a ON a.business_id = b.id
        WHERE COALESCE(b.pipeline_stage,'new') NOT IN ('won','blocked')
        ORDER BY b.id DESC
    """).fetchall()
    con.close()
    result = []
    for r in rows:
        errors = json.loads(r["errors"]) if r["errors"] else []
        result.append({
            "id":             r["id"],
            "name":           r["name"],
            "website":        r["website"],
            "address":        r["address"],
            "category":       r["category"],
            "error_count":    len(errors),
            "call_outcome":   r["call_outcome"],
            "audited":        bool(r["audited"]),
            "pipeline_stage": r["pipeline_stage"] if "pipeline_stage" in r.keys() else "new",
        })
    return jsonify(result)

@app.route("/api/errors/<int:biz_id>")
def api_errors(biz_id):
    con = get_db()
    cur = con.cursor()
    row = cur.execute(
        "SELECT errors FROM audits WHERE business_id = ? ORDER BY id DESC LIMIT 1",
        (biz_id,)
    ).fetchone()
    con.close()
    if not row:
        return jsonify([])
    return jsonify(json.loads(row["errors"]))

@app.route("/api/discover", methods=["POST"])
def api_discover():
    data     = request.get_json(force=True, silent=True) or {}
    city     = data.get("city", "")
    category = data.get("category", "all")
    max_r    = data.get("max_results", 30)
    sources  = data.get("sources", ["osm"])

    all_businesses = []
    messages = []

    if "osm" in sources:
        osm_results = discover_businesses(city, category, max_r)
        all_businesses += osm_results
        if osm_results:
            messages.append(f"OSM: {len(osm_results)} found")
        else:
            messages.append("OSM: 0 found (Overpass may be busy — try again or use Yellow Pages)")

    if "yellowpages" in sources:
        yp_results = discover_yellowpages(city, category, max_r)
        all_businesses += yp_results
        messages.append(f"Yellow Pages: {len(yp_results)} found")

    if "canada411" in sources:
        c411_results = discover_canada411(city, category, max_r)
        all_businesses += c411_results
        messages.append(f"Canada411: {len(c411_results)} found")

    added = save_businesses(all_businesses)
    return jsonify(found=len(all_businesses), added=added, messages=messages)

@app.route("/api/retry_timed_out", methods=["POST"])
def api_retry_timed_out():
    """Re-audit all sites that previously timed out."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        mode = data.get("mode", "quick")
    except Exception:
        mode = "quick"
    con = get_db()
    cur = con.cursor()
    # Find businesses whose latest audit contains a timed_out error
    rows = cur.execute("""
        SELECT b.id, b.website FROM businesses b
        JOIN (
            SELECT business_id, MAX(id) as max_id FROM audits GROUP BY business_id
        ) latest ON latest.business_id = b.id
        JOIN audits a ON a.id = latest.max_id
        WHERE a.errors LIKE '%timed_out%'
    """).fetchall()
    con.close()
    rows = [dict(r) for r in rows]
    if not rows:
        return jsonify(retried=0, total_errors=0, message="No timed out sites found")
    # Delete old timed_out audit records so they get fresh ones
    con = get_db()
    cur = con.cursor()
    for row in rows:
        cur.execute("""
            DELETE FROM audits WHERE business_id = ? 
            AND errors LIKE '%timed_out%'
        """, (row["id"],))
    con.commit()
    con.close()
    audited, total_errors = audit_concurrent(rows, mode=mode, max_workers=5)
    return jsonify(retried=audited, total_errors=total_errors)

@app.route("/api/count_timed_out")
def api_count_timed_out():
    """Count how many sites have timed out."""
    con = get_db()
    cur = con.cursor()
    count = cur.execute("""
        SELECT COUNT(DISTINCT b.id) FROM businesses b
        JOIN (
            SELECT business_id, MAX(id) as max_id FROM audits GROUP BY business_id
        ) latest ON latest.business_id = b.id
        JOIN audits a ON a.id = latest.max_id
        WHERE a.errors LIKE '%timed_out%'
    """).fetchone()[0]
    con.close()
    return jsonify(count=count)

@app.route("/api/import_csv", methods=["POST"])
def api_import_csv():
    """Import businesses from an uploaded CSV file."""
    import csv, io
    if "file" not in request.files:
        return jsonify(error="No file uploaded"), 400
    file = request.files["file"]
    if not file.filename.endswith(".csv"):
        return jsonify(error="File must be a .csv"), 400

    try:
        content_str = file.read().decode("utf-8-sig")  # handle BOM
        reader = csv.DictReader(io.StringIO(content_str))
    except Exception as e:
        return jsonify(error=f"Could not read CSV: {e}"), 400

    # Flexible column name mapping
    def find_col(row, options):
        for opt in options:
            for key in row:
                if key.strip().lower() == opt.lower():
                    return row[key].strip()
        return ""

    added = 0
    skipped = 0
    errors = 0
    con = get_db()
    cur = con.cursor()

    for row in reader:
        try:
            name    = find_col(row, ["name","business","business_name","company","title"])
            website = find_col(row, ["website","url","web","site","webpage","link"])
            phone   = find_col(row, ["phone","tel","telephone","mobile","contact"])
            address = find_col(row, ["address","location","street","addr"])

            if not website:
                skipped += 1
                continue
            if not website.startswith("http"):
                website = "https://" + website
            if not name:
                name = website.replace("https://","").replace("http://","").split("/")[0]

            cur.execute("""
                INSERT OR IGNORE INTO businesses
                (name, website, phone, address, category, source, discovered)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (name, website, phone, address, "csv_import", "CSV Import",
                  datetime.now().isoformat()))
            if cur.rowcount:
                added += 1
            else:
                skipped += 1
        except Exception:
            errors += 1

    con.commit()
    con.close()
    return jsonify(added=added, skipped=skipped, errors=errors)

@app.route("/api/add_manual", methods=["POST"])
def api_add_manual():
    """Manually add a business to the database."""
    data = request.get_json(force=True, silent=True) or {}
    name    = data.get("name", "").strip()
    website = data.get("website", "").strip()
    phone   = data.get("phone", "").strip()
    address = data.get("address", "").strip()

    if not name or not website:
        return jsonify(error="Name and website are required"), 400

    if not website.startswith("http"):
        website = "https://" + website

    con = get_db()
    cur = con.cursor()
    try:
        cur.execute("""
            INSERT OR IGNORE INTO businesses
            (name, website, phone, address, category, source, discovered)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (name, website, phone, address, "manual", "Manual entry", datetime.now().isoformat()))
        added = cur.rowcount
        con.commit()
        biz_id = cur.lastrowid if added else cur.execute(
            "SELECT id FROM businesses WHERE website = ?", (website,)
        ).fetchone()[0]
    except Exception as e:
        con.close()
        return jsonify(error=str(e)), 500
    con.close()

    if added:
        return jsonify(ok=True, id=biz_id, message=f"{name} added successfully")
    else:
        return jsonify(ok=False, id=biz_id, message=f"{name} already exists in database")

@app.route("/api/script/<int:biz_id>")
def api_script(biz_id):
    """Generate a cold call script based on the business's specific errors."""
    con = get_db()
    cur = con.cursor()
    biz = cur.execute("SELECT * FROM businesses WHERE id = ?", (biz_id,)).fetchone()
    audit = cur.execute("""
        SELECT errors FROM audits WHERE business_id = ? ORDER BY id DESC LIMIT 1
    """, (biz_id,)).fetchone()
    con.close()

    if not biz:
        return jsonify(error="Not found"), 404

    errors = json.loads(audit["errors"]) if audit else []
    name = biz["name"]
    website = biz["website"].replace("https://","").replace("http://","").rstrip("/")

    # Build error-specific talking points
    talking_points = []
    has_ssl = any(e["type"] == "ssl" for e in errors)
    has_404 = any(e["type"] in ["not_found","server_error","down"] for e in errors)
    has_mobile = any(e["type"] == "mobile" for e in errors)
    has_seo = any(e["type"] == "seo" for e in errors)
    has_images = any(e["type"] == "accessibility" for e in errors)
    has_links = any(e["type"] == "broken_links" for e in errors)
    has_outdated = any(e["type"] == "outdated" for e in errors)
    has_form = any(e["type"] == "ux" for e in errors)
    has_timeout = any(e["type"] in ["timeout","timed_out"] for e in errors)

    if has_ssl:
        talking_points.append("your site is showing a 'Not Secure' warning in browsers because the SSL certificate has expired — customers see this before they even read your content, and many will leave immediately")
    if has_404:
        talking_points.append("your homepage is returning an error — visitors who type in your web address are getting an error page instead of your business")
    if has_mobile:
        talking_points.append("your website isn't set up for mobile devices, so anyone visiting on a phone or tablet is seeing a broken layout — and over 60% of web traffic is mobile")
    if has_seo:
        talking_points.append("your site is missing some basic SEO tags that Google uses to rank and display your business in search results")
    if has_images:
        talking_points.append("several images on your site are missing descriptions, which affects both your Google ranking and accessibility for visually impaired visitors")
    if has_links:
        talking_points.append("there are some broken links on your site that lead to error pages, which looks unprofessional and frustrates visitors")
    if has_outdated:
        talking_points.append("your site's copyright notice appears to be several years out of date, which can make visitors question whether the business is still active")
    if has_form:
        talking_points.append("your site doesn't appear to have a contact form, which means customers have no easy way to reach you online")
    if has_timeout:
        talking_points.append("your website is loading very slowly — slow sites lose visitors quickly and rank lower on Google")

    if not talking_points:
        talking_points.append("your website could benefit from some general improvements to help attract more customers online")

    # Format talking points
    if len(talking_points) == 1:
        issues_text = talking_points[0]
    elif len(talking_points) == 2:
        issues_text = talking_points[0] + ", and also " + talking_points[1]
    else:
        issues_text = talking_points[0] + ". On top of that, " + ". Also, ".join(talking_points[1:])

    error_count = len(errors)
    severity = "a few things" if error_count <= 2 else "several issues" if error_count <= 4 else "quite a few problems"

    script = f"""COLD CALL SCRIPT — {name}
{'=' * 50}

OPENING
-------
"Hi, could I speak with the owner or manager please?"

[If asked who's calling:]
"My name is [YOUR NAME] from [YOUR COMPANY] — it's regarding your website."

PITCH
-----
"Hi [NAME], I'll keep this brief — I was doing some research on local businesses 
in the area and I came across your website at {website}.

I noticed {severity} that might be costing you customers online — specifically, 
{issues_text}.

We specialise in fixing exactly these kinds of issues for small businesses, 
usually within a few days and at a very reasonable cost.

I'm not trying to sell you anything on this call — I just wanted to make you 
aware of what I found. Would you have 10 minutes this week for a quick chat 
so I can show you exactly what I'm seeing?"

IF INTERESTED
-------------
"Great — I can send you a quick summary of the issues by email beforehand 
so you can see exactly what I'm talking about. What's the best email for you?"

IF NOT INTERESTED
-----------------
"No problem at all, I completely understand. If you ever want to take a look 
in the future, feel free to reach out. Have a great day!"

IF ASKS FOR PRICE
-----------------
"It really depends on what's involved — some of these fixes are very quick, 
others take a bit more work. That's why I'd love a 10-minute call first so 
I can give you an accurate idea. No obligation at all."

COMPLIANCE REMINDER
-------------------
✓ Identify yourself and your company at the start
✓ State clearly why you are calling  
✓ Honour any opt-out requests immediately
✓ Do not call numbers on the DNCL: https://lnnte-dncl.gc.ca
"""

    return jsonify(script=script, name=name, error_count=error_count)

@app.route("/api/audit/<int:biz_id>", methods=["POST"])
def api_audit_one(biz_id):
    try:
        data = request.get_json(force=True, silent=True) or {}
        mode = data.get("mode", "quick")
    except Exception:
        mode = "quick"
    con = get_db()
    cur = con.cursor()
    row = cur.execute("SELECT website FROM businesses WHERE id = ?", (biz_id,)).fetchone()
    con.close()
    if not row:
        return jsonify(error="Not found"), 404
    audit = audit_website(row["website"], mode=mode)
    save_audit(biz_id, audit)
    return jsonify(error_count=len(audit["errors"]), robots_ok=audit["robots_ok"])

@app.route("/api/audit_all", methods=["POST"])
def api_audit_all():
    try:
        data = request.get_json(force=True, silent=True) or {}
        mode = data.get("mode", "quick")
    except Exception:
        mode = "quick"
    con = get_db()
    cur = con.cursor()
    rows = cur.execute("""
        SELECT b.id, b.website FROM businesses b
        WHERE b.id NOT IN (SELECT DISTINCT business_id FROM audits)
    """).fetchall()
    con.close()
    rows = [dict(r) for r in rows]
    audited, total_errors = audit_concurrent(rows, mode=mode, max_workers=10)
    return jsonify(audited=audited, total_errors=total_errors)

# Map call outcomes to pipeline stages
OUTCOME_STAGE = {
    "no_answer":      "contacted",
    "voicemail":      "contacted",
    "interested":     "interested",
    "booked":         "booked",
    "not_interested": "lost",
    "opted_out":      "blocked",
    "won":            "won",
}

@app.route("/api/log_call", methods=["POST"])
def api_log_call():
    data = request.get_json(force=True, silent=True) or {}
    biz_id  = data["business_id"]
    outcome = data["outcome"]
    con  = get_db()
    cur  = con.cursor()
    cur.execute("""
        INSERT INTO calls (business_id, called_at, outcome, notes, followup_date)
        VALUES (?, ?, ?, ?, ?)
    """, (biz_id, datetime.now().isoformat(), outcome,
          data.get("notes", ""), data.get("followup_date", "")))

    # Auto-update pipeline stage
    new_stage = OUTCOME_STAGE.get(outcome)
    if new_stage:
        cur.execute("UPDATE businesses SET pipeline_stage = ? WHERE id = ?",
                    (new_stage, biz_id))

    # If won — create client record
    if outcome == "won":
        cur.execute("""
            INSERT OR IGNORE INTO clients (business_id, client_since, monthly_value, notes)
            VALUES (?, ?, 0, '')
        """, (biz_id, datetime.now().strftime("%Y-%m-%d")))

    con.commit()
    con.close()
    return jsonify(ok=True)

@app.route("/api/set_stage/<int:biz_id>", methods=["POST"])
def api_set_stage(biz_id):
    """Manually set pipeline stage — used by Kanban drag/drop."""
    data = request.get_json(force=True, silent=True) or {}
    stage = data.get("stage", "new")
    con = get_db()
    cur = con.cursor()
    cur.execute("UPDATE businesses SET pipeline_stage = ? WHERE id = ?", (stage, biz_id))
    if stage == "won":
        cur.execute("""
            INSERT OR IGNORE INTO clients (business_id, client_since, monthly_value, notes)
            VALUES (?, ?, 0, '')
        """, (biz_id, datetime.now().strftime("%Y-%m-%d")))
    con.commit()
    con.close()
    return jsonify(ok=True)

@app.route("/api/pipeline")
def api_pipeline():
    """Get all leads grouped by pipeline stage."""
    con = get_db()
    cur = con.cursor()
    rows = cur.execute("""
        SELECT b.id, b.name, b.website, b.phone, b.category,
               COALESCE(b.pipeline_stage, 'new') as stage,
               (SELECT outcome FROM calls WHERE calls.business_id = b.id ORDER BY calls.id DESC LIMIT 1) as last_outcome,
               (SELECT called_at FROM calls WHERE calls.business_id = b.id ORDER BY calls.id DESC LIMIT 1) as last_called,
               a.error_count
        FROM businesses b
        LEFT JOIN (
            SELECT a2.business_id, COUNT(*) as error_count
            FROM audits a2
            JOIN (SELECT business_id, MAX(id) as mid FROM audits GROUP BY business_id) m
              ON a2.id = m.mid AND a2.business_id = m.business_id
            WHERE json_array_length(a2.errors) > 0
            GROUP BY a2.business_id
        ) a ON a.business_id = b.id
        ORDER BY b.id DESC
    """).fetchall()
    con.close()

    stages = {"new":[], "contacted":[], "interested":[], "booked":[], "won":[], "lost":[], "blocked":[]}
    for r in rows:
        r = dict(r)
        stage = r.get("stage") or "new"
        if stage not in stages:
            stages["new"].append(r)
        else:
            stages[stage].append(r)
    return jsonify(stages)

@app.route("/api/reassess")
def api_reassess():
    """Get lost/not-interested leads older than 90 days that now have errors."""
    cutoff = (datetime.now() - __import__('datetime').timedelta(days=90)).strftime("%Y-%m-%d")
    con = get_db()
    cur = con.cursor()
    rows = cur.execute("""
        SELECT b.id, b.name, b.website, b.phone,
               c.called_at, c.outcome,
               json_array_length(a.errors) as error_count
        FROM businesses b
        JOIN (
            SELECT calls.business_id, MAX(calls.id) as mid, calls.called_at, calls.outcome
            FROM calls GROUP BY calls.business_id
        ) c ON c.business_id = b.id
        LEFT JOIN (
            SELECT a2.business_id, a2.errors FROM audits a2
            JOIN (SELECT business_id, MAX(id) as mid FROM audits GROUP BY business_id) m
              ON a2.id = m.mid AND a2.business_id = m.business_id
        ) a ON a.business_id = b.id
        WHERE b.pipeline_stage IN ('lost', 'new')
        AND c.outcome = 'not_interested'
        AND c.called_at < ?
        AND json_array_length(a.errors) > 0
        ORDER BY error_count DESC
    """, (cutoff,)).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/reaudit_lost", methods=["POST"])
def api_reaudit_lost():
    """Re-audit all businesses in Lost stage."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        mode = data.get("mode", "quick")
    except Exception:
        mode = "quick"
    con = get_db()
    cur = con.cursor()
    rows = cur.execute("""
        SELECT b.id, b.website FROM businesses b
        WHERE b.pipeline_stage = 'lost'
    """).fetchall()
    con.close()
    rows = [dict(r) for r in rows]
    if not rows:
        return jsonify(audited=0, total_errors=0, message="No lost leads to re-audit")
    audited, total_errors = audit_concurrent(rows, mode=mode, max_workers=10)
    return jsonify(audited=audited, total_errors=total_errors)

@app.route("/api/blocklist")
def api_blocklist():
    """Get all opted-out (blocked) businesses."""
    con = get_db()
    cur = con.cursor()
    rows = cur.execute("""
        SELECT b.id, b.name, b.website, b.phone,
               c.called_at, c.notes
        FROM businesses b
        JOIN (
            SELECT calls.business_id, MAX(calls.id) as mid, calls.called_at, calls.notes
            FROM calls GROUP BY calls.business_id
        ) c ON c.business_id = b.id
        WHERE b.pipeline_stage = 'blocked'
        ORDER BY c.called_at DESC
    """).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/tasks/<int:biz_id>")
def api_get_tasks(biz_id):
    con = get_db()
    cur = con.cursor()
    rows = cur.execute(
        "SELECT * FROM tasks WHERE business_id = ? ORDER BY completed ASC, due_date ASC, id DESC",
        (biz_id,)
    ).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/tasks", methods=["POST"])
def api_create_task():
    data = request.get_json(force=True, silent=True) or {}
    con = get_db()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO tasks (business_id, title, description, due_date, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (data["business_id"], data["title"], data.get("description",""),
          data.get("due_date",""), datetime.now().isoformat()))
    con.commit()
    task_id = cur.lastrowid
    con.close()
    return jsonify(ok=True, id=task_id)

@app.route("/api/tasks/<int:task_id>/complete", methods=["POST"])
def api_complete_task(task_id):
    data = request.get_json(force=True, silent=True) or {}
    done = data.get("completed", True)
    con = get_db()
    cur = con.cursor()
    cur.execute("UPDATE tasks SET completed = ?, completed_at = ? WHERE id = ?",
                (1 if done else 0, datetime.now().isoformat() if done else None, task_id))
    con.commit()
    con.close()
    return jsonify(ok=True)

@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def api_delete_task(task_id):
    con = get_db()
    cur = con.cursor()
    cur.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    con.commit()
    con.close()
    return jsonify(ok=True)

@app.route("/api/improvements/<int:biz_id>")
def api_get_improvements(biz_id):
    con = get_db()
    cur = con.cursor()
    rows = cur.execute(
        "SELECT * FROM improvements WHERE business_id = ? ORDER BY logged_at DESC",
        (biz_id,)
    ).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/improvements", methods=["POST"])
def api_log_improvement():
    data = request.get_json(force=True, silent=True) or {}
    con = get_db()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO improvements (business_id, title, description, before_score, after_score, logged_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (data["business_id"], data["title"], data.get("description",""),
          data.get("before_score"), data.get("after_score"),
          datetime.now().isoformat()))
    con.commit()
    con.close()
    return jsonify(ok=True)

@app.route("/api/client/<int:biz_id>")
def api_client(biz_id):
    """Get full client data for dashboard."""
    con = get_db()
    cur = con.cursor()
    biz = cur.execute("SELECT * FROM businesses WHERE id = ?", (biz_id,)).fetchone()
    client = cur.execute("SELECT * FROM clients WHERE business_id = ?", (biz_id,)).fetchone()
    tasks = cur.execute(
        "SELECT * FROM tasks WHERE business_id = ? ORDER BY completed ASC, due_date ASC",
        (biz_id,)
    ).fetchall()
    improvements = cur.execute(
        "SELECT * FROM improvements WHERE business_id = ? ORDER BY logged_at DESC",
        (biz_id,)
    ).fetchall()
    audits = cur.execute(
        "SELECT audited_at, json_array_length(errors) as error_count FROM audits WHERE business_id = ? ORDER BY id ASC",
        (biz_id,)
    ).fetchall()
    con.close()

    tasks_done = sum(1 for t in tasks if dict(t)["completed"])
    tasks_total = len(tasks)
    errors_fixed = len(improvements)

    return jsonify(
        business=dict(biz) if biz else {},
        client=dict(client) if client else {},
        tasks=[dict(t) for t in tasks],
        improvements=[dict(i) for i in improvements],
        audit_history=[dict(a) for a in audits],
        stats={
            "tasks_done": tasks_done,
            "tasks_total": tasks_total,
            "errors_fixed": errors_fixed,
            "client_since": dict(client)["client_since"] if client else None,
        }
    )

@app.route("/api/client/<int:biz_id>/update", methods=["POST"])
def api_client_update(biz_id):
    data = request.get_json(force=True, silent=True) or {}
    con = get_db()
    cur = con.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO clients (business_id, client_since, monthly_value, notes)
        VALUES (?, ?, ?, ?)
    """, (biz_id,
          data.get("client_since", datetime.now().strftime("%Y-%m-%d")),
          data.get("monthly_value", 0),
          data.get("notes", "")))
    con.commit()
    con.close()
    return jsonify(ok=True)

@app.route("/api/followups")
def api_followups():
    """Get all leads with follow-ups due today or overdue."""
    today = datetime.now().strftime("%Y-%m-%d")
    con = get_db()
    cur = con.cursor()
    rows = cur.execute("""
        SELECT b.id, b.name, b.website, b.phone,
               c.followup_date, c.outcome, c.notes, c.called_at
        FROM businesses b
        JOIN (
            SELECT calls.business_id, MAX(calls.id) as max_id FROM calls GROUP BY calls.business_id
        ) latest ON latest.business_id = b.id
        JOIN calls c ON c.id = latest.max_id
        WHERE c.followup_date != '' AND c.followup_date IS NOT NULL
        AND c.followup_date <= ?
        ORDER BY c.followup_date ASC
    """, (today,)).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/upcoming_followups")
def api_upcoming_followups():
    """Get all scheduled follow-ups including future ones."""
    con = get_db()
    cur = con.cursor()
    rows = cur.execute("""
        SELECT b.id, b.name, b.website, b.phone,
               c.followup_date, c.outcome, c.notes, c.called_at
        FROM businesses b
        JOIN (
            SELECT business_id, MAX(id) as max_id FROM calls GROUP BY business_id
        ) latest ON latest.business_id = b.id
        JOIN calls c ON c.id = latest.max_id
        WHERE c.followup_date != '' AND c.followup_date IS NOT NULL
        ORDER BY c.followup_date ASC
    """).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/browser_audit/<int:biz_id>", methods=["POST"])
def api_browser_audit(biz_id):
    """Run a full Playwright browser audit on a single site."""
    con = get_db()
    cur = con.cursor()
    biz = cur.execute("SELECT * FROM businesses WHERE id = ?", (biz_id,)).fetchone()
    con.close()
    if not biz:
        return jsonify(error="Not found"), 404

    print(f"[Browser] Starting browser audit for {biz['website']}...")
    result = browser_audit(biz["website"])
    print(f"[Browser] Done — desktop: {result.get('desktop_score')}, mobile: {result.get('mobile_score')}")

    # Store browser metrics in the latest audit record
    con = get_db()
    cur = con.cursor()
    audit = cur.execute(
        "SELECT id, metrics FROM audits WHERE business_id = ? ORDER BY id DESC LIMIT 1",
        (biz_id,)
    ).fetchone()
    if audit:
        existing = json.loads(audit["metrics"]) if audit["metrics"] else {}
        existing.update({
            "browser_lcp_ms":        result.get("lcp_ms"),
            "browser_cls":           result.get("cls_score"),
            "browser_fcp_ms":        result.get("fcp_ms"),
            "browser_ttfb_ms":       result.get("ttfb_ms"),
            "browser_desktop_score": result.get("desktop_score"),
            "browser_mobile_score":  result.get("mobile_score"),
            "browser_js_errors":     result.get("js_errors", []),
            "browser_render_blocking": result.get("render_blocking", []),
            "browser_requests":      result.get("total_requests"),
            "browser_transfer_kb":   result.get("total_transfer_kb"),
        })
        cur.execute("UPDATE audits SET metrics = ? WHERE id = ?",
                    (json.dumps(existing), audit["id"]))
        con.commit()
    con.close()
    return jsonify(result)

@app.route("/api/report/<int:biz_id>")
def api_report(biz_id):
    """Generate a visual report card with letter grades."""
    con = get_db()
    cur = con.cursor()
    biz   = cur.execute("SELECT * FROM businesses WHERE id = ?", (biz_id,)).fetchone()
    audit = cur.execute(
        "SELECT errors, metrics FROM audits WHERE business_id = ? ORDER BY id DESC LIMIT 1",
        (biz_id,)
    ).fetchone()
    con.close()

    if not biz or not audit:
        return jsonify(error="Not found"), 404

    errors  = json.loads(audit["errors"]) if audit["errors"] else []
    metrics = json.loads(audit["metrics"]) if audit["metrics"] else {}
    error_types = {e["type"] for e in errors}

    def grade(issues, total_checks):
        """Convert issue count to letter grade."""
        ratio = issues / max(total_checks, 1)
        if ratio == 0:    return "A"
        if ratio <= 0.15: return "B"
        if ratio <= 0.35: return "C"
        if ratio <= 0.6:  return "D"
        return "F"

    def grade_color(g):
        return {"A":"#187a4c","B":"#2563eb","C":"#e07820","D":"#c0392b","F":"#7a0000"}.get(g,"#888")

    # ── Section scoring ──

    # Security (3 checks)
    sec_issues = sum([
        1 if not metrics.get("https") else 0,
        1 if "ssl" in error_types else 0,
        1 if not metrics.get("robots_txt") else 0,
    ])
    sec_grade = grade(sec_issues, 3)
    sec_items = [
        {"label": "HTTPS / SSL", "pass": metrics.get("https"), "detail": "Site uses secure HTTPS" if metrics.get("https") else "SSL certificate missing or expired"},
        {"label": "robots.txt", "pass": metrics.get("robots_txt"), "detail": "robots.txt present" if metrics.get("robots_txt") else "No robots.txt found"},
        {"label": "No server errors", "pass": "server_error" not in error_types and "not_found" not in error_types, "detail": "Site loads without errors" if "server_error" not in error_types else "Site returning server errors"},
    ]

    # SEO (6 checks)
    seo_issues = sum([
        1 if "seo" in error_types else 0,
        1 if not metrics.get("canonical") else 0,
        1 if not metrics.get("open_graph") else 0,
        1 if not metrics.get("sitemap") else 0,
        1 if (metrics.get("h1_count") or 0) == 0 else 0,
        1 if (metrics.get("h1_count") or 0) > 1 else 0,
    ])
    seo_grade = grade(seo_issues, 6)
    seo_items = [
        {"label": "Page title", "pass": "seo" not in error_types or metrics.get("h1_count") is not None, "detail": "Title tag present"},
        {"label": "Meta description", "pass": "seo" not in error_types, "detail": "Meta description found" if "seo" not in error_types else "Missing meta description"},
        {"label": "H1 heading", "pass": (metrics.get("h1_count") or 0) == 1, "detail": f"{metrics.get('h1_count',0)} H1 tag(s) found"},
        {"label": "Canonical tag", "pass": metrics.get("canonical"), "detail": "Canonical tag present" if metrics.get("canonical") else "No canonical tag"},
        {"label": "Open Graph", "pass": metrics.get("open_graph"), "detail": "Social sharing tags present" if metrics.get("open_graph") else "No Open Graph tags"},
        {"label": "Sitemap.xml", "pass": metrics.get("sitemap"), "detail": "Sitemap found" if metrics.get("sitemap") else "No sitemap.xml"},
    ]

    # Mobile (2 checks)
    mob_issues = sum([
        1 if "mobile" in error_types else 0,
        1 if not metrics.get("https") else 0,
    ])
    mob_grade = grade(mob_issues, 2)
    mob_items = [
        {"label": "Mobile viewport", "pass": "mobile" not in error_types, "detail": "Mobile viewport tag present" if "mobile" not in error_types else "Missing mobile viewport — broken on phones"},
        {"label": "HTTPS on mobile", "pass": metrics.get("https"), "detail": "Secure on mobile" if metrics.get("https") else "Not secure on mobile"},
    ]

    # Performance (6 checks — includes browser metrics if available)
    load_ms   = metrics.get("load_time_ms") or 0
    page_kb   = metrics.get("page_size_kb") or 0
    lcp_ms    = metrics.get("browser_lcp_ms")
    cls_score = metrics.get("browser_cls")
    fcp_ms    = metrics.get("browser_fcp_ms")
    ttfb_ms   = metrics.get("browser_ttfb_ms")
    d_score   = metrics.get("browser_desktop_score")
    m_score   = metrics.get("browser_mobile_score")
    has_browser = d_score is not None

    perf_issues = sum([
        1 if "timeout" in error_types or "timed_out" in error_types else 0,
        1 if load_ms > 3000 else 0,
        1 if page_kb > 3000 else 0,
        1 if lcp_ms and lcp_ms > 2500 else 0,
        1 if cls_score and cls_score > 0.1 else 0,
    ])
    perf_grade = grade(perf_issues, 5 if has_browser else 3)
    perf_items = [
        {"label": "Page load time",    "pass": load_ms < 3000 and load_ms > 0,   "detail": f"{load_ms}ms" if load_ms else "Not measured"},
        {"label": "Page size",         "pass": page_kb < 3000 and page_kb > 0,   "detail": f"{page_kb}KB" if page_kb else "Not measured"},
        {"label": "No timeout",        "pass": "timeout" not in error_types,      "detail": "Site responds quickly" if "timeout" not in error_types else "Site timed out"},
    ]
    if has_browser:
        perf_items += [
            {"label": f"Desktop score ({d_score}/100)", "pass": d_score >= 70,   "detail": f"LCP: {lcp_ms}ms | FCP: {fcp_ms}ms | TTFB: {ttfb_ms}ms"},
            {"label": f"Mobile score ({m_score}/100)",  "pass": m_score >= 50,   "detail": "Good mobile performance" if m_score >= 50 else "Poor mobile performance"},
            {"label": "Layout stability (CLS)",         "pass": (cls_score or 0) < 0.1, "detail": f"CLS score: {cls_score}" if cls_score is not None else "Not measured"},
        ]
        js_errs = metrics.get("browser_js_errors", [])
        blocking = metrics.get("browser_render_blocking", [])
        if js_errs:
            perf_items.append({"label": "JavaScript errors", "pass": False, "detail": f"{len(js_errs)} JS error(s) found"})
        if blocking:
            perf_items.append({"label": "Render blocking", "pass": False, "detail": f"Blocking: {', '.join(blocking[:3])}"})
    else:
        perf_items.append({"label": "Core Web Vitals", "pass": None, "detail": "Click 'Browser Audit' for LCP, CLS, FCP scores"})

    # Accessibility (3 checks)
    imgs_no_alt = metrics.get("images_no_alt") or 0
    img_count   = metrics.get("image_count") or 0
    acc_issues  = sum([
        1 if imgs_no_alt > 0 else 0,
        1 if "accessibility" in error_types else 0,
        1 if not metrics.get("favicon") else 0,
    ])
    acc_grade = grade(acc_issues, 3)
    acc_items = [
        {"label": "Image alt text", "pass": imgs_no_alt == 0, "detail": f"All {img_count} images have alt text" if imgs_no_alt == 0 else f"{imgs_no_alt}/{img_count} images missing alt text"},
        {"label": "Favicon", "pass": metrics.get("favicon"), "detail": "Favicon present" if metrics.get("favicon") else "No favicon — blank browser tab icon"},
        {"label": "Heading structure", "pass": (metrics.get("h1_count") or 0) == 1 and (metrics.get("h2_count") or 0) > 0, "detail": f"H1: {metrics.get('h1_count',0)}, H2: {metrics.get('h2_count',0)}"},
    ]

    # UX (3 checks)
    ux_issues = sum([
        1 if "ux" in error_types else 0,
        1 if "broken_links" in error_types else 0,
        1 if not metrics.get("social_links") else 0,
    ])
    ux_grade = grade(ux_issues, 3)
    ux_items = [
        {"label": "Contact form", "pass": "ux" not in error_types, "detail": "Contact form found" if "ux" not in error_types else "No contact form detected"},
        {"label": "No broken links", "pass": "broken_links" not in error_types, "detail": "No broken links found" if "broken_links" not in error_types else "Broken links detected"},
        {"label": "Social media", "pass": bool(metrics.get("social_links")), "detail": f"Found: {', '.join(metrics.get('social_links',[]))}" if metrics.get("social_links") else "No social media links found"},
    ]

    # Overall grade
    grade_vals = {"A":4,"B":3,"C":2,"D":1,"F":0}
    grades = [sec_grade, seo_grade, mob_grade, perf_grade, acc_grade, ux_grade]
    avg = sum(grade_vals[g] for g in grades) / len(grades)
    overall = ["F","D","C","B","A"][min(4, round(avg))]

    return jsonify(
        name=biz["name"],
        website=biz["website"],
        overall=overall,
        overall_color=grade_color(overall),
        sections=[
            {"title":"Security",      "grade":sec_grade,  "color":grade_color(sec_grade),  "items":sec_items,  "icon":"🔒"},
            {"title":"SEO",           "grade":seo_grade,  "color":grade_color(seo_grade),  "items":seo_items,  "icon":"🔍"},
            {"title":"Mobile",        "grade":mob_grade,  "color":grade_color(mob_grade),  "items":mob_items,  "icon":"📱"},
            {"title":"Performance",   "grade":perf_grade, "color":grade_color(perf_grade), "items":perf_items, "icon":"⚡"},
            {"title":"Accessibility", "grade":acc_grade,  "color":grade_color(acc_grade),  "items":acc_items,  "icon":"♿"},
            {"title":"User Experience","grade":ux_grade,  "color":grade_color(ux_grade),   "items":ux_items,   "icon":"✨"},
        ]
    )

@app.route("/api/email/<int:biz_id>")
def api_email(biz_id):
    """Generate a follow-up email based on the business's errors."""
    con = get_db()
    cur = con.cursor()
    biz = cur.execute("SELECT * FROM businesses WHERE id = ?", (biz_id,)).fetchone()
    audit = cur.execute("""
        SELECT errors FROM audits WHERE business_id = ? ORDER BY id DESC LIMIT 1
    """, (biz_id,)).fetchone()
    call = cur.execute("""
        SELECT * FROM calls WHERE business_id = ? ORDER BY id DESC LIMIT 1
    """, (biz_id,)).fetchone()
    con.close()

    if not biz:
        return jsonify(error="Not found"), 404

    errors = json.loads(audit["errors"]) if audit else []
    name = biz["name"]
    website = biz["website"].replace("https://","").replace("http://","").rstrip("/")
    called_today = call and call["called_at"][:10] == datetime.now().strftime("%Y-%m-%d")

    # Build error bullet points
    bullets = []
    for e in errors:
        if e["type"] == "ssl":
            bullets.append("• Your site is showing a 'Not Secure' warning in browsers due to an expired SSL certificate")
        elif e["type"] in ["not_found","server_error","down"]:
            bullets.append("• Your homepage is returning an error page instead of your website")
        elif e["type"] == "mobile":
            bullets.append("• Your site isn't optimised for mobile devices — over 60% of visitors browse on phones")
        elif e["type"] == "seo":
            bullets.append("• Missing SEO tags are reducing your visibility in Google search results")
        elif e["type"] == "accessibility":
            bullets.append("• Images are missing descriptions, affecting both SEO and accessibility")
        elif e["type"] == "broken_links":
            bullets.append("• Several internal links are broken and leading to error pages")
        elif e["type"] == "outdated":
            bullets.append("• Your site's copyright date appears outdated, which can concern visitors")
        elif e["type"] == "ux":
            bullets.append("• No contact form found — visitors have no easy way to reach you online")
        elif e["type"] in ["timeout","timed_out"]:
            bullets.append("• Your site is loading very slowly, which causes visitors to leave and hurts Google ranking")

    bullets_text = "\n".join(bullets) if bullets else "• General improvements to improve visitor experience and search ranking"

    opening = "Following up on our conversation today" if called_today else "Following up on our recent conversation"

    email = f"""SUBJECT: Quick follow-up about {name}'s website

---

Hi [NAME],

{opening} — as promised, here's a quick summary of what I found on your website at {website}.

Here are the specific issues I noticed:

{bullets_text}

These are all fixable, and addressing them could make a real difference to how many customers find and trust your business online.

I'd love to put together a quick proposal for you — no obligation, just a clear breakdown of what's involved and what it would cost.

Would you be open to a 15-minute call this week so I can walk you through it?

You can reach me at:
[YOUR NAME]
[YOUR PHONE]
[YOUR EMAIL]
[YOUR COMPANY]

Looking forward to hearing from you.

---
Note: If you'd prefer not to be contacted, just reply to let me know and I won't reach out again.
"""
    return jsonify(email=email, subject=f"Quick follow-up about {name}'s website", name=name)

# ─────────────────────────────────────────────
# CLIENT DASHBOARD
# ─────────────────────────────────────────────

CLIENT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Client Dashboard — WebAudit</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f7f7f5;color:#1a1a18;font-size:13px;}
.topbar{background:#fff;border-bottom:.5px solid #e2e2dc;padding:0 1.5rem;display:flex;align-items:center;height:52px;gap:12px;position:sticky;top:0;z-index:10;}
.back-btn{padding:5px 12px;border:.5px solid #ddd;border-radius:6px;background:#f7f7f5;font-size:12px;cursor:pointer;text-decoration:none;color:#333;}
.client-name{font-size:15px;font-weight:600;}
.client-url{font-size:11px;color:#999;}
.time-filters{margin-left:auto;display:flex;gap:4px;}
.tf-btn{padding:5px 12px;border:.5px solid #ddd;border-radius:6px;background:#f7f7f5;font-size:11px;cursor:pointer;color:#555;}
.tf-btn.active{background:#2563eb;color:#fff;border-color:#2563eb;}
.main{padding:1.25rem 1.5rem;}
.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:1.25rem;}
.metric{background:#fff;border:.5px solid #e2e2dc;border-radius:10px;padding:1rem;}
.metric-label{font-size:10px;color:#999;text-transform:uppercase;letter-spacing:.05em;margin-bottom:5px;}
.metric-value{font-size:28px;font-weight:600;letter-spacing:-.5px;}
.metric-sub{font-size:10px;color:#bbb;margin-top:2px;}
.metric-value.green{color:#187a4c;}.metric-value.blue{color:#2563eb;}.metric-value.amber{color:#e07820;}.metric-value.purple{color:#6d28d9;}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;}
.card{background:#fff;border:.5px solid #e2e2dc;border-radius:10px;padding:1rem 1.1rem;}
.card-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;}
.card-title{font-size:13px;font-weight:600;}
.btn-small{padding:5px 10px;border-radius:6px;border:.5px solid #ddd;background:#f7f7f5;font-size:11px;cursor:pointer;font-family:inherit;}
.btn-primary{background:#2563eb;color:#fff;border-color:#2563eb;}
.task-row{display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:.5px solid #f2f2ee;}
.task-row:last-child{border-bottom:none;}
.task-check{width:16px;height:16px;cursor:pointer;flex-shrink:0;}
.task-text{flex:1;font-size:12px;}
.task-text.done{text-decoration:line-through;color:#bbb;}
.task-date{font-size:10px;color:#bbb;white-space:nowrap;}
.task-del{font-size:11px;color:#ddd;cursor:pointer;border:none;background:none;padding:0 4px;}
.task-del:hover{color:#c0392b;}
.imp-row{padding:8px 0;border-bottom:.5px solid #f2f2ee;}
.imp-row:last-child{border-bottom:none;}
.imp-title{font-size:12px;font-weight:500;}
.imp-scores{font-size:10px;color:#888;margin-top:2px;}
.imp-date{font-size:10px;color:#bbb;}
.score-arrow{color:#187a4c;font-weight:600;}
.audit-chart{display:flex;align-items:flex-end;gap:3px;height:60px;margin-top:8px;}
.chart-bar{flex:1;background:#2563eb;border-radius:3px 3px 0 0;min-height:4px;cursor:pointer;position:relative;}
.chart-bar:hover::after{content:attr(data-tip);position:absolute;bottom:105%;left:50%;transform:translateX(-50%);background:#333;color:#fff;font-size:9px;padding:2px 6px;border-radius:4px;white-space:nowrap;}
.add-form{display:none;margin-top:10px;padding:10px;background:#f7f7f5;border-radius:8px;border:.5px solid #e2e2dc;}
.add-form.show{display:block;}
.add-form input,.add-form textarea,.add-form select{width:100%;padding:6px 8px;border:.5px solid #ddd;border-radius:5px;font-size:11px;font-family:inherit;margin-bottom:6px;}
.add-form textarea{height:50px;resize:vertical;}
.form-row{display:flex;gap:6px;}
.form-row input{flex:1;}
.client-info-row{display:flex;gap:10px;align-items:center;margin-bottom:12px;padding:10px;background:#f7f7f5;border-radius:8px;}
.ci-label{font-size:10px;color:#888;}
.ci-value{font-size:13px;font-weight:500;}
.ci-edit{font-size:10px;color:#2563eb;cursor:pointer;margin-left:auto;}
.empty-state{color:#bbb;font-size:12px;text-align:center;padding:1.5rem 0;}
</style>
</head>
<body>

<div class="topbar">
  <a href="/" class="back-btn">← Back</a>
  <div>
    <div class="client-name" id="client-name">Loading...</div>
    <div class="client-url" id="client-url"></div>
  </div>
  <div class="time-filters">
    <button class="tf-btn active" onclick="setFilter('all',this)">All time</button>
    <button class="tf-btn" onclick="setFilter('month',this)">This month</button>
    <button class="tf-btn" onclick="setFilter('week',this)">This week</button>
    <button id="reaudit-btn" onclick="reauditClient()" style="margin-left:12px;padding:5px 12px;border-radius:6px;border:.5px solid #2563eb;background:#eff6ff;color:#1d4ed8;font-size:11px;cursor:pointer;font-family:inherit;font-weight:500;">🔄 Re-audit site</button>
  </div>
</div>

<div class="main">

  <!-- Client info bar -->
  <div class="client-info-row">
    <div>
      <div class="ci-label">Client since</div>
      <div class="ci-value" id="ci-since">—</div>
    </div>
    <div style="margin-left:20px;">
      <div class="ci-label">Monthly value</div>
      <div class="ci-value" id="ci-value">—</div>
    </div>
    <button class="btn-small ci-edit" onclick="toggleClientEdit()">Edit</button>
  </div>
  <div class="add-form" id="client-edit-form">
    <div class="form-row">
      <div style="flex:1"><div style="font-size:10px;color:#888;margin-bottom:3px;">Client since</div><input type="date" id="edit-since"></div>
      <div style="flex:1"><div style="font-size:10px;color:#888;margin-bottom:3px;">Monthly value ($)</div><input type="number" id="edit-value" placeholder="0"></div>
    </div>
    <button class="btn-small btn-primary" onclick="saveClientInfo()">Save</button>
    <button class="btn-small" onclick="toggleClientEdit()" style="margin-left:5px;">Cancel</button>
  </div>

  <!-- Metrics -->
  <div class="metrics">
    <div class="metric">
      <div class="metric-label">Tasks completed</div>
      <div class="metric-value green" id="m-tasks">0/0</div>
      <div class="metric-sub">To-do progress</div>
    </div>
    <div class="metric">
      <div class="metric-label">Errors fixed</div>
      <div class="metric-value blue" id="m-fixed">0</div>
      <div class="metric-sub">Improvements logged</div>
    </div>
    <div class="metric">
      <div class="metric-label">Score improvement</div>
      <div class="metric-value amber" id="m-score">—</div>
      <div class="metric-sub">Latest vs first audit</div>
    </div>
    <div class="metric">
      <div class="metric-label">Days as client</div>
      <div class="metric-value purple" id="m-days">—</div>
      <div class="metric-sub" id="m-since-label">Client since —</div>
    </div>
  </div>

  <div class="two-col">

    <!-- To-do list -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">📋 To-do list</div>
        <button class="btn-small" onclick="toggleForm('task-form')">+ Add task</button>
      </div>
      <div class="add-form" id="task-form">
        <input type="text" id="task-title" placeholder="Task title">
        <div class="form-row">
          <input type="date" id="task-due" title="Due date">
        </div>
        <button class="btn-small btn-primary" onclick="addTask()">Add task</button>
        <button class="btn-small" onclick="toggleForm('task-form')" style="margin-left:5px;">Cancel</button>
      </div>
      <div id="tasks-list"><div class="empty-state">No tasks yet — add one above</div></div>
    </div>

    <!-- Improvement log -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">✅ Improvements logged</div>
        <button class="btn-small" onclick="toggleForm('imp-form')">+ Log fix</button>
      </div>
      <div class="add-form" id="imp-form">
        <input type="text" id="imp-title" placeholder="What was fixed?">
        <textarea id="imp-desc" placeholder="Details (optional)"></textarea>
        <div class="form-row">
          <div style="flex:1"><div style="font-size:10px;color:#888;margin-bottom:3px;">Before score</div><input type="number" id="imp-before" placeholder="e.g. 45" min="0" max="100"></div>
          <div style="flex:1"><div style="font-size:10px;color:#888;margin-bottom:3px;">After score</div><input type="number" id="imp-after" placeholder="e.g. 78" min="0" max="100"></div>
        </div>
        <button class="btn-small btn-primary" onclick="addImprovement()">Log improvement</button>
        <button class="btn-small" onclick="toggleForm('imp-form')" style="margin-left:5px;">Cancel</button>
      </div>
      <div id="imp-list"><div class="empty-state">No improvements logged yet</div></div>
    </div>
  </div>

  <!-- Audit history chart -->
  <div class="card">
    <div class="card-header">
      <div class="card-title">📈 Audit history — errors over time</div>
    </div>
    <div id="audit-chart" class="audit-chart"><div class="empty-state" style="width:100%;height:auto;">No audit history yet</div></div>
  </div>

</div>

<script>
const BIZ_ID = __BIZ_ID__;
var _filter = 'all';
var _clientData = {};

function setFilter(f, btn) {
  _filter = f;
  document.querySelectorAll('.tf-btn').forEach(function(b){ b.classList.remove('active'); });
  btn.classList.add('active');
  renderAll();
}

function filterByDate(items, dateField) {
  if (_filter === 'all') return items;
  const now = new Date();
  const cutoff = new Date();
  if (_filter === 'week') cutoff.setDate(now.getDate() - 7);
  if (_filter === 'month') cutoff.setMonth(now.getMonth() - 1);
  return items.filter(function(item) {
    return item[dateField] && new Date(item[dateField]) >= cutoff;
  });
}

async function loadData() {
  const r = await fetch('/api/client/' + BIZ_ID).then(r=>r.json());
  _clientData = r;
  document.getElementById('client-name').textContent = r.business.name || 'Client';
  document.getElementById('client-url').textContent = (r.business.website||'').replace(/https?:\/\//, '');

  const c = r.client || {};
  document.getElementById('ci-since').textContent = c.client_since || '—';
  document.getElementById('ci-value').textContent = c.monthly_value ? '$' + c.monthly_value + '/mo' : '—';
  document.getElementById('edit-since').value = c.client_since || '';
  document.getElementById('edit-value').value = c.monthly_value || '';

  renderAll();
}

function renderAll() {
  const d = _clientData;
  const tasks = filterByDate(d.tasks || [], 'created_at');
  const imps  = filterByDate(d.improvements || [], 'logged_at');

  // Metrics
  const done = tasks.filter(function(t){ return t.completed; }).length;
  document.getElementById('m-tasks').textContent = done + '/' + tasks.length;
  document.getElementById('m-fixed').textContent = imps.length;

  // Score improvement from improvements
  const withScores = imps.filter(function(i){ return i.before_score && i.after_score; });
  if (withScores.length) {
    const first = withScores[withScores.length-1];
    const last  = withScores[0];
    const diff  = (last.after_score || 0) - (first.before_score || 0);
    document.getElementById('m-score').textContent = (diff >= 0 ? '+' : '') + diff + ' pts';
  } else {
    document.getElementById('m-score').textContent = '—';
  }

  // Days as client
  const c = d.client || {};
  if (c.client_since) {
    const days = Math.floor((new Date() - new Date(c.client_since)) / 86400000);
    document.getElementById('m-days').textContent = days;
    document.getElementById('m-since-label').textContent = 'Since ' + c.client_since;
  }

  // Tasks list
  const taskEl = document.getElementById('tasks-list');
  if (!tasks.length) {
    taskEl.innerHTML = '<div class="empty-state">No tasks' + (_filter !== 'all' ? ' in this period' : ' yet — add one above') + '</div>';
  } else {
    taskEl.innerHTML = tasks.map(function(t) {
      return '<div class="task-row">'
        + '<input type="checkbox" class="task-check" ' + (t.completed ? 'checked' : '') + ' onchange="toggleTask(' + t.id + ',this.checked)">'
        + '<span class="task-text' + (t.completed ? ' done' : '') + '">' + t.title + '</span>'
        + (t.due_date ? '<span class="task-date">' + t.due_date + '</span>' : '')
        + '<button class="task-del" onclick="deleteTask(' + t.id + ')">✕</button>'
        + '</div>';
    }).join('');
  }

  // Improvements list
  const impEl = document.getElementById('imp-list');
  if (!imps.length) {
    impEl.innerHTML = '<div class="empty-state">No improvements' + (_filter !== 'all' ? ' in this period' : ' logged yet') + '</div>';
  } else {
    impEl.innerHTML = imps.map(function(i) {
      const scoreHtml = (i.before_score && i.after_score)
        ? '<span class="score-arrow">' + i.before_score + ' → ' + i.after_score + ' (' + (i.after_score - i.before_score >= 0 ? '+' : '') + (i.after_score - i.before_score) + ')</span>'
        : '';
      return '<div class="imp-row">'
        + '<div style="display:flex;justify-content:space-between;">'
        + '<div class="imp-title">' + i.title + '</div>'
        + '<div class="imp-date">' + (i.logged_at||'').slice(0,10) + '</div>'
        + '</div>'
        + (i.description ? '<div style="font-size:10px;color:#888;margin-top:2px;">' + i.description + '</div>' : '')
        + (scoreHtml ? '<div class="imp-scores">' + scoreHtml + '</div>' : '')
        + '</div>';
    }).join('');
  }

  // Audit chart
  const history = d.audit_history || [];
  const chartEl = document.getElementById('audit-chart');
  if (!history.length) {
    chartEl.innerHTML = '<div class="empty-state" style="width:100%;height:auto;">No audit history</div>';
  } else {
    const max = Math.max(...history.map(function(h){ return h.error_count || 0; }), 1);
    chartEl.innerHTML = history.map(function(h) {
      const pct = Math.max(4, Math.round(((h.error_count || 0) / max) * 100));
      const date = (h.audited_at || '').slice(0,10);
      const clr = h.error_count === 0 ? '#187a4c' : h.error_count <= 2 ? '#e07820' : '#c0392b';
      return '<div class="chart-bar" style="height:' + pct + '%;background:' + clr + ';" data-tip="' + date + ': ' + h.error_count + ' errors"></div>';
    }).join('');
  }
}

function toggleForm(id) {
  const el = document.getElementById(id);
  el.classList.toggle('show');
}

function toggleClientEdit() {
  toggleForm('client-edit-form');
}

async function saveClientInfo() {
  const since = document.getElementById('edit-since').value;
  const val   = document.getElementById('edit-value').value;
  await fetch('/api/client/' + BIZ_ID + '/update', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({client_since: since, monthly_value: parseFloat(val)||0})
  });
  toggleForm('client-edit-form');
  await loadData();
}

async function addTask() {
  const title = document.getElementById('task-title').value.trim();
  if (!title) return;
  await fetch('/api/tasks', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({business_id: BIZ_ID, title, due_date: document.getElementById('task-due').value})
  });
  document.getElementById('task-title').value = '';
  document.getElementById('task-due').value = '';
  toggleForm('task-form');
  await loadData();
}

async function toggleTask(id, done) {
  await fetch('/api/tasks/' + id + '/complete', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({completed: done})
  });
  await loadData();
}

async function deleteTask(id) {
  await fetch('/api/tasks/' + id, {method:'DELETE'});
  await loadData();
}

async function addImprovement() {
  const title = document.getElementById('imp-title').value.trim();
  if (!title) return;
  await fetch('/api/improvements', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      business_id: BIZ_ID,
      title,
      description: document.getElementById('imp-desc').value,
      before_score: parseInt(document.getElementById('imp-before').value)||null,
      after_score:  parseInt(document.getElementById('imp-after').value)||null,
    })
  });
  document.getElementById('imp-title').value = '';
  document.getElementById('imp-desc').value = '';
  document.getElementById('imp-before').value = '';
  document.getElementById('imp-after').value = '';
  toggleForm('imp-form');
  await loadData();
}

loadData();
</script>
</body>
</html>"""

@app.route("/client/<int:biz_id>")
def client_dashboard(biz_id):
    return Response(CLIENT_HTML.replace("__BIZ_ID__", str(biz_id)), mimetype="text/html")

# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print(" WebAudit — Ethical Lead Generation Pipeline")
    print("=" * 55)
    print(f" Bot identity : {USER_AGENT}")
    print(f" Database     : {DB_PATH}")
    print("=" * 55 + "\n")

    init_db()
    run_migrations()

    # Run canary check in background
    import threading
    threading.Thread(target=run_canary_check, daemon=True).start()

    # Try to launch in a native desktop window (pywebview)
    try:
        import webview

        def start_flask():
            app.run(debug=False, port=5000, use_reloader=False)

        # Start Flask in background thread
        flask_thread = threading.Thread(target=start_flask, daemon=True)
        flask_thread.start()

        import time
        time.sleep(1)  # Give Flask a moment to start

        print(" Opening WebAudit in desktop window...")
        webview.create_window(
            "WebAudit",
            "http://localhost:5000",
            width=1280,
            height=820,
            min_size=(900, 600),
            resizable=True,
        )
        webview.start()

    except ImportError:
        # Fallback: run normally and open browser
        print(" pywebview not found — opening in browser instead")
        print(" Dashboard : http://localhost:5000")
        import webbrowser, time
        threading.Timer(1.5, lambda: webbrowser.open("http://localhost:5000")).start()
        app.run(debug=False, port=5000)
