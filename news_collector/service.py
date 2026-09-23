from __future__ import annotations

import logging
import urllib.error
from dataclasses import dataclass, replace

from .database import NewsDatabase
from .http import HttpClient, RobotsDenied
from .models import ArticleCandidate, CollectorConfig, SourceConfig
from .sources import get_adapter


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RunSummary:
    discovered: int = 0
    inserted: int = 0
    fetched: int = 0
    failed: int = 0
    source_failures: int = 0


def build_http_client(config: CollectorConfig) -> HttpClient:
    return HttpClient(
        delay_seconds=config.request_delay_seconds,
        jitter_seconds=config.request_jitter_seconds,
        timeout_seconds=config.timeout_seconds,
        max_retries=config.max_retries,
        respect_robots_txt=config.respect_robots_txt,
    )


def collect_once(config: CollectorConfig) -> RunSummary:
    database = NewsDatabase(config.database)
    client = build_http_client(config)
    summary = RunSummary()
    run_id = database.begin_run()
    try:
        for source in config.sources:
            if not source.enabled:
                continue
            try:
                active_source = source
                if database.has_source_articles(source.id):
                    active_source = replace(
                        source,
                        backfill_pages=source.poll_pages,
                        initial_backfill=False,
                    )
                candidates = _discover_source(active_source, client)
                inserted = sum(database.discover(source, article) for article in candidates)
                database.commit()
                summary.discovered += len(candidates)
                summary.inserted += inserted
                LOGGER.info("[%s] 발견 %d건, 신규 %d건", source.name, len(candidates), inserted)
            except Exception as error:
                summary.source_failures += 1
                LOGGER.error("[%s] 목록 수집 실패: %s", source.name, error)

        for article in database.pending(config.max_articles_per_run):
            try:
                response = client.get(article["url"])
                adapter = get_adapter(article["source_kind"])
                content = adapter.parse_article(
                    response.body,
                    ArticleCandidate(
                        url=article["url"],
                        title=article["title_hint"],
                        published_at=article["published_hint"],
                        source_url=article["source_url"],
                    ),
                )
                database.mark_fetched(article["url"], article["source"], content)
                summary.fetched += 1
            except RobotsDenied as error:
                database.mark_failed(article["url"], str(error), permanent=True)
                summary.failed += 1
            except urllib.error.HTTPError as error:
                permanent = error.code in {401, 403, 404, 410}
                database.mark_failed(
                    article["url"], f"HTTP {error.code}: {error.reason}", permanent=permanent
                )
                summary.failed += 1
            except Exception as error:
                database.mark_failed(article["url"], str(error), permanent=False)
                summary.failed += 1
            finally:
                database.commit()

        status = "partial" if summary.source_failures or summary.failed else "success"
        database.finish_run(
            run_id,
            discovered=summary.discovered,
            inserted=summary.inserted,
            fetched=summary.fetched,
            failed=summary.failed,
            status=status,
            message=f"source_failures={summary.source_failures}",
        )
        return summary
    except BaseException as error:
        database.finish_run(
            run_id,
            discovered=summary.discovered,
            inserted=summary.inserted,
            fetched=summary.fetched,
            failed=summary.failed,
            status="failed",
            message=str(error),
        )
        raise
    finally:
        database.close()


def _discover_source(source: SourceConfig, client: HttpClient):
    return get_adapter(source.kind).collect(source, client)
