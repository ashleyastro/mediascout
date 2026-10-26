"""Find candidate article URLs for an outlet (all time, or since an optional cutoff).

Sources, cheapest first:
  1. XML sitemaps (robots.txt Sitemap lines, then common paths). Sitemaps go back months or
     years, which is what makes an all-time count feasible without a paid index.
  2. RSS/Atom feeds (recent items only).
  3. Keyword search providers across all outlets at once: GDELT (free, ~3 months back) and
     NewsAPI.org (API key; history depends on plan).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

import feedparser
from bs4 import BeautifulSoup

from .extract import parse_date
from .fetcher import Fetcher
from .topics import Topic, prefilter_hit, search_queries

log = logging.getLogger(__name__)

COMMON_SITEMAPS = ["/sitemap_index.xml", "/sitemap.xml", "/news-sitemap.xml", "/sitemap-news.xml",
                   "/wp-sitemap.xml"]
COMMON_FEEDS = ["/feed", "/rss", "/feed.xml", "/rss.xml", "/index.xml", "/feeds/all.rss"]
SKIP_SITEMAP_WORDS = ("tag", "category", "categories", "author", "attachment", "page-sitemap", "video",
                      "image", "topic", "taxonomy", "product", "event", "podcast", "gallery")
YEAR_MONTH_RE = re.compile(r"(20\d{2})[-/_]?(0[1-9]|1[0-2])(?!\d)")
YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


@dataclass
class Candidate:
    url: str
    published: datetime | None = None
    title: str | None = None
    source: str = ""
    author_hint: str | None = None


def canonical(url: str) -> str:
    p = urlparse(url.strip())
    host = p.netloc.lower()
    path = p.path or "/"
    return f"https://{host}{path}".rstrip("/") if path != "/" else f"https://{host}/"


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def belongs_to(url: str, domain: str) -> bool:
    host = domain_of(url)
    return host == domain or host.endswith("." + domain)


def _sitemap_too_old(loc: str, lastmod: datetime | None, since: datetime | None) -> bool:
    if since is None:
        return False
    if lastmod and lastmod < since:
        return True
    path = urlparse(loc).path
    m = YEAR_MONTH_RE.search(path)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        return (year, month) < (since.year, since.month)
    m = YEAR_RE.search(path)
    if m:
        return int(m.group(1)) < since.year
    return False


def parse_sitemap(xml: str) -> tuple[list[tuple[str, datetime | None]], list[Candidate]]:
    """Return (child sitemaps, url entries)."""
    soup = BeautifulSoup(xml, "xml")
    children = []
    for sm in soup.find_all("sitemap"):
        loc = sm.find("loc")
        if loc and loc.text.strip():
            lastmod = sm.find("lastmod")
            children.append((loc.text.strip(), parse_date(lastmod.text) if lastmod else None))
    entries = []
    for u in soup.find_all("url"):
        loc = u.find("loc")
        if not loc or not loc.text.strip():
            continue
        pub = u.find("publication_date")
        lastmod = u.find("lastmod")
        title = u.find("title")  # news:title (namespace prefix dropped by the parser)
        published = parse_date(pub.text) if pub else None
        entries.append(Candidate(
            url=loc.text.strip(),
            published=published or (parse_date(lastmod.text) if lastmod else None),
            title=title.text.strip() if title else None,
            source="sitemap",
        ))
    return children, entries


async def from_sitemaps(fetcher: Fetcher, outlet: dict, topic: Topic, since: datetime | None,
                        max_children: int) -> list[Candidate]:
    domain = outlet["domain"]
    base = f"https://{'www.' if domain.count('.') == 1 else ''}{domain}"
    roots: list[str] = []
    if outlet.get("sitemap_url"):
        roots.append(outlet["sitemap_url"])
    # robots.txt usually lists the site's sitemaps.
    await fetcher.prime_robots(base + "/")
    roots.extend(fetcher.robots_sitemaps(base + "/"))
    if not roots:
        roots = [base + p for p in COMMON_SITEMAPS]

    seen: set[str] = set()
    queue = [(r, 0) for r in dict.fromkeys(roots)]
    out: list[Candidate] = []
    fetched_children = 0
    while queue and fetched_children < max_children:
        loc, depth = queue.pop(0)
        if loc in seen:
            continue
        seen.add(loc)
        xml = await fetcher.get_text(loc)
        fetched_children += 1
        if not xml or "<" not in xml[:500]:
            continue
        children, entries = parse_sitemap(xml)
        if depth < 3:
            # Newest sitemaps first: most indexes list them chronologically, in either order.
            ranked = sorted(children, key=lambda c: (c[1] or datetime.min.replace(tzinfo=timezone.utc)),
                            reverse=True)
            for child, lastmod in ranked:
                name = urlparse(child).path.lower()
                if any(w in name for w in SKIP_SITEMAP_WORDS):
                    continue
                if _sitemap_too_old(child, lastmod, since):
                    continue
                queue.append((child, depth + 1))
        for e in entries:
            if since and e.published and e.published < since:
                continue
            if belongs_to(e.url, domain) and prefilter_hit(topic, e.url, e.title):
                out.append(e)
    log.info("%s: %d sitemap candidates from %d sitemaps", domain, len(out), fetched_children)
    return out


async def from_feeds(fetcher: Fetcher, outlet: dict, topic: Topic, since: datetime | None) -> list[Candidate]:
    domain = outlet["domain"]
    base = f"https://{'www.' if domain.count('.') == 1 else ''}{domain}"
    feeds = [outlet["feed_url"]] if outlet.get("feed_url") else [base + p for p in COMMON_FEEDS[:2]]
    out: list[Candidate] = []
    for feed_url in feeds:
        body = await fetcher.get_text(feed_url)
        if not body:
            continue
        parsed = feedparser.parse(body)
        for entry in parsed.entries:
            link = entry.get("link")
            if not link or not belongs_to(link, domain):
                continue
            published = parse_date(entry.get("published") or entry.get("updated") or "")
            if since and published and published < since:
                continue
            title = entry.get("title", "")
            summary = BeautifulSoup(entry.get("summary", ""), "lxml").get_text(" ")
            if prefilter_hit(topic, link, f"{title} {summary}"):
                out.append(Candidate(url=link, published=published, title=title, source="rss",
                                     author_hint=entry.get("author")))
        if parsed.entries:
            break
    return out


# ---------------------------------------------------------------- cross-outlet search providers

async def from_gdelt(fetcher: Fetcher, topic: Topic, since: datetime | None) -> list[Candidate]:
    """GDELT DOC 2.0 API. Free, no key; covers roughly the last 3 months."""
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=89)
    if since:
        start = max(since, start)
    out = []
    for q in search_queries(topic):
        params = {
            "query": q,
            "mode": "artlist",
            "format": "json",
            "maxrecords": "250",
            "sort": "datedesc",
            "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        }
        data = await fetcher.get_json("https://api.gdeltproject.org/api/v2/doc/doc", params)
        if not isinstance(data, dict):
            continue
        for art in data.get("articles", []):
            out.append(Candidate(
                url=art.get("url", ""),
                published=parse_date(art.get("seendate", "")),
                title=art.get("title"),
                source="gdelt",
            ))
    return [c for c in out if c.url]


async def from_newsapi(fetcher: Fetcher, topic: Topic, since: datetime | None, domains: list[str],
                       api_key: str) -> list[Candidate]:
    """NewsAPI.org /v2/everything. Returns bylines too, which helps sites without JSON-LD."""
    out = []
    for q in search_queries(topic):
        for i in range(0, len(domains), 20):
            params = {
                "q": q,
                "domains": ",".join(domains[i : i + 20]),
                "sortBy": "publishedAt",
                "pageSize": "100",
                "language": "en",
                "apiKey": api_key,
            }
            if since:  # without "from", NewsAPI returns as far back as the plan allows
                params["from"] = since.date().isoformat()
            data = await fetcher.get_json("https://newsapi.org/v2/everything?" + urlencode(params))
            if not isinstance(data, dict):
                continue
            for art in data.get("articles", []):
                out.append(Candidate(
                    url=art.get("url", ""),
                    published=parse_date(art.get("publishedAt", "")),
                    title=art.get("title"),
                    source="newsapi",
                    author_hint=art.get("author"),
                ))
    return [c for c in out if c.url]

