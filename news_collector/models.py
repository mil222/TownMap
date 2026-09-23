from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArticleCandidate:
    url: str
    title: str = ""
    published_at: str = ""
    source_url: str = ""


@dataclass(frozen=True, slots=True)
class ArticleContent:
    title: str
    published_at: str
    body_text: str


@dataclass(frozen=True, slots=True)
class SourceConfig:
    id: str
    name: str
    kind: str
    url: str
    enabled: bool = True
    backfill_pages: int = 1
    poll_pages: int = 1
    initial_backfill: bool = True


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    database: str
    request_delay_seconds: float
    request_jitter_seconds: float
    timeout_seconds: float
    max_retries: int
    max_articles_per_run: int
    respect_robots_txt: bool
    sources: tuple[SourceConfig, ...]
