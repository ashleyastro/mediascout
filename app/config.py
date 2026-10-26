"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    db_path: str
    user_agent: str
    respect_robots: bool
    max_concurrency: int
    per_host_concurrency: int
    per_host_delay_ms: int
    request_timeout: float
    outlet_concurrency: int
    max_candidates_per_outlet: int
    max_child_sitemaps: int
    page_cache_days: int

    # Optional discovery / enrichment providers
    newsapi_key: str | None
    gdelt_enabled: bool
    hunter_api_key: str | None

    # Email export
    smtp_host: str | None
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    smtp_from: str | None
    smtp_starttls: bool

    # Access control
    app_password: str | None
    mcp_secret: str | None
    allowed_hosts: list[str]

    public_base_url: str
    demo_mode: bool

    @property
    def mcp_path(self) -> str:
        return f"/mcp/{self.mcp_secret}" if self.mcp_secret else "/mcp"


def load_settings() -> Settings:
    env = os.environ.get
    return Settings(
        db_path=env("MEDIASCOUT_DB", "mediascout.db"),
        user_agent=env(
            "MEDIASCOUT_USER_AGENT",
            "MediaScoutBot/1.0 (+https://example.com/bot; media research crawler)",
        ),
        respect_robots=_bool(env("RESPECT_ROBOTS"), True),
        max_concurrency=_int(env("MAX_CONCURRENCY"), 32),
        per_host_concurrency=_int(env("PER_HOST_CONCURRENCY"), 2),
        per_host_delay_ms=_int(env("PER_HOST_DELAY_MS"), 500),
        request_timeout=float(env("REQUEST_TIMEOUT", "20")),
        outlet_concurrency=_int(env("OUTLET_CONCURRENCY"), 12),
        max_candidates_per_outlet=_int(env("MAX_CANDIDATES_PER_OUTLET"), 300),
        max_child_sitemaps=_int(env("MAX_CHILD_SITEMAPS"), 120),
        page_cache_days=_int(env("PAGE_CACHE_DAYS"), 30),
        newsapi_key=env("NEWSAPI_KEY") or None,
        gdelt_enabled=_bool(env("GDELT_ENABLED"), True),
        hunter_api_key=env("HUNTER_API_KEY") or None,
        smtp_host=env("SMTP_HOST") or None,
        smtp_port=_int(env("SMTP_PORT"), 587),
        smtp_user=env("SMTP_USER") or None,
        smtp_password=env("SMTP_PASSWORD") or None,
        smtp_from=env("SMTP_FROM") or None,
        smtp_starttls=_bool(env("SMTP_STARTTLS"), True),
        app_password=env("APP_PASSWORD") or None,
        mcp_secret=env("MCP_SECRET") or None,
        allowed_hosts=[h.strip() for h in env("ALLOWED_HOSTS", "").split(",") if h.strip()],
        public_base_url=env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        demo_mode=_bool(env("DEMO_MODE"), False),
    )


settings = load_settings()
