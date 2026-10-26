from __future__ import annotations

import csv
import io
import json
from dataclasses import replace

import pytest

from app import db, exporters
from app.config import settings
from app.extract import clean_person_name, extract_article, extract_contacts
from app.models import ScanParams
from app.outlets import load_seed_if_empty, parse_csv
from app.results import journalists_for_scan
from app.scanner import run_scan
from app.topics import build_topic, match_text, prefilter_hit, search_queries

from . import fakesite


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    db.set_db_path(str(tmp_path / "test.db"))
    db.init_db()
    yield


TEST_SETTINGS = replace(settings, per_host_delay_ms=0, gdelt_enabled=False, newsapi_key=None,
                        hunter_api_key=None)


# ---------------------------------------------------------------- topic matching

def test_case_sensitivity_rule():
    topic = build_topic(industries=["crm-sales"])
    assert match_text(topic, "Salesforce posts record quarter", "")
    # Lowercase "salesforce" should not match a brand term with capitals; one body hit is below threshold.
    assert not match_text(topic, "The salesforce of the future", "")
    assert not match_text(topic, "Markets today", "A single CRM mention.")
    assert match_text(topic, "Markets today", "CRM spending rose. HubSpot gained.")


def test_match_mode_all_requires_both():
    topic = build_topic(industries=["crm-sales"], executives=["Marc Benioff"], mode="all")
    assert not match_text(topic, "Salesforce CRM growth", "")
    assert match_text(topic, "Salesforce CRM growth", "Marc Benioff said demand was strong.")
    any_topic = build_topic(executives=["Marc Benioff"], mode="any")
    assert match_text(any_topic, "Marc Benioff on AI", "")


def test_prefilter_uses_slug_and_surname():
    topic = build_topic(industries=["cybersecurity"], executives=["Marc Benioff"])
    assert prefilter_hit(topic, "https://x.com/2026/05/zero-trust-adoption-grows")
    assert prefilter_hit(topic, "https://x.com/news/benioff-says-ai-agents")
    assert not prefilter_hit(topic, "https://x.com/news/football-results")
    assert prefilter_hit(topic, "https://x.com/p/12345", title="CrowdStrike outage report")


def test_search_queries_are_valid_gdelt_syntax():
    queries = search_queries(build_topic(industries=["crm-sales"], custom_terms=["Gong"], executives=["Jane Roe"]))
    assert '"Jane Roe"' in queries
    assert '"Gong"' in queries  # single term: no parentheses
    assert any(q.startswith("(") and " OR " in q for q in queries)


# ---------------------------------------------------------------- extraction

def test_extract_article_jsonld_graph():
    html = fakesite.article_html("crm-x", "CRM headline", "Salesforce body", "jane", 3)
    meta = extract_article(html, fakesite.BASE + "/news/crm-x")
    assert meta.title == "CRM headline"
    assert meta.published and meta.published_dt
    assert [a.name for a in meta.authors] == ["Jane Doe"]
    assert meta.authors[0].url == fakesite.BASE + "/author/jane-doe"
    assert "advertising@" not in meta.text  # footer removed


def test_extract_article_meta_fallback_and_multi_byline():
    html = """<html><head><meta property="og:title" content="Big SaaS deal">
    <meta property="article:published_time" content="2026-09-01T10:00:00Z">
    <meta name="author" content="By Ana García and Luc Martin"></head><body><p>SaaS</p></body></html>"""
    meta = extract_article(html, "https://e.com/a")
    assert meta.title == "Big SaaS deal"
    assert meta.published.startswith("2026-09-01")
    assert [a.name for a in meta.authors] == ["Ana García", "Luc Martin"]


@pytest.mark.parametrize("raw,expected", [
    ("By Jane Doe", "Jane Doe"),
    ("Jane Doe | Senior Reporter", "Jane Doe"),
    ("Jane Doe, Senior Editor", "Jane Doe"),
    ("Reuters", None),
    ("Staff Writer", None),
    ("TechCrunch Staff", None),
    ("jane doe", "Jane Doe"),
    ("Madonna", None),
    ("editor@site.com", None),
])
def test_clean_person_name(raw, expected):
    assert clean_person_name(raw) == expected


def test_extract_contacts_prefers_personal_links():
    c = extract_contacts(fakesite.author_html("jane"), fakesite.BASE + "/author/jane-doe", "Jane Doe", fakesite.DOMAIN)
    assert c["email"] == f"jane.doe@{fakesite.DOMAIN}"
    assert c["email_confidence"] == "high"
    assert c["twitter"] == "https://x.com/janedoe"
    assert c["linkedin"].startswith("https://www.linkedin.com/in/jane-doe-reporter")


# ---------------------------------------------------------------- outlets CSV

def test_parse_outlet_csv_validates_rows():
    rows, errors = parse_csv("domain,name,region\nhttps://www.Example.com/news,Example,na\nbad,Bad,NA\nx.com,X,APAC\n")
    assert [r["domain"] for r in rows] == ["example.com"]
    assert rows[0]["region"] == "NA"
    assert len(errors) == 2


def test_seed_outlets_load():
    assert load_seed_if_empty() > 150
    counts = db.outlet_counts()
    assert set(counts) == {"NA", "EU", "UK"}


# ---------------------------------------------------------------- end to end

async def _run_fake_scan(**overrides) -> tuple[str, fakesite.FakeSite]:
    db.upsert_outlets([{
        "domain": fakesite.DOMAIN, "name": "Fake News", "region": "NA", "country": "US", "category": "tech",
        "priority": 1, "feed_url": None, "sitemap_url": None, "active": 1,
    }])
    params = ScanParams(**{"industries": ["crm-sales"], "regions": ["NA"], **overrides})
    site = fakesite.FakeSite()
    scan_id = "testscan"
    db.create_scan(scan_id, params.model_dump())
    await run_scan(scan_id, params, TEST_SETTINGS, transport=site.transport())
    return scan_id, site


def test_defaults_are_five_articles_all_time():
    params = ScanParams(industries=["crm-sales"])
    assert params.min_articles == 5
    assert params.lookback_days is None


@pytest.mark.asyncio
async def test_end_to_end_scan_all_time():
    scan_id, site = await _run_fake_scan()
    scan = db.get_scan(scan_id)
    assert scan["status"] == "done", scan["error"]
    assert scan["outlets_done"] == 1

    rows = journalists_for_scan(scan_id)
    # Jane has 5 CRM stories ever, including 2019; Bob only 2; "Staff Writer" is dropped.
    assert [r["name"] for r in rows] == ["Jane Doe"]
    jane = rows[0]
    assert jane["article_count"] == 5
    assert any("crm-2019-retrospective" in a["url"] for a in jane["articles"])
    assert jane["contact"]["email"] == f"jane.doe@{fakesite.DOMAIN}"
    assert jane["contact"]["twitter"] == "https://x.com/janedoe"
    # Off-topic URLs and tag sitemaps are never fetched.
    assert not any("crypto-market-today" in u for u in site.requests)
    assert not any("sitemap-tags" in u for u in site.requests)


@pytest.mark.asyncio
async def test_optional_date_limit():
    scan_id, site = await _run_fake_scan(min_articles=3, lookback_value=5, lookback_unit="months")
    rows = journalists_for_scan(scan_id)
    assert [(r["name"], r["article_count"]) for r in rows] == [("Jane Doe", 3)]
    assert not any("crm-old-story" in a["url"] for a in rows[0]["articles"])
    # Sitemaps older than the cutoff are skipped without being fetched.
    assert not any("2019" in u for u in site.requests)


@pytest.mark.asyncio
async def test_lower_threshold():
    scan_id, _ = await _run_fake_scan(min_articles=2)
    rows = {r["name"]: r for r in journalists_for_scan(scan_id)}
    assert rows["Jane Doe"]["article_count"] == 5
    assert rows["Bob Smith"]["article_count"] == 2


@pytest.mark.asyncio
async def test_suppression_and_exports():
    scan_id, _ = await _run_fake_scan(min_articles=2)
    scan = db.get_scan(scan_id)

    rows = journalists_for_scan(scan_id)
    text = exporters.to_csv(rows)
    parsed = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    assert {r["name"] for r in parsed} == {"Jane Doe", "Bob Smith"}
    assert parsed[0]["email"].startswith("jane.doe@")

    payload = json.loads(exporters.to_json(scan, rows))
    assert payload["criteria"]["min_articles"] == 2
    md = exporters.to_markdown(scan, rows)
    assert "| Jane Doe | 5 |" in md
    assert "all time" in md
    assert "media list" in exporters.ai_prompt(scan, rows).lower()

    db.add_suppression(f"jane.doe@{fakesite.DOMAIN}")
    db.add_suppression("bob smith")
    assert journalists_for_scan(scan_id) == []


def test_email_requires_configuration():
    scan = {"id": "x", "params": ScanParams(industries=["crm-sales"]).model_dump()}
    with pytest.raises(RuntimeError, match="not configured"):
        exporters.send_email(TEST_SETTINGS, ["a@b.com"], scan, [])


def test_email_builds_message(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["host"] = host

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = u

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr(exporters.smtplib, "SMTP", FakeSMTP)
    cfg = replace(TEST_SETTINGS, smtp_host="smtp.test", smtp_from="bot@test.com", smtp_user="u", smtp_password="p")
    scan = {"id": "abc", "params": ScanParams(industries=["crm-sales"]).model_dump()}
    exporters.send_email(cfg, ["pr@client.com"], scan, [], note="Hi team")
    msg = sent["msg"]
    assert msg["To"] == "pr@client.com"
    names = [p.get_filename() for p in msg.iter_attachments()]
    assert names == ["media-list-abc.csv", "media-list-abc.json"]
    assert sent["tls"] and sent["login"] == "u"
