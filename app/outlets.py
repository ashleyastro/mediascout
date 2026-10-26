"""Outlet list import. The seed CSV ships with the app; larger lists are imported as CSV."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any

from . import db

SEED_PATH = Path(__file__).parent / "data" / "outlets.csv"
REGIONS = {"NA", "EU", "UK"}


def _clean_domain(value: str) -> str:
    d = value.strip().lower()
    for prefix in ("https://", "http://"):
        d = d.removeprefix(prefix)
    d = d.split("/")[0]
    return d.removeprefix("www.")


def parse_csv(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse outlet rows. Required columns: domain, name, region. Returns (rows, errors)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    rows, errors = [], []
    for i, raw in enumerate(reader, start=2):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        domain = _clean_domain(r.get("domain", ""))
        region = r.get("region", "").upper()
        if not domain or "." not in domain:
            errors.append(f"line {i}: missing or invalid domain")
            continue
        if region not in REGIONS:
            errors.append(f"line {i}: region must be one of NA, EU, UK (got '{region}')")
            continue
        try:
            priority = int(r.get("priority") or r.get("rank") or 1000)
        except ValueError:
            priority = 1000
        rows.append({
            "domain": domain,
            "name": r.get("name") or domain,
            "region": region,
            "country": r.get("country", "").upper() or None,
            "category": r.get("category") or None,
            "priority": priority,
            "feed_url": r.get("feed_url") or None,
            "sitemap_url": r.get("sitemap_url") or None,
            "active": 0 if r.get("active", "1") in {"0", "false", "no"} else 1,
        })
    return rows, errors


def load_seed_if_empty() -> int:
    if sum(db.outlet_counts().values()) > 0:
        return 0
    rows, _ = parse_csv(SEED_PATH.read_text(encoding="utf-8"))
    return db.upsert_outlets(rows)
