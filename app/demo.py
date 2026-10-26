"""Demo mode: a self-contained dataset of fictional outlets, journalists and executives.

Nothing here is real. Outlets use the reserved .example domain, so links and emails can never
reach a real site or inbox, and no social profiles are generated, so no handle can collide
with a real account. Demo scans run the real topic matcher and aggregation over this dataset
instead of crawling the web.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db
from .extract import normalize_name
from .models import ScanParams
from .topics import INDUSTRIES, build_topic, match_text, slugify

log = logging.getLogger(__name__)

SAMPLE_SCAN_ID = "sample"
MAX_DEMO_SCANS = 100
STEP_DELAY = 0.35  # seconds per outlet, so the progress bar is visible

DEMO_OUTLETS = [
    ("cloudledger.example", "The Cloud Ledger", "NA", "US", "enterprise tech"),
    ("saasweekly.example", "SaaS Weekly", "NA", "US", "saas"),
    ("revenuewire.example", "Revenue Wire", "NA", "US", "business"),
    ("stackandscale.example", "Stack & Scale", "NA", "US", "developer"),
    ("northernbyte.example", "Northern Byte", "NA", "CA", "tech"),
    ("securebrief.example", "Secure Brief", "NA", "US", "security"),
    ("londonledger.example", "The London Ledger", "UK", "GB", "business"),
    ("fintechquarter.example", "Fintech Quarter", "UK", "GB", "fintech"),
    ("berlinbyte.example", "Berlin Byte", "EU", "DE", "tech"),
    ("matinnumerique.example", "Matin Numérique", "EU", "FR", "tech"),
    ("canalnorte.example", "Canal Norte", "EU", "ES", "business"),
    ("dataharbor.example", "Data Harbor", "EU", "NL", "data"),
]

# Fictional executives at fictional companies.
DEMO_EXECUTIVES = {
    "Dana Whitfield": ("Cloudlane", "crm-sales"),
    "Marcus Oyelaran": ("Ledgerly", "fintech-saas"),
    "Ingrid Solberg": ("Nordshield", "cybersecurity"),
    "Priya Raman": ("Quillstack", "ai-saas"),
}

# (name, outlets, beats, articles, executive they cover or None)
DEMO_JOURNALISTS = [
    ("Avery Collins", ["cloudledger.example"], ["crm-sales", "general-saas"], 14, "Dana Whitfield"),
    ("Jordan Ellis", ["saasweekly.example", "revenuewire.example"], ["crm-sales"], 9, "Dana Whitfield"),
    ("Morgan Reyes", ["revenuewire.example"], ["crm-sales", "martech"], 6, None),
    ("Taylor Brooks", ["saasweekly.example"], ["crm-sales"], 4, None),
    ("Riley Chen", ["securebrief.example"], ["cybersecurity"], 16, "Ingrid Solberg"),
    ("Casey Nakamura", ["securebrief.example", "cloudledger.example"], ["cybersecurity", "cloud-infra"], 8, None),
    ("Elena Fischer", ["berlinbyte.example"], ["cybersecurity", "ai-saas"], 7, "Ingrid Solberg"),
    ("Sam Okafor", ["stackandscale.example"], ["devtools", "cloud-infra"], 12, None),
    ("Noor Haddad", ["stackandscale.example"], ["devtools", "ai-saas"], 5, "Priya Raman"),
    ("Lucas Moreau", ["matinnumerique.example"], ["ai-saas", "general-saas"], 10, "Priya Raman"),
    ("Camille Laurent", ["matinnumerique.example"], ["hr-tech"], 6, None),
    ("Hannah Price", ["londonledger.example"], ["fintech-saas", "erp"], 11, "Marcus Oyelaran"),
    ("Oliver Grant", ["fintechquarter.example", "londonledger.example"], ["fintech-saas"], 9, "Marcus Oyelaran"),
    ("Isla Murray", ["fintechquarter.example"], ["fintech-saas"], 3, None),
    ("Mateo Navarro", ["canalnorte.example"], ["ecommerce", "general-saas"], 7, None),
    ("Lucía Ortega", ["canalnorte.example"], ["hr-tech", "collaboration"], 5, None),
    ("Pieter de Vries", ["dataharbor.example"], ["data-analytics"], 13, None),
    ("Femke Jansen", ["dataharbor.example", "berlinbyte.example"], ["data-analytics", "ai-saas"], 6, "Priya Raman"),
    ("Grace Liu", ["northernbyte.example"], ["hr-tech", "customer-service"], 8, None),
    ("Ethan Clarke", ["northernbyte.example", "cloudledger.example"], ["customer-service", "crm-sales"], 5, "Dana Whitfield"),
    ("Maya Patel", ["cloudledger.example"], ["healthtech", "vertical-saas"], 9, None),
    ("Leo Bauer", ["berlinbyte.example"], ["erp", "data-analytics"], 4, None),
    ("Zoe Martin", ["saasweekly.example"], ["martech", "ecommerce"], 7, None),
    ("Ben Adler", ["revenuewire.example"], ["general-saas"], 2, None),
]

HEADLINES = [
    "Why {term} is back on the CIO agenda",
    "{term}: what buyers are asking for this year",
    "Inside the race to rebuild {term}",
    "The {term} market is consolidating. Here is who wins",
    "Five takeaways from this week's {term} earnings calls",
    "{term} vendors bet on AI agents to defend pricing",
    "How mid-market teams are rethinking {term}",
    "{term} startups raise new rounds despite a slower market",
]
EXEC_HEADLINES = [
    "{name} says {company} will double down on {term}",
    "Interview: {name} on where {company} goes next",
    "{company} CEO {name} outlines a new {term} strategy",
]


def _contact(name: str, domain: str) -> dict[str, Any]:
    local = ".".join(slugify(p) for p in name.split())
    return {
        "email": f"{local}@{domain}",
        "email_source": "author page",
        "email_confidence": "high",
        "profile_url": f"https://www.{domain}/author/{slugify(name)}",
    }


def build_dataset(now: datetime | None = None) -> list[dict[str, Any]]:
    """Deterministic list of fictional articles, each with title, body, date and byline."""
    now = now or datetime.now(timezone.utc)
    rng = random.Random(2026)
    articles = []
    for name, outlets, beats, count, exec_name in DEMO_JOURNALISTS:
        for i in range(count):
            domain = outlets[i % len(outlets)]
            beat = beats[i % len(beats)]
            # Generic lowercase phrases only, so no fictional story names a real company.
            terms = [t for t in INDUSTRIES[beat]["terms"] if " " in t and t == t.lower()]
            term = rng.choice(terms)
            if exec_name and i % 3 == 0:
                company, _ = DEMO_EXECUTIVES[exec_name]
                title = rng.choice(EXEC_HEADLINES).format(name=exec_name, company=company, term=term)
                body = f"{exec_name}, chief executive of {company}, discussed {term} and the wider market."
            else:
                title = rng.choice(HEADLINES).format(term=term)
                title = title[0].upper() + title[1:]
                body = f"An analysis of {term} for enterprise buyers."
            # Spread stories from 2019 to last week, newest first for each journalist.
            published = now - timedelta(days=int(7 + i * (2400 / max(count, 1)) + rng.randint(0, 20)))
            articles.append({
                "url": f"https://www.{domain}/{published:%Y/%m}/{slugify(title)}",
                "domain": domain,
                "title": title,
                "body": body,
                "published": published.isoformat(timespec="seconds"),
                "author": name,
            })
    return articles


def seed_demo() -> None:
    """Load the fictional outlets and contacts, and build the sample scan once."""
    db.upsert_outlets([
        {"domain": d, "name": n, "region": r, "country": c, "category": cat, "priority": i,
         "feed_url": None, "sitemap_url": None, "active": 1}
        for i, (d, n, r, c, cat) in enumerate(DEMO_OUTLETS)
    ])
    for name, outlets, *_ in DEMO_JOURNALISTS:
        db.save_author(normalize_name(name), name, [_contact(name, outlets[0])["profile_url"]],
                       _contact(name, outlets[0]))
    if not db.get_scan(SAMPLE_SCAN_ID):
        params = ScanParams(industries=["crm-sales", "general-saas", "martech", "customer-service"],
                            executives=["Dana Whitfield"])
        db.create_scan(SAMPLE_SCAN_ID, params.model_dump())


async def seed_demo_async() -> None:
    seed_demo()
    scan = db.get_scan(SAMPLE_SCAN_ID)
    if scan and scan["status"] != "done":
        db.update_scan(SAMPLE_SCAN_ID, outlets_done=0, articles_checked=0, articles_matched=0, error=None)
        await run_demo_scan(SAMPLE_SCAN_ID, ScanParams(**scan["params"]), delay=0)


async def run_demo_scan(scan_id: str, params: ScanParams, delay: float | None = None) -> None:
    """Simulate a scan over the demo dataset using the real matcher and threshold logic."""
    delay = STEP_DELAY if delay is None else delay
    try:
        db.prune_scans(MAX_DEMO_SCANS, protect=[SAMPLE_SCAN_ID])
        topic = build_topic(params.industries, params.custom_terms, params.executives, params.match_mode)
        days = params.lookback_days
        since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
        outlets = db.list_outlets(params.regions, params.max_outlets)
        db.update_scan(scan_id, status="running", stage="scanning outlets", outlets_total=len(outlets))

        by_domain: dict[str, list[dict[str, Any]]] = {}
        for art in build_dataset():
            by_domain.setdefault(art["domain"], []).append(art)

        for outlet in outlets:
            if delay:
                await asyncio.sleep(delay)
            checked = matched_count = 0
            for art in by_domain.get(outlet["domain"], []):
                checked += 1
                if since and datetime.fromisoformat(art["published"]) < since:
                    continue
                matched = match_text(topic, art["title"], art["body"])
                if matched and db.insert_article(scan_id, {
                    "url": art["url"], "domain": art["domain"], "title": art["title"],
                    "published": art["published"], "authors": [{"name": art["author"]}], "matched": matched,
                }):
                    matched_count += 1
            db.increment_scan(scan_id, outlets_done=1, articles_checked=checked, articles_matched=matched_count)

        db.update_scan(scan_id, stage="finding contact details")
        if delay:
            await asyncio.sleep(delay * 2)  # contacts were stored at startup; this is for pacing only
        db.update_scan(scan_id, status="done", stage="done", finished_at=db.utcnow())
    except asyncio.CancelledError:
        db.update_scan(scan_id, status="cancelled", stage="cancelled", finished_at=db.utcnow())
        raise
    except Exception as exc:
        log.exception("demo scan %s failed", scan_id)
        db.update_scan(scan_id, status="failed", error=str(exc)[:500], finished_at=db.utcnow())
