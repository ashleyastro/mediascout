"""A small simulated news site served through httpx.MockTransport for offline end-to-end tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

DOMAIN = "fakenews.example"
BASE = f"https://www.{DOMAIN}"
NOW = datetime.now(timezone.utc)


def _iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


# (slug, headline, body, author key, days ago)
ARTICLES = [
    ("crm-market-shakeup", "CRM vendors brace for a market shakeup",
     "Salesforce and HubSpot are rethinking CRM pricing as customer relationship management matures.", "jane", 10),
    ("salesforce-agentforce-pricing", "Salesforce changes Agentforce pricing",
     "Salesforce said the CRM changes take effect next quarter.", "jane", 40),
    ("hubspot-crm-smb", "HubSpot pushes CRM deeper into SMB",
     "HubSpot CRM added new revenue operations features for small teams.", "jane", 90),
    ("crm-old-story", "An old CRM story outside the window",
     "Salesforce CRM CRM history.", "jane", 400),
    ("crm-ai-agents", "CRM meets AI agents",
     "Salesforce and other CRM platforms add agents.", "bob", 15),
    ("crm-roundup", "Weekly CRM roundup",
     "HubSpot CRM news and Salesforce updates.", "bob", 20),
    ("crypto-market-today", "Crypto prices today", "Bitcoin moved. Nothing about SaaS.", "bob", 5),
    ("crm-staff-brief", "CRM staff brief", "Salesforce CRM update.", "staff", 3),
]

# Only listed in the 2019 monthly sitemap, so it is found by all-time scans and skipped by dated ones.
OLD_ARTICLE = ("crm-2019-retrospective", "How CRM looked in 2019",
               "Salesforce and HubSpot dominated CRM.", "jane", (NOW - datetime(2019, 1, 15, tzinfo=timezone.utc)).days)

AUTHORS = {
    "jane": {"name": "Jane Doe", "path": "/author/jane-doe"},
    "bob": {"name": "Bob Smith", "path": "/author/bob-smith"},
    "staff": {"name": "Staff Writer", "path": "/author/staff"},
}


def article_html(slug: str, headline: str, body: str, author: str, days_ago: int) -> str:
    a = AUTHORS[author]
    ld = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "NewsArticle", "headline": headline, "datePublished": _iso(days_ago),
             "author": [{"@id": f"{BASE}#{author}"}], "description": body[:80]},
            {"@type": "Person", "@id": f"{BASE}#{author}", "name": a["name"], "url": BASE + a["path"]},
        ],
    }
    return f"""<html><head><title>{headline}</title>
<script type="application/ld+json">{json.dumps(ld)}</script></head>
<body><header><a href="https://twitter.com/fakenews">Follow us</a></header>
<article><h1>{headline}</h1><p>{body}</p></article>
<footer>contact: advertising@{DOMAIN}</footer></body></html>"""


def author_html(author: str) -> str:
    if author == "jane":
        return f"""<html><body><header><a href="https://twitter.com/fakenews">@fakenews</a></header>
<main><h1>Jane Doe</h1><p>Senior reporter covering enterprise software.</p>
<a href="mailto:jane.doe@{DOMAIN}">Email Jane</a>
<a href="https://x.com/janedoe">X</a>
<a href="https://www.linkedin.com/in/jane-doe-reporter/">LinkedIn</a>
<a href="https://www.linkedin.com/company/fakenews">Company page</a>
</main><footer><a href="https://twitter.com/fakenews">us</a></footer></body></html>"""
    return "<html><body><main><h1>Bob Smith</h1><p>Reporter.</p></main></body></html>"


def sitemap_index() -> str:
    month = NOW.strftime("%Y-%m")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{BASE}/sitemap-posts-{month}.xml</loc><lastmod>{_iso(1)}</lastmod></sitemap>
  <sitemap><loc>{BASE}/sitemap-posts-2019-01.xml</loc></sitemap>
  <sitemap><loc>{BASE}/sitemap-tags.xml</loc></sitemap>
</sitemapindex>"""


def sitemap_posts(articles=ARTICLES) -> str:
    urls = "".join(
        f"<url><loc>{BASE}/news/{slug}</loc><lastmod>{_iso(days)}</lastmod></url>"
        for slug, _, _, _, days in articles
    )
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'


class FakeSite:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        path = request.url.path
        if request.url.host != f"www.{DOMAIN}":
            return httpx.Response(404)
        if path == "/robots.txt":
            return httpx.Response(200, text=f"User-agent: *\nDisallow: /private\nSitemap: {BASE}/sitemap_index.xml\n")
        if path == "/sitemap_index.xml":
            return httpx.Response(200, text=sitemap_index())
        if path.startswith("/sitemap-posts-"):
            if "2019" in path:
                return httpx.Response(200, text=sitemap_posts([OLD_ARTICLE]))
            return httpx.Response(200, text=sitemap_posts())
        if path == "/sitemap-tags.xml":
            raise AssertionError("tag sitemap should have been skipped")
        if path.startswith("/news/"):
            slug = path.removeprefix("/news/")
            for s, headline, body, author, days in [*ARTICLES, OLD_ARTICLE]:
                if s == slug:
                    return httpx.Response(200, text=article_html(s, headline, body, author, days),
                                          headers={"content-type": "text/html; charset=utf-8"})
        for key, a in AUTHORS.items():
            if path == a["path"]:
                return httpx.Response(200, text=author_html(key), headers={"content-type": "text/html"})
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
