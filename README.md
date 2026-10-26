# MediaScout

Find the journalists and bloggers who keep writing about your SaaS category or a specific public executive, with their contact details, and send the list wherever you need it: CSV, JSON, Markdown, email, Claude or ChatGPT.

## What it does

1. **You choose a topic.** Pick one or more SaaS industries (16 presets such as CRM, HR Tech, Cybersecurity, Data & Analytics), add your own keywords or company names, and/or name public executives. Choose whether a story must match an industry **or** an executive, or **both**.
2. **It scans the outlet list.** For every outlet in the selected regions (North America, EU, UK) it reads the site's XML sitemaps (which often reach back years) and RSS feeds for stories on the topic. It can also use GDELT (free) and NewsAPI.org (API key) to find matching stories across all outlets at once.
3. **It reads each candidate story.** Headline, publish date and bylines come from the page's structured data (JSON-LD), with meta-tag and byline fallbacks. Each story is scored against the topic. Wire-service and "Staff" bylines are dropped.
4. **It keeps the repeat writers.** Bylines are grouped across outlets. Anyone with at least N matching stories ever published (default 5) makes the list. An optional date range limits the count to recent stories.
5. **It finds contact details.** For each journalist it reads their author page on the outlet for a published email, X/Twitter, LinkedIn, Bluesky, Threads, Mastodon and Muck Rack links. If no email is published and a Hunter.io key is set, it asks Hunter for a likely address. Every email is labeled with its source and a confidence level.
6. **You export it.** Download CSV/JSON/Markdown, email it with attachments, open it in Claude or ChatGPT with a ready-made outreach prompt, or connect Claude/ChatGPT to the built-in MCP server so the assistant can run scans and read lists directly.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env            # then edit
.venv/bin/uvicorn app.main:app --env-file .env --reload
```

Open http://localhost:8000. The first start loads the seed outlet list (214 outlets).

Docker:

```bash
docker build -t mediascout .
docker run -p 8000:8000 --env-file .env -v mediascout-data:/data mediascout
```

Tests (fully offline; an end-to-end scan runs against a simulated news site):

```bash
.venv/bin/python -m pytest
```

## Getting to 1,000 outlets

The app ships with a curated seed list of **214** tech, business and trade outlets (111 North America, 73 EU, 30 UK). There is no free, authoritative "top 1,000 media sites" list, so the scan list is designed to be imported. Good sources:

- **Similarweb** or **Semrush** exports of top News & Media / Business / Technology sites by country.
- **Cision, Muck Rack or Meltwater** outlet exports, if you already license one.
- The **Tranco** top-sites list, filtered to news domains.

Import on the **Outlets** tab, or `POST /api/outlets/import`. The CSV needs `domain,name,region` (region is `NA`, `EU` or `UK`), plus optional `country,category,priority,feed_url,sitemap_url`. Lower `priority` is scanned first. Re-importing a domain updates it.

## Demo mode

Set `DEMO_MODE=true` to run a public, risk-free demo. It loads 12 fictional outlets on the reserved `.example` domain, 24 fictional journalists and 4 fictional executives (try **Dana Whitfield**), plus a ready-made sample media list. Scans run the real topic matcher and five-story threshold over that dataset instead of crawling. Email, outlet import and opt-out edits are turned off, and no social profiles are generated, so nothing can reach or describe a real person.

Deploy it free on Render: push this repo to GitHub, then in Render choose **New → Blueprint**, pick the repo, and approve. `render.yaml` sets everything up. The free plan sleeps when idle, so the first visit takes about 30 seconds to wake.

## Connecting Claude and ChatGPT

The app runs an MCP server at `{PUBLIC_BASE_URL}/mcp` (or `/mcp/<MCP_SECRET>` when the secret is set). The **Connect** tab shows the exact URL.

- **Claude:** Settings → Connectors → Add custom connector → paste the URL.
- **ChatGPT:** Settings → Apps & Connectors → Advanced → enable Developer mode → Create → paste the URL.

Tools exposed: `list_saas_industries`, `start_journalist_scan`, `get_scan_status`, `list_recent_scans`, `get_journalists`, plus `search` and `fetch` for ChatGPT's connector conventions.

Both products require the server to be reachable over public HTTPS. Neither supports a static API key for custom connectors, so set `MCP_SECRET` to a long random value to keep the URL private. Adding OAuth is the next step for multi-user deployments.

The **Open in Claude / Open in ChatGPT** buttons need no setup. They open a new chat pre-filled with the media list and an outreach prompt. Lists too long for a URL are copied to the clipboard for pasting instead.

## Configuration

All settings are environment variables; see `.env.example`. The important ones:

| Variable | Purpose |
|---|---|
| `APP_PASSWORD` | Password-protects the UI and API. **Set this before deploying.** Without it, anyone who finds the URL can run scans and send email. |
| `MCP_SECRET`, `ALLOWED_HOSTS` | Protect the MCP endpoint. |
| `SMTP_HOST`, `SMTP_FROM`, `SMTP_USER`, `SMTP_PASSWORD` | Enable email export (any SMTP provider: SendGrid, Postmark, SES, Google Workspace). |
| `HUNTER_API_KEY` | Email lookup when the author page publishes none. |
| `NEWSAPI_KEY` | Extra discovery, and bylines for sites without structured data. The free plan only covers the last month and is for development use. |
| `RESPECT_ROBOTS`, `PER_HOST_DELAY_MS` | Crawl politeness. Leave robots on. |

## Limitations to know about

- **Paywalled and bot-protected sites.** Some large outlets (for example WSJ, FT, Bloomberg) block crawlers in robots.txt or with bot protection. Those outlets will return few or no results. NewsAPI or a licensed media database is the practical route for them.
- **Discovery is keyword-gated.** To keep a 1,000-site scan to hours rather than days, the app only fetches stories whose sitemap title or URL slug mentions a topic term (or an executive's surname). Stories with unrelated slugs can be missed. Raise `MAX_CANDIDATES_PER_OUTLET` and add specific company names as keywords to widen coverage.
- **"All time" means as far back as each source reaches.** Sitemaps usually cover years, GDELT about 3 months, and NewsAPI depends on your plan. Per outlet, the scan reads up to `MAX_CHILD_SITEMAPS` sitemap files (newest first) and checks up to `MAX_CANDIDATES_PER_OUTLET` stories, so for prolific outlets the oldest stories may not be counted. Raise both for deeper history at the cost of longer scans.
- **Name collisions.** Journalists are grouped by normalized name across outlets, so two different people with the same name would be merged.
- **Contact coverage.** Most outlets publish social links on author pages; fewer publish email addresses. Hunter results are educated guesses and are labeled as such.
- **Scale.** Scans run inside the web process and are marked failed if the server restarts. For scheduled or very large scans, move `run_scan` onto a job queue (RQ, Celery, or Arq) and SQLite onto Postgres.

## Compliance

Journalists' professional contact details are commonly processed for media relations under the GDPR "legitimate interests" basis, but that comes with obligations: tell people where you got their data when you first contact them, honor objections promptly, and keep only what you need. The **Opt-outs** list (Outlets tab, or `/api/suppressions`) removes a person from all results and exports by email or name. Respect each site's terms of use, and confirm your approach with counsel before using lists for outreach in the EU or UK.

## Project layout

```
app/
  main.py         FastAPI app: UI, REST API, MCP mount, optional password gate
  scanner.py      Scan orchestration (outlets -> candidates -> articles -> journalists)
  discovery.py    Sitemaps, RSS, GDELT, NewsAPI
  fetcher.py      Polite HTTP client (robots.txt, per-host limits, retries)
  extract.py      Article metadata, bylines and contact extraction
  topics.py       SaaS industry taxonomy and matching rules
  enrich.py       Author-page and Hunter.io contact enrichment
  results.py      Byline grouping and the final journalist list
  exporters.py    CSV / JSON / Markdown / AI prompt / email
  mcp_server.py   MCP tools for Claude and ChatGPT
  db.py           SQLite storage
  data/outlets.csv  Seed outlet list
  static/         Web UI
tests/            Offline unit and end-to-end tests
```

## License

Apache License 2.0. See `LICENSE`.
