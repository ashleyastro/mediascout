"""Pull article metadata (headline, date, bylines) and author contact details out of HTML."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

ARTICLE_TYPES = {
    "article", "newsarticle", "blogposting", "reportagenewsarticle", "analysisnewsarticle",
    "opinionnewsarticle", "backgroundnewsarticle", "techarticle", "reviewnewsarticle", "liveblogposting",
}

# Bylines that name an organization or a role rather than a person.
NON_PERSON = {
    "staff", "staff writer", "staff reporter", "editor", "editors", "editorial", "editorial team",
    "editorial staff", "the editors", "newsroom", "news desk", "web desk", "admin", "administrator",
    "guest", "guest author", "guest contributor", "contributor", "contributors", "sponsored",
    "sponsored content", "press release", "partner content", "brand studio", "reuters", "associated press",
    "the associated press", "ap", "afp", "bloomberg", "bloomberg news", "dow jones", "pr newswire",
    "business wire", "globenewswire", "team", "unknown", "anonymous", "wire staff",
}
NON_PERSON_WORDS = {"staff", "team", "desk", "newsroom", "editorial", "press", "media", "news", "wire",
                    "inc", "llc", "ltd", "magazine", "group", "studio", "partner", "sponsored"}

MAX_TEXT = 12000


@dataclass
class Author:
    name: str
    url: str | None = None
    email: str | None = None
    same_as: list[str] = field(default_factory=list)


@dataclass
class ArticleMeta:
    url: str
    title: str = ""
    description: str = ""
    published: str | None = None   # ISO 8601, UTC
    authors: list[Author] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    text: str = ""
    is_article: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ArticleMeta":
        d = dict(d)
        d["authors"] = [Author(**a) for a in d.get("authors", [])]
        return cls(**d)

    @property
    def published_dt(self) -> datetime | None:
        return parse_date(self.published) if self.published else None


# ---------------------------------------------------------------- helpers

def parse_date(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = dateparser.parse(value.strip())
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def normalize_name(name: str) -> str:
    """Stable key for grouping bylines: lowercase, no accents-insensitive punctuation noise."""
    name = re.sub(r"[^\w\s'-]", " ", name.lower())
    return " ".join(name.split())


def clean_person_name(raw: str, outlet_name: str | None = None) -> str | None:
    name = " ".join(raw.replace(" ", " ").split())
    name = re.sub(r"^(by|par|von|di|por|door|av)\s+", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*[|(].*$", "", name)          # "Jane Doe | Senior Reporter", "Jane Doe (Reuters)"
    name = re.sub(r",\s*(senior|staff|contributing|chief|deputy|executive|editor|reporter|writer|correspondent).*$",
                  "", name, flags=re.IGNORECASE)
    name = name.strip(" -–—,:;")
    if not name or "@" in name or "http" in name.lower() or any(ch.isdigit() for ch in name):
        return None
    low = name.lower()
    if low in NON_PERSON or (outlet_name and low == outlet_name.lower()):
        return None
    words = name.split()
    if len(words) < 2 or len(words) > 5:
        return None
    if any(w.lower().strip(".") in NON_PERSON_WORDS for w in words):
        return None
    if not any(c.isupper() for c in name):
        name = name.title()
    return name


def split_byline(raw: str) -> list[str]:
    raw = re.sub(r"^\s*(by|par|von|por)\s+", "", raw, flags=re.IGNORECASE)
    parts = re.split(r"\s*(?:,|&|\band\b|\bet\b|\bund\b|\by\b|\be\b)\s*", raw)
    return [p for p in parts if p.strip()]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _types(node: dict[str, Any]) -> set[str]:
    return {str(t).lower() for t in _as_list(node.get("@type"))}


def _jsonld_nodes(soup: BeautifulSoup) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, dict):
            nodes.append(obj)
            if "@graph" in obj:
                walk(obj["@graph"])
            if isinstance(obj.get("mainEntity"), (dict, list)):
                walk(obj["mainEntity"])

    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            walk(json.loads(raw, strict=False))
        except json.JSONDecodeError:
            # Some CMSs emit trailing commas or concatenated objects; skip rather than guess.
            continue
    return nodes


def _meta(soup: BeautifulSoup, *names: str) -> list[str]:
    values = []
    for name in names:
        for tag in soup.find_all("meta", attrs={"property": name}) + soup.find_all("meta", attrs={"name": name}):
            content = (tag.get("content") or "").strip()
            if content:
                values.append(content)
    return values


def _authors_from_jsonld(value: Any, by_id: dict[str, dict[str, Any]], base_url: str) -> list[Author]:
    out = []
    for item in _as_list(value):
        if isinstance(item, str):
            out.extend(Author(name=n) for n in split_byline(item))
            continue
        if not isinstance(item, dict):
            continue
        if "@id" in item and "name" not in item and item["@id"] in by_id:
            item = by_id[item["@id"]]
        types = _types(item)
        if types and "person" not in types:
            continue  # Organization bylines
        name = item.get("name")
        if isinstance(name, list):
            name = name[0] if name else None
        if not isinstance(name, str):
            continue
        url = item.get("url") if isinstance(item.get("url"), str) else None
        email = item.get("email") if isinstance(item.get("email"), str) else None
        same_as = [s for s in _as_list(item.get("sameAs")) if isinstance(s, str)]
        out.append(Author(
            name=name,
            url=urljoin(base_url, url) if url else None,
            email=email.removeprefix("mailto:") if email else None,
            same_as=same_as,
        ))
    return out


def _visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg"]):
        tag.decompose()
    container = soup.find("article") or soup.find("main") or soup.body or soup
    text = container.get_text(" ", strip=True)
    return " ".join(text.split())[:MAX_TEXT]


# ---------------------------------------------------------------- article extraction

def extract_article(html: str, url: str, outlet_name: str | None = None) -> ArticleMeta:
    soup = BeautifulSoup(html, "lxml")
    meta = ArticleMeta(url=url)
    nodes = _jsonld_nodes(soup)
    by_id = {n["@id"]: n for n in nodes if isinstance(n.get("@id"), str)}

    article_node = next((n for n in nodes if _types(n) & ARTICLE_TYPES), None)
    authors: list[Author] = []
    published: datetime | None = None

    if article_node:
        meta.is_article = True
        headline = article_node.get("headline") or article_node.get("name")
        if isinstance(headline, str):
            meta.title = headline.strip()
        desc = article_node.get("description")
        if isinstance(desc, str):
            meta.description = desc.strip()
        published = parse_date(article_node.get("datePublished")) or parse_date(article_node.get("dateCreated"))
        authors = _authors_from_jsonld(article_node.get("author"), by_id, url)
        kw = article_node.get("keywords")
        if isinstance(kw, str):
            meta.keywords = [k.strip() for k in kw.split(",") if k.strip()]
        elif isinstance(kw, list):
            meta.keywords = [str(k).strip() for k in kw if str(k).strip()]

    # Open Graph / meta-tag fallbacks
    if not meta.title:
        meta.title = next(iter(_meta(soup, "og:title", "twitter:title")), "") or (
            soup.title.get_text(strip=True) if soup.title else "")
    if not meta.description:
        meta.description = next(iter(_meta(soup, "og:description", "description")), "")
    if not published:
        for value in _meta(soup, "article:published_time", "og:published_time", "datePublished",
                           "pubdate", "publish-date", "parsely-pub-date", "sailthru.date", "dc.date"):
            published = parse_date(value)
            if published:
                break
    if not published:
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag:
            published = parse_date(time_tag["datetime"])
    if _meta(soup, "og:type") and _meta(soup, "og:type")[0].lower() == "article":
        meta.is_article = True
    if not meta.keywords:
        meta.keywords = [k.strip() for v in _meta(soup, "news_keywords", "keywords", "article:tag")
                         for k in v.split(",") if k.strip()]

    if not authors:
        profile_links = [a for a in soup.find_all("a", rel=True) if "author" in a.get("rel", [])]
        for a in profile_links:
            authors.append(Author(name=a.get_text(" ", strip=True), url=urljoin(url, a.get("href", ""))))
    if not authors:
        for value in _meta(soup, "author", "parsely-author", "sailthru.author", "byl", "dc.creator"):
            authors.extend(Author(name=n) for n in split_byline(value))
        for value in _meta(soup, "article:author"):
            if value.startswith("http"):
                continue  # usually a Facebook profile URL, not a name
            authors.extend(Author(name=n) for n in split_byline(value))
    if not authors:
        byline = soup.find(attrs={"class": re.compile(r"\bbyline\b|\bauthor-name\b|\barticle-author\b", re.I)})
        if byline:
            link = byline.find("a", href=True)
            text = byline.get_text(" ", strip=True)
            for n in split_byline(text):
                authors.append(Author(name=n, url=urljoin(url, link["href"]) if link else None))

    # Clean and de-duplicate bylines
    seen: set[str] = set()
    for author in authors:
        name = clean_person_name(author.name, outlet_name)
        if not name:
            continue
        key = normalize_name(name)
        if key in seen:
            continue
        seen.add(key)
        author.name = name
        meta.authors.append(author)

    meta.published = _iso(published)
    meta.text = _visible_text(soup)
    return meta


# ---------------------------------------------------------------- contact extraction

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
BAD_EMAIL_PARTS = ("example.", "sentry", "wixpress", "noreply", "no-reply", "donotreply", ".png", ".jpg",
                   ".gif", ".webp", ".svg", "privacy@", "abuse@", "webmaster@", "subscriptions@",
                   "advertising@", "ads@", "support@", "help@", "careers@", "jobs@")

SOCIAL_HOSTS = {
    "twitter": ("twitter.com", "x.com"),
    "linkedin": ("linkedin.com",),
    "muckrack": ("muckrack.com",),
    "bluesky": ("bsky.app",),
    "threads": ("threads.net", "threads.com"),
    "mastodon": ("mastodon.social", "journa.host", "mastodon.online"),
}
SKIP_TWITTER_PATHS = {"share", "intent", "home", "hashtag", "search", "i"}


def _plausible_email(email: str) -> bool:
    low = email.lower()
    return not any(part in low for part in BAD_EMAIL_PARTS)


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def classify_link(url: str) -> str | None:
    host = _host(url)
    for kind, hosts in SOCIAL_HOSTS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            path = urlparse(url).path.strip("/").split("/")
            if kind == "twitter" and (not path[0] or path[0].lower() in SKIP_TWITTER_PATHS):
                return None
            if kind == "linkedin" and (not path or path[0] not in {"in", "pub"}):
                return None  # company pages, share links
            return kind
    return None


def _name_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z]+", name.lower()) if len(t) >= 3}


def _outlet_token(domain: str) -> str:
    parts = domain.lower().split(".")
    return parts[-2] if len(parts) >= 2 else parts[0]


def pick_best(urls: list[str], author_name: str, outlet_domain: str) -> str | None:
    """Prefer the link whose handle resembles the author's name over the outlet's own account."""
    if not urls:
        return None
    tokens = _name_tokens(author_name)
    outlet = _outlet_token(outlet_domain)
    personal = [u for u in urls if outlet not in u.lower()]
    for u in personal:
        handle = urlparse(u).path.lower()
        if any(t in handle for t in tokens):
            return u
    return personal[0] if personal else None


def extract_contacts(html: str, page_url: str, author_name: str, outlet_domain: str) -> dict[str, Any]:
    """Parse an author profile page into contact fields. Every field is optional."""
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, list[str]] = {k: [] for k in SOCIAL_HOSTS}
    emails: list[str] = []

    # JSON-LD Person / ProfilePage data is the most reliable source when present.
    tokens = _name_tokens(author_name)
    for node in _jsonld_nodes(soup):
        if "person" not in _types(node):
            continue
        if tokens and not (tokens & _name_tokens(str(node.get("name", "")))):
            continue
        email = node.get("email")
        if isinstance(email, str):
            emails.append(email.removeprefix("mailto:"))
        for link in _as_list(node.get("sameAs")):
            if isinstance(link, str) and (kind := classify_link(link)):
                found[kind].append(link)

    for tag in soup(["header", "footer", "nav", "script", "style"]):
        tag.decompose()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if EMAIL_RE.fullmatch(addr):
                emails.append(addr)
            continue
        absolute = urljoin(page_url, href)
        if kind := classify_link(absolute):
            found[kind].append(absolute.split("?")[0].rstrip("/"))

    emails.extend(EMAIL_RE.findall(soup.get_text(" ")))
    emails = [e for e in dict.fromkeys(e.lower() for e in emails) if _plausible_email(e)]

    # Rank emails: name-like local part on the outlet's domain first.
    def email_rank(e: str) -> tuple[int, int]:
        local, _, domain = e.partition("@")
        return (0 if any(t in local for t in tokens) else 1,
                0 if _outlet_token(outlet_domain) in domain else 1)

    emails.sort(key=email_rank)

    contact: dict[str, Any] = {"profile_url": page_url}
    if emails:
        contact["email"] = emails[0]
        contact["email_source"] = "author page"
        contact["email_confidence"] = "high" if email_rank(emails[0])[0] == 0 else "medium"
    for kind, urls in found.items():
        best = pick_best(list(dict.fromkeys(urls)), author_name, outlet_domain)
        if best:
            contact[kind] = best
    return contact


def merge_contacts(*contacts: dict[str, Any]) -> dict[str, Any]:
    """Combine contact dicts; earlier arguments win for each field."""
    merged: dict[str, Any] = {}
    for c in contacts:
        for k, v in c.items():
            if v and k not in merged:
                merged[k] = v
    return merged
