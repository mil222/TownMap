from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import ArticleCandidate, ArticleContent, SourceConfig
from .urls import canonicalize_url


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class NewsDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def close(self) -> None:
        self.connection.close()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS articles (
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL PRIMARY KEY,
                published_at TEXT NOT NULL,
                body_text TEXT NOT NULL,
                collected_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crawl_queue (
                url TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source TEXT NOT NULL,
                source_url TEXT NOT NULL,
                title_hint TEXT NOT NULL DEFAULT '',
                published_hint TEXT NOT NULL DEFAULT '',
                discovered_at TEXT NOT NULL,
                fetch_state TEXT NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_crawl_queue_fetch
                ON crawl_queue(fetch_state, attempt_count, discovered_at);
            CREATE INDEX IF NOT EXISTS idx_crawl_queue_source
                ON crawl_queue(source_id, discovered_at DESC);

            CREATE TABLE IF NOT EXISTS collection_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                discovered_count INTEGER NOT NULL DEFAULT 0,
                inserted_count INTEGER NOT NULL DEFAULT 0,
                fetched_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running',
                message TEXT
            );
            """
        )
        self.connection.commit()

    def begin_run(self) -> int:
        cursor = self.connection.execute(
            "INSERT INTO collection_runs(started_at) VALUES (?)", (utc_now(),)
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        discovered: int,
        inserted: int,
        fetched: int,
        failed: int,
        status: str,
        message: str = "",
    ) -> None:
        self.connection.execute(
            """
            UPDATE collection_runs
            SET finished_at=?, discovered_count=?, inserted_count=?, fetched_count=?,
                failed_count=?, status=?, message=?
            WHERE id=?
            """,
            (utc_now(), discovered, inserted, fetched, failed, status, message, run_id),
        )
        self.connection.commit()

    def discover(self, source: SourceConfig, article: ArticleCandidate) -> bool:
        canonical = canonicalize_url(article.url)
        if not canonical:
            return False
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO crawl_queue(
                url, source_id, source_kind, source, source_url,
                title_hint, published_hint, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canonical,
                source.id,
                source.kind,
                source.name,
                article.source_url or source.url,
                article.title.strip(),
                article.published_at.strip(),
                utc_now(),
            ),
        )
        inserted = cursor.rowcount == 1
        if not inserted:
            self.connection.execute(
                """
                UPDATE crawl_queue
                SET title_hint=CASE WHEN title_hint='' THEN ? ELSE title_hint END,
                    published_hint=CASE WHEN published_hint='' THEN ? ELSE published_hint END
                WHERE url=?
                """,
                (article.title.strip(), article.published_at.strip(), canonical),
            )
        return inserted

    def commit(self) -> None:
        self.connection.commit()

    def pending(self, limit: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT * FROM crawl_queue
            WHERE fetch_state IN ('pending', 'retry') AND attempt_count < 5
            ORDER BY discovered_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def has_source_articles(self, source_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM crawl_queue WHERE source_id=? LIMIT 1", (source_id,)
        ).fetchone()
        return row is not None

    def mark_fetched(self, url: str, source: str, content: ArticleContent) -> None:
        self.connection.execute(
            """
            INSERT INTO articles(source, title, url, published_at, body_text, collected_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                source=excluded.source,
                title=excluded.title,
                published_at=excluded.published_at,
                body_text=excluded.body_text,
                collected_at=excluded.collected_at
            """,
            (
                source,
                content.title,
                url,
                content.published_at,
                content.body_text,
                utc_now(),
            ),
        )
        self.connection.execute(
            """
            UPDATE crawl_queue
            SET fetch_state='fetched', attempt_count=attempt_count+1, last_error=NULL
            WHERE url=?
            """,
            (url,),
        )

    def mark_failed(self, url: str, error: str, *, permanent: bool) -> None:
        self.connection.execute(
            """
            UPDATE crawl_queue
            SET fetch_state=?, attempt_count=attempt_count+1, last_error=?
            WHERE url=?
            """,
            ("failed" if permanent else "retry", error[:1000], url),
        )

    def stats(self) -> sqlite3.Row:
        return self.connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(fetch_state='fetched') AS fetched,
                   SUM(fetch_state IN ('pending', 'retry')) AS pending,
                   SUM(fetch_state='failed') AS failed
            FROM crawl_queue
            """
        ).fetchone()


@contextmanager
def open_database(path: str | Path) -> Iterator[NewsDatabase]:
    database = NewsDatabase(path)
    try:
        yield database
    finally:
        database.close()
