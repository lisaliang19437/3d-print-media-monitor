#!/usr/bin/env python3
"""
3D Print Media Monitor — Daily Crawler (Google News RSS edition)

Instead of crawling each media outlet's RSS and hoping for brand mentions,
we query Google News RSS per brand keyword. This is far more reliable:
  - Google indexes all target media outlets
  - Searches return only relevant articles
  - Works consistently from GitHub Actions

Usage:
    pip install feedparser requests
    python crawler.py              # fetch last 1 day
    python crawler.py --days 3     # fetch last 3 days (useful for first run)
"""

import re
import json
import hashlib
import argparse
import feedparser
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote_plus


# ─── Brand search queries ─────────────────────────────────────────────────────
# Each brand maps to a Google News search query.
# Use quotes for exact phrases; OR for alternatives.

BRAND_QUERIES: dict[str, str] = {
    "eufyMake":   '"eufyMake" OR "eufy Make" OR "eufy E1" OR "eufy E2" OR "eufy maker"',
    "Bambu Lab":  '"Bambu Lab" 3D printer',
    "xTool":      'xTool laser OR xTool S2 OR xTool P2 OR xTool F1 OR xTool engraver',
    "Procolored": 'Procolored printer OR Procolored DTF',
    "Anycubic":   'Anycubic 3D printer OR Anycubic resin OR Anycubic Kobra OR Anycubic Photon',
    "Formlabs":   'Formlabs 3D printing OR Formlabs Form 3 OR Formlabs Form 4',
    "Longer":     '"Longer 3D" printer OR "Longer Orange" OR "Longer Cube"',
    "Elegoo":     'Elegoo 3D printer OR Elegoo Saturn OR Elegoo Neptune',
    "Creality":   'Creality 3D printer OR Creality Ender OR Creality K1 OR Creality K2',
    "Cricut":     'Cricut maker OR Cricut Explore OR Cricut Joy OR Cricut cutting machine',
}

GOOGLE_NEWS_BASE = (
    "https://news.google.com/rss/search"
    "?q={query}&hl=en-US&gl=US&ceid=US:en"
)

# ─── UVPM lookup table ────────────────────────────────────────────────────────
# Static estimates used for article ranking only.

UVPM_TABLE: dict[str, int] = {
    "The Verge":             48_000_000,
    "CNET":                  38_000_000,
    "Engadget":              22_000_000,
    "TechCrunch":            20_000_000,
    "Wired":                 18_000_000,
    "Tom's Guide":           15_000_000,
    "ZDNet":                 14_000_000,
    "Digital Trends":        13_000_000,
    "PCMag":                 12_000_000,
    "Mashable":              10_000_000,
    "TechRadar":             10_000_000,
    "Gizmodo":                8_000_000,
    "SlashGear":              3_000_000,
    "3DPrint.com":            2_100_000,
    "All3DP":                 1_800_000,
    "Hackaday":               1_500_000,
    "Make: Magazine":         1_200_000,
    "3D Printing Industry":     850_000,
    "Hackster.io":              800_000,
    "Fabbaloo":                 420_000,
    # Common Google News sources that may appear
    "Forbes":                30_000_000,
    "Business Insider":      20_000_000,
    "Ars Technica":          10_000_000,
    "9to5Mac":                8_000_000,
    "Android Authority":      6_000_000,
    "Yahoo News":            40_000_000,
    "Reuters":               35_000_000,
    "Bloomberg":             25_000_000,
}

TIER_TABLE: dict[str, int] = {
    **{k: 1 for k in [
        "The Verge", "CNET", "Engadget", "TechCrunch", "Wired",
        "Tom's Guide", "ZDNet", "Digital Trends", "PCMag", "Mashable",
        "TechRadar", "Gizmodo", "SlashGear", "Forbes", "Business Insider",
        "Ars Technica", "Yahoo News", "Reuters", "Bloomberg",
    ]},
    **{k: 2 for k in [
        "3DPrint.com", "All3DP", "Hackaday", "Make: Magazine",
        "3D Printing Industry", "Hackster.io", "Fabbaloo",
        "9to5Mac", "Android Authority",
    ]},
}
DEFAULT_UVPM = 300_000   # for sources not in the table


# ─── Classification rules ─────────────────────────────────────────────────────
# Priority: hands_on_review > product_news > brand_news (fallback)

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
        r"\btariff\b",
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


def classify(title: str, summary: str) -> str:
    combined = f"{title} {summary}".lower()
    for cat in ["hands_on_review", "product_news", "brand_news"]:
        if any(re.search(p, combined) for p in CATEGORY_RULES[cat]):
            return cat
    return "brand_news"


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


def fetch_feed(url: str, timeout: int = 20) -> feedparser.FeedParserDict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return feedparser.parse(resp.text)


def get_source_name(entry) -> str:
    """Extract the media outlet name from a Google News RSS entry."""
    # Primary: feedparser 'source' attribute (most reliable)
    src = getattr(entry, "source", None)
    if src:
        name = (src.get("title", "") if isinstance(src, dict)
                else getattr(src, "title", ""))
        if name:
            return name.strip()

    # Fallback: Google News titles are formatted as "Article Title - Media Name"
    title = getattr(entry, "title", "") or ""
    if " - " in title:
        return title.rsplit(" - ", 1)[-1].strip()

    return "Unknown"


def clean_title(title: str, source: str) -> str:
    """Remove the ' - Media Name' suffix Google appends to titles."""
    suffix = f" - {source}"
    if title.endswith(suffix):
        return title[: -len(suffix)].strip()
    return title.strip()


# ─── Main crawler ─────────────────────────────────────────────────────────────

def crawl(lookback_days: int = 1) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # url → article dict  (for deduplication + multi-brand merging)
    articles_map: dict[str, dict] = {}
    media_hit: set[str] = set()

    for brand, query in BRAND_QUERIES.items():
        url = GOOGLE_NEWS_BASE.format(query=quote_plus(query))
        print(f"  Searching '{brand}'...", end=" ", flush=True)
        try:
            feed = fetch_feed(url)
            matched = 0

            for entry in feed.entries:
                link    = getattr(entry, "link",    "") or ""
                title   = getattr(entry, "title",   "") or ""
                summary = strip_html(getattr(entry, "summary", "") or "")
                pub_dt  = parse_entry_date(entry)

                # Date filter (skip if older than cutoff and date is known)
                if pub_dt and pub_dt < cutoff:
                    continue

                source = get_source_name(entry)
                display_title = clean_title(title, source)

                if link in articles_map:
                    # Article already seen from another brand search — just add brand
                    if brand not in articles_map[link]["brands"]:
                        articles_map[link]["brands"].append(brand)
                    continue

                uvpm = UVPM_TABLE.get(source, DEFAULT_UVPM)
                tier = TIER_TABLE.get(source, 2)
                media_hit.add(source)
                matched += 1

                articles_map[link] = {
                    "id":           article_id(link),
                    "title":        display_title,
                    "url":          link,
                    "media":        source,
                    "uvpm":         uvpm,
                    "uvpm_display": fmt_uvpm(uvpm),
                    "tier":         tier,
                    "category":     classify(display_title, summary),
                    "brands":       [brand],
                    "published":    pub_dt.isoformat() if pub_dt else None,
                    "summary":      summary[:400],
                }

            print(f"{matched} hit(s)")
        except Exception as e:
            print(f"ERROR — {e}")

    # Convert map to sorted list
    articles = sorted(articles_map.values(), key=lambda a: a["uvpm"], reverse=True)

    # Brand counts
    brand_counts = {b: 0 for b in BRAND_QUERIES}
    for a in articles:
        for b in a["brands"]:
            if b in brand_counts:
                brand_counts[b] += 1

    total_reach = sum(
        UVPM_TABLE.get(m, DEFAULT_UVPM) for m in media_hit
    )
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return {
        "date":       today,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "stats": {
            "total":               len(articles),
            "total_reach_display": fmt_uvpm(total_reach),
            "brands_hit":          sum(1 for v in brand_counts.values() if v > 0),
            "brands_total":        len(BRAND_QUERIES),
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

