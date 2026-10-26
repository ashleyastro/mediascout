"""Group matched articles by byline and assemble the journalist list."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from . import db
from .extract import classify_link, normalize_name

SAMPLE_ARTICLES = 5


def aggregate(articles: list[dict[str, Any]], min_articles: int) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    spellings: dict[str, Counter] = defaultdict(Counter)

    for art in articles:
        for author in art["authors"]:
            key = normalize_name(author["name"])
            spellings[key][author["name"]] += 1
            j = by_key.setdefault(key, {
                "key": key, "articles": [], "outlet_counts": Counter(), "topics": set(),
                "profile_urls": [], "hint_contact": {},
            })
            j["articles"].append({
                "title": art.get("title") or art["url"],
                "url": art["url"],
                "published": art.get("published"),
                "domain": art["domain"],
            })
            j["outlet_counts"][art["domain"]] += 1
            j["topics"].update(art["matched"])
            url = author.get("url")
            if url:
                if classify_link(url):
                    j["hint_contact"].setdefault(classify_link(url), url)
                elif url not in j["profile_urls"]:
                    j["profile_urls"].append(url)
            for link in author.get("same_as") or []:
                kind = classify_link(link)
                if kind:
                    j["hint_contact"].setdefault(kind, link)
            if author.get("email"):
                j["hint_contact"].setdefault("email", author["email"])
                j["hint_contact"].setdefault("email_source", "article metadata")
                j["hint_contact"].setdefault("email_confidence", "high")

    out = []
    for key, j in by_key.items():
        # The same URL can appear twice if an outlet serves it on two hosts; count unique stories.
        unique = {a["url"]: a for a in j["articles"]}.values()
        arts = sorted(unique, key=lambda a: a["published"] or "", reverse=True)
        if len(arts) < min_articles:
            continue
        out.append({
            "key": key,
            "name": spellings[key].most_common(1)[0][0],
            "article_count": len(arts),
            "outlets": [d for d, _ in j["outlet_counts"].most_common()],
            "topics": sorted(j["topics"]),
            "latest_published": arts[0]["published"],
            "articles": arts,
            "profile_urls": j["profile_urls"],
            "hint_contact": j["hint_contact"],
        })
    out.sort(key=lambda j: (-j["article_count"], j["name"]))
    return out


def journalists_for_scan(scan_id: str) -> list[dict[str, Any]]:
    """Final, export-ready rows: aggregated bylines + stored contacts, minus opt-outs."""
    scan = db.get_scan(scan_id)
    if not scan:
        return []
    groups = aggregate(db.scan_articles(scan_id), int(scan["params"].get("min_articles", 5)))
    stored = db.get_authors([g["key"] for g in groups])
    suppressed = db.list_suppressions()
    rows = []
    for g in groups:
        record = stored.get(g["key"])
        contact = dict(g["hint_contact"])
        if record:
            contact = {**contact, **{k: v for k, v in record["contact"].items() if v}}
        email = (contact.get("email") or "").lower()
        if g["key"] in suppressed or (email and email in suppressed):
            continue
        rows.append({
            "name": g["name"],
            "article_count": g["article_count"],
            "outlets": g["outlets"],
            "topics": g["topics"],
            "latest_published": g["latest_published"],
            "contact": contact,
            "articles": g["articles"],
        })
    return rows
