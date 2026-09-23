from __future__ import annotations

import email.utils
import logging
import os
import random
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    body: bytes


class RobotsDenied(RuntimeError):
    pass


class HttpClient:
    def __init__(
        self,
        *,
        delay_seconds: float,
        jitter_seconds: float,
        timeout_seconds: float,
        max_retries: int,
        respect_robots_txt: bool,
    ) -> None:
        contact = os.environ.get("NEWS_CRAWLER_CONTACT", "").strip()
        self.user_agent = "TownMapNewsCollector/0.2"
        if contact:
            self.user_agent += f" (+{contact})"
        self.delay_seconds = delay_seconds
        self.jitter_seconds = jitter_seconds
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.respect_robots_txt = respect_robots_txt
        self._last_request_by_host: dict[str, float] = {}
        self._robots_by_origin: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def get(self, url: str, *, check_robots: bool = True) -> HttpResponse:
        if check_robots and self.respect_robots_txt and not self._robots_allowed(url):
            raise RobotsDenied(f"robots.txt가 수집을 허용하지 않습니다: {url}")

        host = urlsplit(url).netloc.lower()
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self._wait_for_host(host)
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                    "Accept-Encoding": "identity",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read()
                    return HttpResponse(body=body)
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code in {401, 403, 404, 410}:
                    raise
                if error.code == 429 or 500 <= error.code < 600:
                    self._backoff(attempt, error.headers.get("Retry-After"))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
                self._backoff(attempt, None)

        raise RuntimeError(f"{self.max_retries}회 재시도 후 요청 실패: {url}") from last_error

    def _wait_for_host(self, host: str) -> None:
        previous = self._last_request_by_host.get(host)
        target_delay = self.delay_seconds + random.uniform(0, self.jitter_seconds)
        if previous is not None:
            remaining = target_delay - (time.monotonic() - previous)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_by_host[host] = time.monotonic()

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = _retry_after_seconds(retry_after)
        if delay is None:
            delay = min(60.0, (2**attempt) + random.uniform(0, 1.0))
        time.sleep(delay)

    def _robots_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        if origin not in self._robots_by_origin:
            robots_url = f"{origin}/robots.txt"
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(robots_url)
            try:
                response = self.get(robots_url, check_robots=False)
                parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
                self._robots_by_origin[origin] = parser
            except urllib.error.HTTPError as error:
                # RFC 관행: 4xx(401/403 제외)는 robots 파일이 없는 것으로 본다.
                if error.code in {401, 403}:
                    LOGGER.warning("robots.txt 접근 거부로 해당 호스트 수집을 건너뜁니다: %s", origin)
                    self._robots_by_origin[origin] = None
                    return False
                self._robots_by_origin[origin] = parser
                parser.parse([])
            except Exception as error:  # 네트워크 일시 장애는 로그를 남기고 이번 실행에서만 허용
                LOGGER.warning("robots.txt 확인 실패(%s): %s", origin, error)
                self._robots_by_origin[origin] = parser
                parser.parse([])

        parser = self._robots_by_origin[origin]
        return parser is not None and parser.can_fetch(self.user_agent, url)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, min(300.0, float(value)))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, min(300.0, (parsed - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError):
            return None
