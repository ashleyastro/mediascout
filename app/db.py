"""SQLite persistence for outlets, scans, matched articles and journalist contacts."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS outlets (
    domain      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    region      TEXT NOT NULL,          -- NA | EU | UK
    country     TEXT,
    category    TEXT,
    priority    INTEGER DEFAULT 1000,   -- lower = scanned first
    feed_url    TEXT,
    sitemap_url TEXT,
    active      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS scans (
    id               TEXT PRIMARY KEY,
    created_at       TEXT NOT NULL,
    finished_at      TEXT,
    params           TEXT NOT NULL,
    status           TEXT NOT NULL,     -- queued | running | done | failed | cancelled
    stage            TEXT,
    outlets_total    INTEGER DEFAULT 0,
    outlets_done     INTEGER DEFAULT 0,
    articles_checked INTEGER DEFAULT 0,
    articles_matched INTEGER DEFAULT 0,
    error            TEXT
);

CREATE TABLE IF NOT EXISTS articles (
    scan_id   TEXT NOT NULL,
    url       TEXT NOT NULL,
    domain    TEXT NOT NULL,
    title     TEXT,
    published TEXT,
    authors   TEXT NOT NULL,            -- JSON list of {name, url, email, same_as}
    matched   TEXT NOT NULL,            -- JSON list of matched topic labels
    PRIMARY KEY (scan_id, url)
);
CREATE INDEX IF NOT EXISTS idx_articles_scan ON articles(scan_id);

CREATE TABLE IF NOT EXISTS authors (
    key          TEXT PRIMARY KEY,      -- normalized name
    name         TEXT NOT NULL,
    profile_urls TEXT,                  -- JSON list
    contact      TEXT,                  -- JSON object
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS page_cache (
    url        TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    meta       TEXT NOT NULL            -- JSON-serialized ArticleMeta
);

CREATE TABLE IF NOT EXISTS suppressions (
    value      TEXT PRIMARY KEY,        -- lowercased email or normalized name
    created_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_db_path_override: str | None = None


def set_db_path(path: str) -> None:
    """Point the module at a different database file (used by tests)."""
    global _db_path_override
    _db_path_override = path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = _connect(_db_path_override)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------- outlets

def upsert_outlets(rows: list[dict[str, Any]]) -> int:
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO outlets (domain, name, region, country, category, priority, feed_url, sitemap_url, active)
            VALUES (:domain, :name, :region, :country, :category, :priority, :feed_url, :sitemap_url, :active)
            ON CONFLICT(domain) DO UPDATE SET
                name=excluded.name, region=excluded.region, country=excluded.country,
                category=excluded.category, priority=excluded.priority,
                feed_url=COALESCE(excluded.feed_url, outlets.feed_url),
                sitemap_url=COALESCE(excluded.sitemap_url, outlets.sitemap_url),
                active=excluded.active
            """,
            rows,
        )
    return len(rows)


def list_outlets(regions: list[str] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM outlets WHERE active = 1"
    args: list[Any] = []
    if regions:
        sql += f" AND region IN ({','.join('?' * len(regions))})"
        args.extend(regions)
    sql += " ORDER BY priority, domain"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def outlet_counts() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute("SELECT region, COUNT(*) AS n FROM outlets WHERE active = 1 GROUP BY region")
        return {r["region"]: r["n"] for r in rows}


# ---------------------------------------------------------------- scans

def create_scan(scan_id: str, params: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO scans (id, created_at, params, status, stage) VALUES (?, ?, ?, 'queued', 'queued')",
            (scan_id, utcnow(), json.dumps(params)),
        )


def update_scan(scan_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE scans SET {cols} WHERE id = ?", (*fields.values(), scan_id))


def increment_scan(scan_id: str, **deltas: int) -> None:
    cols = ", ".join(f"{k} = {k} + ?" for k in deltas)
    with connect() as conn:
        conn.execute(f"UPDATE scans SET {cols} WHERE id = ?", (*deltas.values(), scan_id))


def get_scan(scan_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
    if not row:
        return None
    scan = dict(row)
    scan["params"] = json.loads(scan["params"])
    return scan


def list_scans(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM scans ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        scan = dict(row)
        scan["params"] = json.loads(scan["params"])
        out.append(scan)
    return out


def prune_scans(keep: int, protect: list[str] | None = None) -> None:
    """Delete all but the newest `keep` scans (and their articles), never touching `protect`."""
    protect = protect or []
    marks = ",".join("?" * len(protect)) or "''"
    with connect() as conn:
        old = [r["id"] for r in conn.execute(
            f"SELECT id FROM scans WHERE id NOT IN ({marks}) ORDER BY created_at DESC LIMIT -1 OFFSET ?",
            (*protect, keep))]
        for scan_id in old:
            conn.execute("DELETE FROM articles WHERE scan_id = ?", (scan_id,))
            conn.execute("DELETE FROM scans WHERE id = ?", (scan_id,))


def mark_interrupted_scans() -> None:
    """Scans run in-process; any left 'running' after a restart did not finish."""
    with connect() as conn:
        conn.execute(
            "UPDATE scans SET status='failed', error='Server restarted before the scan finished', finished_at=? "
            "WHERE status IN ('queued', 'running')",
            (utcnow(),),
        )


# ---------------------------------------------------------------- articles

def insert_article(scan_id: str, article: dict[str, Any]) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO articles (scan_id, url, domain, title, published, authors, matched) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                scan_id,
                article["url"],
                article["domain"],
                article.get("title"),
                article.get("published"),
                json.dumps(article["authors"]),
                json.dumps(article["matched"]),
            ),
        )
        return cur.rowcount > 0


def scan_articles(scan_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM articles WHERE scan_id = ?", (scan_id,)).fetchall()
    out = []
    for row in rows:
        a = dict(row)
        a["authors"] = json.loads(a["authors"])
        a["matched"] = json.loads(a["matched"])
        out.append(a)
    return out


# ---------------------------------------------------------------- authors / contacts

def get_authors(keys: list[str]) -> dict[str, dict[str, Any]]:
    if not keys:
        return {}
    out: dict[str, dict[str, Any]] = {}
    with connect() as conn:
        for i in range(0, len(keys), 500):
            chunk = keys[i : i + 500]
            rows = conn.execute(
                f"SELECT * FROM authors WHERE key IN ({','.join('?' * len(chunk))})", chunk
            ).fetchall()
            for row in rows:
                out[row["key"]] = {
                    "key": row["key"],
                    "name": row["name"],
                    "profile_urls": json.loads(row["profile_urls"] or "[]"),
                    "contact": json.loads(row["contact"] or "{}"),
                    "updated_at": row["updated_at"],
                }
    return out


def save_author(key: str, name: str, profile_urls: list[str], contact: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO authors (key, name, profile_urls, contact, updated_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET name=excluded.name, profile_urls=excluded.profile_urls,
                contact=excluded.contact, updated_at=excluded.updated_at
            """,
            (key, name, json.dumps(profile_urls), json.dumps(contact), utcnow()),
        )


# ---------------------------------------------------------------- page cache

def cache_get(url: str, max_age_days: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT fetched_at, meta FROM page_cache WHERE url = ?", (url,)).fetchone()
    if not row:
        return None
    fetched = datetime.fromisoformat(row["fetched_at"])
    if (datetime.now(timezone.utc) - fetched).days > max_age_days:
        return None
    return json.loads(row["meta"])


def cache_put(url: str, meta: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO page_cache (url, fetched_at, meta) VALUES (?, ?, ?)",
            (url, utcnow(), json.dumps(meta)),
        )


# ---------------------------------------------------------------- suppressions (opt-outs)

def add_suppression(value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO suppressions (value, created_at) VALUES (?, ?)",
            (value.strip().lower(), utcnow()),
        )


def remove_suppression(value: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM suppressions WHERE value = ?", (value.strip().lower(),))


def list_suppressions() -> set[str]:
    with connect() as conn:
        return {r["value"] for r in conn.execute("SELECT value FROM suppressions")}
