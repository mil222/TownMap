from __future__ import annotations

import tomllib
from pathlib import Path

from .models import CollectorConfig, SourceConfig


DEFAULT_CONFIG_PATH = Path("config/news_sources.toml")


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> CollectorConfig:
    config_path = Path(path)
    with config_path.open("rb") as file:
        raw = tomllib.load(file)

    collector = raw.get("collector", {})
    sources = tuple(
        SourceConfig(
            id=item["id"],
            name=item["name"],
            kind=item["kind"],
            url=item["url"],
            enabled=bool(item.get("enabled", True)),
            backfill_pages=max(1, int(item.get("backfill_pages", 1))),
            poll_pages=max(1, int(item.get("poll_pages", 1))),
        )
        for item in raw.get("sources", [])
    )
    if not sources:
        raise ValueError(f"수집 소스가 없습니다: {config_path}")

    return CollectorConfig(
        database=str(collector.get("database", "data/news.db")),
        request_delay_seconds=max(0.0, float(collector.get("request_delay_seconds", 1.0))),
        request_jitter_seconds=max(0.0, float(collector.get("request_jitter_seconds", 0.25))),
        timeout_seconds=max(1.0, float(collector.get("timeout_seconds", 15))),
        max_retries=max(1, int(collector.get("max_retries", 3))),
        max_articles_per_run=max(1, int(collector.get("max_articles_per_run", 500))),
        respect_robots_txt=bool(collector.get("respect_robots_txt", True)),
        sources=sources,
    )
