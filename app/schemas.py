"""Request and response models for the HTTP API (app/main.py)."""

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

MAX_K = 15
MAX_HISTORY_ITEMS = 12

# Trimmed first, then the length is checked, so "   " is rejected as empty.
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
FilterValue = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")  # an unknown field is a 422, not silently ignored


class Filters(_Request):
    """Equality filters and an inclusive created-date range. Unset or empty means no filter."""

    category: FilterValue | None = None
    priority: FilterValue | None = None
    status: FilterValue | None = None
    team: FilterValue | None = None
    date_from: str | None = None
    date_to: str | None = None

    @field_validator("date_from", "date_to", mode="before")
    @classmethod
    def _real_date(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str):
            raise ValueError("must be a date in YYYY-MM-DD format")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("must be a date in YYYY-MM-DD format")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("must be a real calendar date") from None
        return value

    @model_validator(mode="after")
    def _range_in_order(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")
        return self


class SearchRequest(_Request):
    query: Text
    k: int = Field(default=6, ge=1, le=MAX_K)
    filters: Filters | None = None


class HistoryTurn(_Request):
    role: Literal["user", "assistant"]
    content: Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class ChatRequest(_Request):
    question: Text
    k: int = Field(default=6, ge=1, le=MAX_K)
    filters: Filters | None = None
    history: list[HistoryTurn] | None = Field(default=None, max_length=MAX_HISTORY_ITEMS)


class ErrorResponse(BaseModel):
    error: str
    code: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    indexed_tickets: int
    model: str
    key_configured: bool


class FiltersResponse(BaseModel):
    category: list[str]
    priority: list[str]
    status: list[str]
    team: list[str]
    created_min: str | None  # YYYY-MM-DD, None when there are no tickets
    created_max: str | None


class SearchHit(BaseModel):
    ticket_id: str
    title: str
    category: str | None
    priority: str | None
    status: str | None
    similarity: float
    above_threshold: bool  # similarity >= SIMILARITY_THRESHOLD, i.e. what /chat would keep
    snippet: str


class SearchResponse(BaseModel):
    threshold: float
    hits: list[SearchHit]


class Source(BaseModel):
    ticket_id: str
    title: str
    category: str | None
    priority: str | None
    status: str | None
    score: float | None  # None for tickets fetched by exact reference
    snippet: str


class ChatResponse(BaseModel):
    answer: str
    found: bool
    citations: list[str]
    sources: list[Source]
    retrieved_count: int
    unverified_citations: list[str]
    missing_references: list[str]


class Comment(BaseModel):
    author: str
    timestamp: str
    text: str


class TicketDetail(BaseModel):
    ticket_id: str
    title: str
    description: str
    category: str
    priority: str
    status: str
    team: str
    reporter: str
    assignee: str
    created_at: str
    resolved_at: str | None
    resolution_notes: str | None
    comments: list[Comment]
