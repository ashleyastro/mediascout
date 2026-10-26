"""CSV / JSON / Markdown exports, the AI hand-off prompt, and email delivery."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import smtplib
from email.message import EmailMessage
from typing import Any

from .config import Settings
from .topics import INDUSTRIES

CSV_COLUMNS = [
    "name", "article_count", "outlets", "email", "email_source", "email_confidence", "twitter",
    "linkedin", "bluesky", "threads", "mastodon", "muckrack", "profile_url", "latest_published",
    "topics", "recent_articles",
]


def scan_summary(scan: dict[str, Any]) -> str:
    p = scan["params"]
    topics = []
    if p.get("industries"):
        topics.append(", ".join(str(INDUSTRIES[i]["label"]) for i in p["industries"] if i in INDUSTRIES))
    if p.get("custom_terms"):
        topics.append("keywords: " + ", ".join(p["custom_terms"]))
    if p.get("executives"):
        topics.append("executives: " + ", ".join(p["executives"]))
    joiner = " AND " if p.get("match_mode") == "all" else " OR "
    window = (f"in the last {p['lookback_value']} {p.get('lookback_unit', 'years')}"
              if p.get("lookback_value") else "all time")
    return (f"{joiner.join(topics)} | at least {p.get('min_articles', 5)} stories ({window}) | "
            f"regions: {', '.join(p.get('regions', []))}")


def _row(j: dict[str, Any]) -> dict[str, Any]:
    c = j["contact"]
    return {
        "name": j["name"],
        "article_count": j["article_count"],
        "outlets": "; ".join(j["outlets"]),
        "email": c.get("email", ""),
        "email_source": c.get("email_source", ""),
        "email_confidence": c.get("email_confidence", ""),
        "twitter": c.get("twitter", ""),
        "linkedin": c.get("linkedin", ""),
        "bluesky": c.get("bluesky", ""),
        "threads": c.get("threads", ""),
        "mastodon": c.get("mastodon", ""),
        "muckrack": c.get("muckrack", ""),
        "profile_url": c.get("profile_url", ""),
        "latest_published": (j.get("latest_published") or "")[:10],
        "topics": "; ".join(j["topics"]),
        "recent_articles": " | ".join(f"{a['title']} <{a['url']}>" for a in j["articles"][:5]),
    }


def to_csv(journalists: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for j in journalists:
        writer.writerow(_row(j))
    # Excel needs the BOM to open UTF-8 names (accents, umlauts) correctly.
    return "\ufeff" + buf.getvalue()


def to_json(scan: dict[str, Any], journalists: list[dict[str, Any]]) -> str:
    return json.dumps({
        "scan_id": scan["id"],
        "criteria": scan["params"],
        "summary": scan_summary(scan),
        "generated_at": scan.get("finished_at"),
        "journalists": journalists,
    }, indent=2, ensure_ascii=False)


def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def to_markdown(scan: dict[str, Any], journalists: list[dict[str, Any]], max_articles: int = 3) -> str:
    lines = [
        "# Media list",
        "",
        f"**Criteria:** {scan_summary(scan)}",
        f"**Journalists found:** {len(journalists)}",
        "",
        "| Name | Stories | Outlets | Email | Social | Latest |",
        "|---|---|---|---|---|---|",
    ]
    for j in journalists:
        c = j["contact"]
        social = ", ".join(c[k] for k in ("twitter", "linkedin", "bluesky", "muckrack") if c.get(k))
        lines.append(
            f"| {_md_escape(j['name'])} | {j['article_count']} | {_md_escape(', '.join(j['outlets'][:3]))} | "
            f"{c.get('email', '')} | {social} | {(j.get('latest_published') or '')[:10]} |"
        )
    lines += ["", "## Recent stories by journalist", ""]
    for j in journalists:
        lines.append(f"### {j['name']}")
        for a in j["articles"][:max_articles]:
            lines.append(f"- {(a.get('published') or '')[:10]} [{_md_escape(a['title'])}]({a['url']}) ({a['domain']})")
        lines.append("")
    return "\n".join(lines)


def ai_prompt(scan: dict[str, Any], journalists: list[dict[str, Any]], max_articles: int = 2) -> str:
    """A ready-to-paste prompt for Claude or ChatGPT."""
    return (
        "I'm planning PR outreach. Below is a media list of journalists and bloggers who have recently "
        "covered this topic, with their contact details and recent stories. Please help me prioritize "
        "who to pitch first, explain each person's apparent angle based on their stories, and draft a "
        "short, personalized pitch opener for the top 10.\n\n"
        + to_markdown(scan, journalists, max_articles=max_articles)
    )


def send_email(settings: Settings, to: list[str], scan: dict[str, Any], journalists: list[dict[str, Any]],
               note: str | None = None) -> None:
    if not (settings.smtp_host and settings.smtp_from):
        raise RuntimeError("Email is not configured. Set SMTP_HOST and SMTP_FROM (see .env.example).")
    msg = EmailMessage()
    msg["Subject"] = f"Media list: {len(journalists)} journalists ({scan_summary(scan)[:80]})"
    msg["From"] = settings.smtp_from
    msg["To"] = ", ".join(to)
    body = (note.strip() + "\n\n" if note else "") + to_markdown(scan, journalists, max_articles=2)
    msg.set_content(body)
    msg.add_attachment(to_csv(journalists).encode("utf-8"), maintype="text", subtype="csv",
                       filename=f"media-list-{scan['id']}.csv")
    msg.add_attachment(to_json(scan, journalists).encode("utf-8"), maintype="application", subtype="json",
                       filename=f"media-list-{scan['id']}.json")

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)


async def send_email_async(*args: Any, **kwargs: Any) -> None:
    await asyncio.to_thread(send_email, *args, **kwargs)
