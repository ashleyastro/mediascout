"""Request/response schemas shared by the REST API and the MCP tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Region = Literal["NA", "EU", "UK"]


class ScanParams(BaseModel):
    industries: list[str] = Field(default_factory=list, description="Preset industry ids, e.g. 'cybersecurity'")
    custom_terms: list[str] = Field(default_factory=list, description="Extra keywords or company names")
    executives: list[str] = Field(default_factory=list, description="Public executives, e.g. 'Marc Benioff'")
    match_mode: Literal["any", "all"] = Field(
        "any", description="'any' = industry OR executive; 'all' = industry AND executive")
    lookback_value: int | None = Field(
        None, ge=1, le=120, description="Optional date limit; leave empty to count articles from all time")
    lookback_unit: Literal["months", "years"] = "years"
    min_articles: int = Field(5, ge=1, le=100)
    regions: list[Region] = Field(default_factory=lambda: ["NA", "EU", "UK"])
    max_outlets: int = Field(1000, ge=1, le=5000)

    @field_validator("industries", "custom_terms", "executives")
    @classmethod
    def _strip(cls, values: list[str]) -> list[str]:
        return [v.strip() for v in values if v and v.strip()]

    @model_validator(mode="after")
    def _needs_topic(self) -> "ScanParams":
        if not (self.industries or self.custom_terms or self.executives):
            raise ValueError("Choose at least one industry, keyword or executive")
        if not self.regions:
            raise ValueError("Choose at least one region")
        return self

    @property
    def lookback_days(self) -> int | None:
        if self.lookback_value is None:
            return None
        per_unit = 365.25 if self.lookback_unit == "years" else 30.44
        return round(self.lookback_value * per_unit)


class EmailRequest(BaseModel):
    to: list[str] = Field(min_length=1, max_length=10)
    note: str | None = Field(None, max_length=2000)

    @field_validator("to")
    @classmethod
    def _valid(cls, values: list[str]) -> list[str]:
        import re
        out = []
        for v in values:
            v = v.strip()
            if not re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", v):
                raise ValueError(f"Invalid email address: {v}")
            out.append(v)
        return out


class SuppressionRequest(BaseModel):
    value: str = Field(min_length=3, max_length=320, description="Email address or full name to exclude")
