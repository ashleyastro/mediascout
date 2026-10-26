"""MCP server so Claude (custom connectors) and ChatGPT (developer-mode connectors) can use results.

Both products connect to a remote MCP server over Streamable HTTP. Point them at
    {PUBLIC_BASE_URL}{settings.mcp_path}
Set MCP_SECRET to put the endpoint behind an unguessable path.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from . import db, scanner
from .exporters import scan_summary, to_markdown
from .models import ScanParams
from .results import journalists_for_scan
from .topics import INDUSTRIES

mcp = MCPServer(
    name="MediaScout",
    instructions=(
        "MediaScout finds journalists and bloggers who repeatedly cover a SaaS industry or a public "
        "executive, with contact details. Use list_recent_scans or get_journalists to read existing "
        "media lists. To build a new one, call start_journalist_scan, then poll get_scan_status until "
        "status is 'done' (large scans take a while), then call get_journalists."
    ),
)


def _scan_brief(scan: dict[str, Any]) -> dict[str, Any]:
    return {
        "scan_id": scan["id"],
        "status": scan["status"],
        "stage": scan.get("stage"),
        "criteria": scan_summary(scan),
        "created_at": scan["created_at"],
        "progress": f"{scan['outlets_done']}/{scan['outlets_total']} outlets, "
                    f"{scan['articles_matched']} matching stories of {scan['articles_checked']} checked",
        "error": scan.get("error"),
    }


@mcp.tool()
def list_saas_industries() -> list[dict[str, Any]]:
    """List the preset SaaS industry ids and the keywords each one matches."""
    return [{"id": k, "label": v["label"], "terms": v["terms"]} for k, v in INDUSTRIES.items()]


@mcp.tool()
async def start_journalist_scan(
    industries: list[str] | None = None,
    executives: list[str] | None = None,
    custom_terms: list[str] | None = None,
    match_mode: str = "any",
    lookback_value: int | None = None,
    lookback_unit: str = "years",
    min_articles: int = 5,
    regions: list[str] | None = None,
    max_outlets: int = 1000,
) -> dict[str, Any]:
    """Start a scan of the outlet list for journalists covering the given topics.

    industries: preset ids from list_saas_industries. executives: full names of public executives.
    match_mode: 'any' (industry OR executive) or 'all' (both). regions: any of NA, EU, UK.
    min_articles: matching stories a journalist needs, counted across all time unless
    lookback_value/lookback_unit set an optional date limit.
    Returns a scan_id; poll get_scan_status, then call get_journalists.
    """
    params = ScanParams(
        industries=industries or [],
        executives=executives or [],
        custom_terms=custom_terms or [],
        match_mode=match_mode,  # type: ignore[arg-type]
        lookback_value=lookback_value,
        lookback_unit=lookback_unit,  # type: ignore[arg-type]
        min_articles=min_articles,
        regions=regions or ["NA", "EU", "UK"],  # type: ignore[arg-type]
        max_outlets=max_outlets,
    )
    scan_id = scanner.start_scan(params)
    return {"scan_id": scan_id, "status": "queued"}


@mcp.tool()
def get_scan_status(scan_id: str) -> dict[str, Any]:
    """Check progress of a scan."""
    scan = db.get_scan(scan_id)
    if not scan:
        return {"error": f"No scan with id {scan_id}"}
    return _scan_brief(scan)


@mcp.tool()
def list_recent_scans(limit: int = 10) -> list[dict[str, Any]]:
    """List recent scans (newest first) with their criteria and status."""
    return [_scan_brief(s) for s in db.list_scans(min(max(limit, 1), 50))]


@mcp.tool()
def get_journalists(scan_id: str, limit: int = 50, offset: int = 0, as_markdown: bool = False) -> Any:
    """Return the media list for a scan: name, story count, outlets, contacts and recent stories.

    Set as_markdown=true for a compact table that is easier to read in chat.
    """
    scan = db.get_scan(scan_id)
    if not scan:
        return {"error": f"No scan with id {scan_id}"}
    rows = journalists_for_scan(scan_id)
    page = rows[offset : offset + min(max(limit, 1), 200)]
    if as_markdown:
        return to_markdown(scan, page)
    for r in page:
        r["articles"] = r["articles"][:5]
    return {
        "scan": _scan_brief(scan),
        "total": len(rows),
        "offset": offset,
        "journalists": page,
    }


@mcp.tool()
def search(query: str) -> dict[str, Any]:
    """Search journalists across completed scans by name, outlet or topic (ChatGPT connector search)."""
    q = query.lower().strip()
    results = []
    for scan in db.list_scans(20):
        if scan["status"] != "done":
            continue
        for j in journalists_for_scan(scan["id"]):
            haystack = " ".join([j["name"], *j["outlets"], *j["topics"]]).lower()
            if q and q in haystack:
                results.append({
                    "id": f"{scan['id']}::{j['name']}",
                    "title": f"{j['name']} ({', '.join(j['outlets'][:2])})",
                    "url": j["contact"].get("profile_url") or j["articles"][0]["url"],
                })
        if len(results) >= 50:
            break
    return {"results": results[:50]}


@mcp.tool()
def fetch(id: str) -> dict[str, Any]:
    """Fetch one journalist record by the id returned from search."""
    scan_id, _, name = id.partition("::")
    for j in journalists_for_scan(scan_id):
        if j["name"] == name:
            lines = [f"{j['name']}: {j['article_count']} stories; outlets: {', '.join(j['outlets'])}",
                     f"Contact: {j['contact']}", "Recent stories:"]
            lines += [f"- {a['title']} ({a['url']})" for a in j["articles"][:10]]
            return {"id": id, "title": j["name"], "text": "\n".join(lines),
                    "url": j["contact"].get("profile_url") or j["articles"][0]["url"], "metadata": j["contact"]}
    return {"id": id, "title": "Not found", "text": "No journalist with that id.", "url": ""}
