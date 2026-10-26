"""Scan orchestration: outlets -> candidate URLs -> article metadata -> topic match -> contacts."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx

from . import db
from .config import Settings, settings as default_settings
from .discovery import (Candidate, canonical, domain_of, from_feeds, from_gdelt, from_newsapi,
                        from_sitemaps)
from .enrich import enrich_journalists
from .extract import ArticleMeta, Author, clean_person_name, extract_article, split_byline
from .fetcher import Fetcher
from .models import ScanParams
from .results import aggregate
from .topics import Topic, build_topic, match_text

log = logging.getLogger(__name__)

_tasks: dict[str, asyncio.Task] = {}


def start_scan(params: ScanParams, settings: Settings | None = None,
               transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Create a scan record and run it in the background on the current event loop."""
    scan_id = uuid.uuid4().hex[:12]
    db.create_scan(scan_id, params.model_dump())
    cfg = settings or default_settings
    if cfg.demo_mode:
        from .demo import run_demo_scan  # imported lazily: demo mode is the exception
        coro = run_demo_scan(scan_id, params)
    else:
        coro = run_scan(scan_id, params, cfg, transport)
    task = asyncio.create_task(coro)
    _tasks[scan_id] = task
    task.add_done_callback(lambda _: _tasks.pop(scan_id, None))
    return scan_id


def cancel_scan(scan_id: str) -> bool:
    task = _tasks.get(scan_id)
    if not task:
        return False
    task.cancel()
    return True


def _outlet_index(outlets: list[dict]) -> dict[str, dict]:
    return {o["domain"]: o for o in outlets}


def _match_outlet(url: str, index: dict[str, dict]) -> str | None:
    host = domain_of(url)
    parts = host.split(".")
    for i in range(len(parts) - 1):
        candidate = ".".join(parts[i:])
        if candidate in index:
            return candidate
    return None


async def run_scan(scan_id: str, params: ScanParams, settings: Settings,
                   transport: httpx.AsyncBaseTransport | None = None) -> None:
    try:
        db.update_scan(scan_id, status="running", stage="discovering")
        topic = build_topic(params.industries, params.custom_terms, params.executives, params.match_mode)
        days = params.lookback_days
        since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
        outlets = db.list_outlets(params.regions, params.max_outlets)
        index = _outlet_index(outlets)
        db.update_scan(scan_id, outlets_total=len(outlets))

        async with Fetcher(settings, transport) as fetcher:
            extra: dict[str, list[Candidate]] = defaultdict(list)
            searches = []
            if settings.gdelt_enabled:
                searches.append(from_gdelt(fetcher, topic, since))
            if settings.newsapi_key:
                searches.append(from_newsapi(fetcher, topic, since, list(index), settings.newsapi_key))
            for batch in await asyncio.gather(*searches, return_exceptions=True):
                if isinstance(batch, BaseException):
                    log.warning("search provider failed: %s", batch)
                    continue
                for cand in batch:
                    if (d := _match_outlet(cand.url, index)):
                        extra[d].append(cand)

            db.update_scan(scan_id, stage="scanning outlets")
            sem = asyncio.Semaphore(settings.outlet_concurrency)

            async def do_outlet(outlet: dict) -> None:
                async with sem:
                    try:
                        await scan_outlet(scan_id, fetcher, outlet, topic, since, extra[outlet["domain"]], settings)
                    except Exception:  # one broken site must not sink the scan
                        log.exception("outlet %s failed", outlet["domain"])
                    finally:
                        db.increment_scan(scan_id, outlets_done=1)

            await asyncio.gather(*(do_outlet(o) for o in outlets))

            db.update_scan(scan_id, stage="finding contact details")
            journalists = aggregate(db.scan_articles(scan_id), params.min_articles)
            await enrich_journalists(fetcher, journalists, settings)

        db.update_scan(scan_id, status="done", stage="done", finished_at=db.utcnow())
    except asyncio.CancelledError:
        db.update_scan(scan_id, status="cancelled", stage="cancelled", finished_at=db.utcnow())
        raise
    except Exception as exc:
        log.exception("scan %s failed", scan_id)
        db.update_scan(scan_id, status="failed", error=str(exc)[:500], finished_at=db.utcnow())


async def scan_outlet(scan_id: str, fetcher: Fetcher, outlet: dict, topic: Topic, since: datetime | None,
                      extra: list[Candidate], settings: Settings) -> None:
    sitemap_cands, feed_cands = await asyncio.gather(
        from_sitemaps(fetcher, outlet, topic, since, settings.max_child_sitemaps),
        from_feeds(fetcher, outlet, topic, since),
    )
    merged: dict[str, Candidate] = {}
    for cand in [*extra, *sitemap_cands, *feed_cands]:
        key = canonical(cand.url)
        prev = merged.get(key)
        if not prev:
            merged[key] = cand
        else:
            prev.title = prev.title or cand.title
            prev.published = prev.published or cand.published
            prev.author_hint = prev.author_hint or cand.author_hint

    floor = datetime.min.replace(tzinfo=timezone.utc)
    ranked = sorted(merged.values(), key=lambda c: c.published or floor, reverse=True)
    ranked = ranked[: settings.max_candidates_per_outlet]
    await asyncio.gather(*(check_candidate(scan_id, fetcher, outlet, topic, since, c, settings) for c in ranked))


async def check_candidate(scan_id: str, fetcher: Fetcher, outlet: dict, topic: Topic, since: datetime | None,
                          cand: Candidate, settings: Settings) -> None:
    key = canonical(cand.url)
    cached = db.cache_get(key, settings.page_cache_days)
    if cached:
        meta = ArticleMeta.from_dict(cached)
    else:
        html = await fetcher.get_text(cand.url)
        if not html:
            return
        meta = extract_article(html, cand.url, outlet.get("name"))
        db.cache_put(key, meta.to_dict())
    db.increment_scan(scan_id, articles_checked=1)

    published = meta.published_dt or cand.published
    if since and (not published or published < since):
        return

    authors = meta.authors
    if not authors and cand.author_hint:
        names = [clean_person_name(n, outlet.get("name")) for n in split_byline(cand.author_hint)]
        authors = [Author(name=n) for n in names if n]
    if not authors:
        return

    headline = " ".join(filter(None, [meta.title or cand.title or "", meta.description, " ".join(meta.keywords)]))
    matched = match_text(topic, headline, meta.text)
    if not matched:
        return

    inserted = db.insert_article(scan_id, {
        "url": key,
        "domain": outlet["domain"],
        "title": meta.title or cand.title,
        "published": published.isoformat(timespec="seconds") if published else None,
        "authors": [a.__dict__ for a in authors],
        "matched": matched,
    })
    if inserted:
        db.increment_scan(scan_id, articles_matched=1)
