#!/usr/bin/env python3
"""
3D Print Media Monitor — Daily RSS Crawler

Fetches articles from target media, filters by brand keywords,
classifies into 3 categories, and outputs data.json for the landing page.

Usage:
    pip install feedparser requests
    python crawler.py              # fetch last 1 day
    python crawler.py --days 3     # fetch last 3 days (useful for first run)
"""

import sys
import re
import json
import hashlib
import argparse
import feedparser
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path


# ─── Brand keywords ───────────────────────────────────────────────────────────
# Each brand maps to a list of lowercase search terms.
# "Longer" is too generic, so we only match compound phrases.

BRAND_KEYWORDS: dict[str, list[str]] = {
    "eufyMake":   ["eufymake", "eufy make", "eufy e1", "eufy e2", "eufy maker"],
    "Bambu Lab":  ["bambu lab", "bambulab", "bambu x1", "bambu a1", "bambu p1",
                   "bambu ams", "bambu studio", "bambu a2"],
    "xTool":      ["xtool", "x tool s2", "x tool p2", "x tool f1"],
    "Procolored": ["procolored", "procoloured"],
    "Anycubic":   ["anycubic", "photon mono", "photon m", "kobra 2", "kobra 3"],
    "Formlabs":   ["formlabs", "form 3", "form 4", "form auto", "form wash"],
    "Longer":     ["longer 3d", "longer orange", "longer cube", "longer lk5",
                   "longer lk4", "longer rap"],
    "Elegoo":     ["elegoo", "saturn 4", "saturn 3", "neptune 4", "neptune 5"],
    "Creality":   ["creality", "ender-3", "ender 3", "k1 max", "k2 plus",
                   "k3 max", "k3 ultra", "creality os"],
    "Cricut":     ["cricut maker", "cricut explore", "cricut joy", "cricut venture",
                   "cricut design space"],
}

# ─── Media sources ────────────────────────────────────────────────────────────
# UVPM values are static estimates used for sorting only.

MEDIA_SOURCES: list[dict] = [
    # ── Tier 1: Consumer tech ─────────────────────────────────────
    {"name": "The Verge",       "rss": "https://www.theverge.com/rss/index.xml",         "uvpm": 48_000_000, "tier": 1},
    {"name": "CNET",            "rss": "https://www.cnet.com/rss/all/",                  "uvpm": 38_000_000, "tier": 1},
    {"name": "Engadget",        "rss": "https://www.engadget.com/rss.xml",               "uvpm": 22_000_000, "tier": 1},
    {"name": "TechCrunch",      "rss": "https://techcrunch.com/feed/",                   "uvpm": 20_000_000, "tier": 1},
    {"name": "Wired",           "rss": "https://www.wired.com/feed/rss",                 "uvpm": 18_000_000, "tier": 1},
    {"name": "Tom's Guide",     "rss": "https://www.tomsguide.com/feeds/all",            "uvpm": 15_000_000, "tier": 1},
    {"name": "ZDNet",           "rss": "https://www.zdnet.com/news/rss.xml",             "uvpm": 14_000_000, "tier": 1},
    {"name": "Digital Trends",  "rss": "https://www.digitaltrends.com/feed/",            "uvpm": 13_000_000, "tier": 1},
    {"name": "PCMag",           "rss": "https://www.pcmag.com/feeds/all",                "uvpm": 12_000_000, "tier": 1},
    {"name": "Mashable",        "rss": "https://mashable.com/feeds/rss/all",             "uvpm": 10_000_000, "tier": 1},
    {"name": "TechRadar",       "rss": "https://www.techradar.com/rss",                  "uvpm": 10_000_000, "tier": 1},
    {"name": "Gizmodo",         "rss": "https://gizmodo.com/rss",                        "uvpm": 8_000_000,  "tier": 1},
    {"name": "SlashGear",       "rss": "https://www.slashgear.com/feed/",                "uvpm": 3_000_000,  "tier": 1},
    # ── Tier 2: 3D Print / Maker ──────────────────────────────────
    {"name": "3DPrint.com",         "rss": "https://3dprint.com/feed/",                  "uvpm": 2_100_000,  "tier": 2},
    {"name": "All3DP",              "rss": "https://all3dp.com/feed/",                   "uvpm": 1_800_000,  "tier": 2},
    {"name": "Hackaday",            "rss": "https://hackaday.com/blog/feed/",            "uvpm": 1_500_000,  "tier": 2},
    {"name": "Make: Magazine",      "rss": "https://makezine.com/feed/",                 "uvpm": 1_200_000,  "tier": 2},
    {"name": "3D Printing Industry","rss": "https://3dprintingindustry.com/feed/",       "uvpm": 850_000,    "tier": 2},
    {"name": "Hackster.io",         "rss": "https://www.hackster.io/news.atom",          "uvpm": 800_000,    "tier": 2},
    {"name": "Fabbaloo",            "rss": "https://fabbaloo.com/feed/",                 "uvpm": 420_000,    "tier": 2},
]

# ─── Classification rules ─────────────────────────────────────────────────────
# Evaluated against lowercase(title + " " + summary).
# Priority order: hands_on_review > product_news > brand_news (fallback)

CATEGORY_RULES: dict[str, list[str]] = {
    "hands_on_review": [
        r"\breview\b",
        r"\btested?\b",
        r"\bhands[- ]on\b",
        r"\bbenchmark(ed|s|ing)?\b",
        r"\bwe tried\b",
        r"\blong[- ]term\b",
        r"\bfirst look\b",
        r"\bfirst impression\b",
        r"\bunboxing\b",
        r"\bafter \d+ (month|week|year)",
        r"\beditors?[' ]choice\b",
        r"\bbest buy\b",
        r"\bour verdict\b",
        r"\bpros and cons\b",
        r"\brating[s]?\b",
        r"\b\d+[./]\d+\s*(star|out of|\/10)\b",
    ],
    "product_news": [
        r"\blaunch(es|ed|ing)?\b",
        r"\bunveil(s|ed|ing)?\b",
        r"\bannounce[sd]?\b",
        r"\bnew \w+ (printer|cutter|engraver|scanner|model|version|series)\b",
        r"\bpre[- ]?order(s|ed|ing)?\b",
        r"\bnow available\b",
        r"\bnow on sale\b",
        r"\bships?\b",
        r"\brelease[sd]?\b",
        r"\bpric(e|ed|ing)\b",
        r"\bspecification[s]?\b",
        r"\bfirmware update\b",
        r"\bsoftware update\b",
        r"\bnew feature[s]?\b",
        r"\bupgrade[sd]?\b",
    ],
    "brand_news": [
        r"\braise[sd]?\b",
        r"\bfunding\b",
        r"\bseries [a-e] round\b",
        r"\bacquisition\b",
        r"\bacquire[sd]?\b",
        r"\bpartnership\b",
        r"\blawsuit\b",
        r"\b(ceo|cto|coo|cfo)\b",
        r"\bexpan(ds?|sion|ded)\b",
        r"\bopen[- ]source[sd]?\b",
        r"\bipo\b",
        r"\brevenue\b",
        r"\bhires?\b",
        r"\bfactory\b",
        r"\btariff\b",
        r"\bban\b",
        r"\blegal\b",
    ],
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def fmt_uvpm(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def article_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:10]


def find_brands(text: str) -> list[str]:
    lower = text.lower()
    return [brand for brand, kws in BRAND_KEYWORDS.items()
            if any(kw in lower for kw in kws)]


def classify(title: str, summary: str) -> str:
    combined = f"{title} {summary}".lower()
    for cat in ["hands_on_review", "product_news", "brand_news"]:
        if any(re.search(p, combined) for p in CATEGORY_RULES[cat]):
            return cat
    return "brand_news"  # fallback


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def parse_entry_date(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def fetch_feed(url: str, timeout: int = 15) -> feedparser.FeedParserDict:
    headers = {"User-Agent": "MediaMonitor/1.0 (RSS reader; contact: your@email.com)"}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return feedparser.parse(resp.text)


# ─── Main crawler ─────────────────────────────────────────────────────────────

def crawl(lookback_days: int = 1) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    articles: list[dict] = []
    seen_urls: set[str] = set()
    media_hit: set[str] = set()

    for source in MEDIA_SOURCES:
        print(f"  [{source['tier']}] Fetching {source['name']}...", end=" ", flush=True)
        try:
            feed = fetch_feed(source["rss"])
            matched = 0
            for entry in feed.entries:
                title   = getattr(entry, "title",   "") or ""
                link    = getattr(entry, "link",    "") or ""
                summary = strip_html(getattr(entry, "summary", "") or "")

                # Skip duplicates
                if link in seen_urls:
                    continue

                # Date filter
                pub_dt = parse_entry_date(entry)
                if pub_dt and pub_dt < cutoff:
                    continue

                # Brand filter
                brands = find_brands(f"{title} {summary}")
                if not brands:
                    continue

                seen_urls.add(link)
                media_hit.add(source["name"])
                matched += 1

                articles.append({
                    "id":           article_id(link),
                    "title":        title,
                    "url":          link,
                    "media":        source["name"],
                    "uvpm":         source["uvpm"],
                    "uvpm_display": fmt_uvpm(source["uvpm"]),
                    "tier":         source["tier"],
                    "category":     classify(title, summary),
                    "brands":       brands,
                    "published":    pub_dt.isoformat() if pub_dt else None,
                    "summary":      summary[:400],
                })
            print(f"{matched} hit(s)")
        except Exception as e:
            print(f"ERROR — {e}")

    # Sort by UVPM descending
    articles.sort(key=lambda a: a["uvpm"], reverse=True)

    # Brand counts
    brand_counts = {b: 0 for b in BRAND_KEYWORDS}
    for a in articles:
        for b in a["brands"]:
            if b in brand_counts:
                brand_counts[b] += 1

    # Total reach = sum of UVPM for each media that had ≥1 hit
    total_reach = sum(s["uvpm"] for s in MEDIA_SOURCES if s["name"] in media_hit)
    brands_hit  = sum(1 for v in brand_counts.values() if v > 0)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "date":       today,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "stats": {
            "total":               len(articles),
            "total_reach_display": fmt_uvpm(total_reach),
            "brands_hit":          brands_hit,
            "brands_total":        len(BRAND_KEYWORDS),
            "media_count":         len(media_hit),
        },
        "brand_counts": brand_counts,
        "articles":     articles,
    }


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="3D Print Media Monitor crawler")
    parser.add_argument("--days", type=int, default=1,
                        help="How many days back to fetch (default: 1)")
    args = parser.parse_args()

    print(f"\n3D Print Media Monitor — crawling last {args.days} day(s)\n{'─'*50}")
    data = crawl(lookback_days=args.days)

    out_path = Path(__file__).parent / "data.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n{'─'*50}")
    print(f"Articles : {data['stats']['total']}")
    print(f"Brands   : {data['stats']['brands_hit']}/{data['stats']['brands_total']} hit")
    print(f"Media    : {data['stats']['media_count']} outlets")
    print(f"Output   : {out_path}")
    for brand, count in data["brand_counts"].items():
        if count:
            print(f"  {brand}: {count}")
