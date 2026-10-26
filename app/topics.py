"""SaaS industry taxonomy and the rules that decide whether an article is on-topic.

Matching rule for each term: if the term contains any uppercase letter it is matched
case-sensitively ("CRM", "Salesforce"), otherwise case-insensitively ("zero trust").
That keeps acronyms and brand names precise without forcing users to think about it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable

INDUSTRIES: dict[str, dict[str, object]] = {
    "general-saas": {
        "label": "SaaS / Enterprise Software (general)",
        "terms": ["SaaS", "software-as-a-service", "software as a service", "cloud software",
                  "enterprise software", "subscription software", "annual recurring revenue"],
    },
    "crm-sales": {
        "label": "CRM & Sales Tech",
        "terms": ["CRM", "customer relationship management", "sales engagement", "revenue operations",
                  "RevOps", "Salesforce", "HubSpot", "Gong", "Outreach.io"],
    },
    "hr-tech": {
        "label": "HR Tech & Payroll",
        "terms": ["HR software", "HR tech", "HRIS", "human capital management", "payroll software",
                  "applicant tracking", "Workday", "Rippling", "Deel", "Gusto", "BambooHR"],
    },
    "fintech-saas": {
        "label": "Finance & Spend Management SaaS",
        "terms": ["spend management", "expense management", "accounts payable automation",
                  "subscription billing", "billing software", "Brex", "Stripe", "Coupa", "Bill.com"],
    },
    "cybersecurity": {
        "label": "Cybersecurity",
        "terms": ["cybersecurity", "zero trust", "SIEM", "endpoint security", "identity security",
                  "ransomware", "CrowdStrike", "Palo Alto Networks", "Zscaler", "Okta", "SentinelOne"],
    },
    "data-analytics": {
        "label": "Data & Analytics",
        "terms": ["data warehouse", "data lakehouse", "business intelligence", "analytics platform",
                  "data observability", "Snowflake", "Databricks", "Tableau", "dbt Labs"],
    },
    "ai-saas": {
        "label": "Enterprise AI & Agents",
        "terms": ["generative AI", "AI agents", "agentic AI", "large language model", "LLM",
                  "enterprise AI", "AI copilot", "OpenAI", "Anthropic"],
    },
    "devtools": {
        "label": "Developer Tools & DevOps",
        "terms": ["developer tools", "DevOps", "observability", "CI/CD", "platform engineering",
                  "GitHub", "GitLab", "Datadog", "JFrog"],
    },
    "cloud-infra": {
        "label": "Cloud Infrastructure",
        "terms": ["cloud computing", "Kubernetes", "serverless", "infrastructure as code",
                  "AWS", "Microsoft Azure", "Google Cloud", "HashiCorp"],
    },
    "martech": {
        "label": "Marketing Tech",
        "terms": ["marketing automation", "martech", "customer data platform", "email marketing",
                  "Braze", "Klaviyo", "Adobe Experience Cloud", "Marketo"],
    },
    "collaboration": {
        "label": "Collaboration & Productivity",
        "terms": ["collaboration software", "project management software", "workplace productivity",
                  "Atlassian", "Asana", "monday.com", "Zoom Communications", "Microsoft Teams", "Slack"],
    },
    "erp": {
        "label": "ERP & Financial Systems",
        "terms": ["ERP", "enterprise resource planning", "financial planning software", "NetSuite",
                  "SAP", "Oracle Fusion", "Infor"],
    },
    "customer-service": {
        "label": "Customer Service & Contact Center",
        "terms": ["customer service software", "contact center", "CCaaS", "help desk software",
                  "Zendesk", "Intercom", "Freshworks", "ServiceNow", "Genesys"],
    },
    "vertical-saas": {
        "label": "Vertical SaaS",
        "terms": ["vertical SaaS", "industry cloud", "Procore", "Veeva", "Toast Inc", "ServiceTitan"],
    },
    "ecommerce": {
        "label": "E-commerce Platforms",
        "terms": ["ecommerce platform", "e-commerce platform", "Shopify", "BigCommerce",
                  "commerce cloud", "headless commerce"],
    },
    "healthtech": {
        "label": "Health Tech SaaS",
        "terms": ["healthcare software", "electronic health records", "EHR", "Epic Systems",
                  "telehealth platform", "health tech"],
    },
}

TITLE_WEIGHT = 2
INDUSTRY_THRESHOLD = 2   # one headline hit, or two body mentions
EXECUTIVE_THRESHOLD = 1  # executives are specific enough that one mention counts


@lru_cache(maxsize=4096)
def term_pattern(term: str) -> re.Pattern[str]:
    flags = 0 if any(c.isupper() for c in term) else re.IGNORECASE
    return re.compile(r"(?<![\w])" + re.escape(term) + r"(?![\w])", flags)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@dataclass
class TopicGroup:
    label: str
    terms: list[str]
    kind: str  # "industry" | "executive"

    @property
    def threshold(self) -> int:
        return EXECUTIVE_THRESHOLD if self.kind == "executive" else INDUSTRY_THRESHOLD


@dataclass
class Topic:
    industries: list[TopicGroup] = field(default_factory=list)
    executives: list[TopicGroup] = field(default_factory=list)
    mode: str = "any"  # "any" (industry OR executive) | "all" (industry AND executive)

    @property
    def groups(self) -> list[TopicGroup]:
        return self.industries + self.executives

    def describe(self) -> str:
        parts = []
        if self.industries:
            parts.append(" / ".join(g.label for g in self.industries))
        if self.executives:
            parts.append(" / ".join(g.label for g in self.executives))
        joiner = " AND " if self.mode == "all" else " OR "
        return joiner.join(parts) or "(no topic)"


def build_topic(
    industries: Iterable[str] = (),
    custom_terms: Iterable[str] = (),
    executives: Iterable[str] = (),
    mode: str = "any",
) -> Topic:
    topic = Topic(mode="all" if mode == "all" else "any")
    for ident in industries:
        preset = INDUSTRIES.get(ident)
        if preset:
            topic.industries.append(TopicGroup(str(preset["label"]), list(preset["terms"]), "industry"))  # type: ignore[arg-type]
    custom = [t.strip() for t in custom_terms if t and t.strip()]
    if custom:
        topic.industries.append(TopicGroup("Custom: " + ", ".join(custom), custom, "industry"))
    for name in executives:
        name = " ".join(name.split())
        if name:
            topic.executives.append(TopicGroup(name, [name], "executive"))
    return topic


def _score(group: TopicGroup, title: str, body: str) -> int:
    score = 0
    for term in group.terms:
        pat = term_pattern(term)
        if pat.search(title):
            score += TITLE_WEIGHT
        score += min(len(pat.findall(body)), 3)
    return score


def match_text(topic: Topic, title: str, body: str) -> list[str]:
    """Return the labels of matched groups, or [] if the article is off-topic."""
    ind = [g.label for g in topic.industries if _score(g, title, body) >= g.threshold]
    exe = [g.label for g in topic.executives if _score(g, title, body) >= g.threshold]
    if topic.mode == "all" and topic.industries and topic.executives:
        return ind + exe if ind and exe else []
    return ind + exe


def prefilter_hit(topic: Topic, url: str, title: str | None = None) -> bool:
    """Cheap check used on sitemap/RSS entries before we spend a fetch on the page.

    Looks for any term in the headline (when the listing provides one) or in the URL slug.
    Executives also match on surname alone in slugs, since slugs often drop first names.
    """
    if title:
        for group in topic.groups:
            if any(term_pattern(t).search(title) for t in group.terms):
                return True
            if group.kind == "executive":
                last = group.terms[0].split()[-1]
                if term_pattern(last).search(title):
                    return True
    path = "-" + slugify(url.split("://", 1)[-1].split("/", 1)[-1]) + "-"
    for group in topic.groups:
        for term in group.terms:
            if f"-{slugify(term)}-" in path:
                return True
        if group.kind == "executive":
            last = slugify(group.terms[0].split()[-1])
            if len(last) >= 4 and f"-{last}-" in path:
                return True
    return False


def search_queries(topic: Topic) -> list[str]:
    """Queries for keyword search providers (GDELT, NewsAPI)."""
    queries = [f'"{g.terms[0]}"' for g in topic.executives]
    for g in topic.industries:
        # Multi-word terms are the most distinctive; fall back to the first term.
        phrases = [t for t in g.terms if " " in t][:3] or g.terms[:1]
        quoted = [f'"{p}"' for p in phrases]
        # GDELT rejects parentheses that do not wrap an OR expression.
        queries.append(quoted[0] if len(quoted) == 1 else "(" + " OR ".join(quoted) + ")")
    return queries
