"""Contact enrichment for journalists who passed the article threshold."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from . import db
from .config import Settings
from .discovery import domain_of
from .extract import extract_contacts, merge_contacts
from .fetcher import Fetcher

log = logging.getLogger(__name__)

MAX_PROFILE_PAGES = 2


def _fresh(updated_at: str | None, max_age_days: int) -> bool:
    if not updated_at:
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(updated_at)
    return age.days <= max_age_days


async def hunter_lookup(fetcher: Fetcher, name: str, domain: str, api_key: str) -> dict[str, Any]:
    parts = name.split()
    if len(parts) < 2:
        return {}
    data = await fetcher.get_json("https://api.hunter.io/v2/email-finder", {
        "domain": domain, "first_name": parts[0], "last_name": parts[-1], "api_key": api_key,
    })
    if not isinstance(data, dict):
        return {}
    found = data.get("data") or {}
    email, score = found.get("email"), found.get("score") or 0
    if not email:
        return {}
    return {
        "email": email,
        "email_source": "hunter.io (inferred)",
        "email_confidence": "high" if score >= 80 else "medium" if score >= 50 else "low",
    }


async def enrich_journalists(fetcher: Fetcher, journalists: list[dict[str, Any]], settings: Settings) -> int:
    """Fetch author pages (and optionally Hunter.io) and store contacts. Returns count updated."""
    existing = db.get_authors([j["key"] for j in journalists])
    updated = 0

    async def one(j: dict[str, Any]) -> None:
        nonlocal updated
        prev = existing.get(j["key"])
        if (prev and _fresh(prev["updated_at"], settings.page_cache_days)
                and set(j["profile_urls"]) <= set(prev["profile_urls"])):
            return
        found = []
        for url in j["profile_urls"][:MAX_PROFILE_PAGES]:
            html = await fetcher.get_text(url)
            if html:
                found.append(extract_contacts(html, url, j["name"], domain_of(url)))
        contact = merge_contacts(*found, j["hint_contact"])
        if "email" not in contact and settings.hunter_api_key and j["outlets"]:
            contact.update(await hunter_lookup(fetcher, j["name"], j["outlets"][0], settings.hunter_api_key))
        profile_urls = sorted(set(j["profile_urls"]) | set(prev["profile_urls"] if prev else []))
        db.save_author(j["key"], j["name"], profile_urls, contact)
        updated += 1

    await asyncio.gather(*(one(j) for j in journalists))
    return updated
