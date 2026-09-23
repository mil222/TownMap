from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

from ..http import HttpClient
from ..models import ArticleCandidate, ArticleContent, SourceConfig


KIND = "yna_latest"


class _LatestNewsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_latest_section = False
        self._section_nesting = 0
        self._current_url = ""
        self._title_parts: list[str] = []
        self.items: list[ArticleCandidate] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "section":
            if self._in_latest_section:
                self._section_nesting += 1
            elif "box-latest01" in classes:
                self._in_latest_section = True
                self._section_nesting = 1

        if (
            self._in_latest_section
            and tag == "a"
            and "tit-news" in classes
            and attributes.get("href")
        ):
            self._current_url = attributes["href"] or ""
            self._title_parts = []

    def handle_data(self, data: str) -> None:
        if self._current_url:
            self._title_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current_url:
            title = " ".join("".join(self._title_parts).split())
            self.items.append(ArticleCandidate(url=self._current_url, title=title))
            self._current_url = ""
            self._title_parts = []

        if tag == "section" and self._in_latest_section:
            self._section_nesting -= 1
            if self._section_nesting == 0:
                self._in_latest_section = False


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.published_at = ""
        self.paragraphs: list[str] = []
        self._in_story = False
        self._story_div_depth = 0
        self._capture_paragraph = False
        self._paragraph_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())

        if tag == "meta":
            property_name = attributes.get("property", "")
            if property_name == "og:title":
                self.title = (attributes.get("content") or "").strip()
            elif property_name == "article:published_time":
                self.published_at = (attributes.get("content") or "").strip()

        if tag == "div":
            if self._in_story:
                self._story_div_depth += 1
            elif {"story-news", "article"}.issubset(classes):
                self._in_story = True
                self._story_div_depth = 1

        if self._in_story and tag == "p":
            # 사진 설명, 저작권 문구 등 class가 있는 보조 문단은 제외한다.
            self._capture_paragraph = not classes
            self._paragraph_parts = []
        elif self._capture_paragraph and tag == "br":
            self._paragraph_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._capture_paragraph:
            self._paragraph_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._capture_paragraph:
            paragraph = _clean_text("".join(self._paragraph_parts))
            if paragraph and not _is_byline_or_notice(paragraph):
                self.paragraphs.append(paragraph)
            self._capture_paragraph = False
            self._paragraph_parts = []

        if tag == "div" and self._in_story:
            self._story_div_depth -= 1
            if self._story_div_depth == 0:
                self._in_story = False


def collect(source: SourceConfig, client: HttpClient) -> list[ArticleCandidate]:
    all_items: dict[str, ArticleCandidate] = {}
    for page_number in range(1, source.backfill_pages + 1):
        page_url = _page_url(source.url, page_number)
        response = client.get(page_url)
        parser = _LatestNewsParser()
        parser.feed(_decode_html(response.body))
        page_items = parser.items
        if not page_items:
            break

        new_count = 0
        for article in page_items:
            if article.url not in all_items:
                all_items[article.url] = ArticleCandidate(
                    url=article.url,
                    title=article.title,
                    source_url=page_url,
                )
                new_count += 1
        if new_count == 0:
            break
    return list(all_items.values())


def parse_article(content: bytes, candidate: ArticleCandidate) -> ArticleContent:
    parser = _ArticleParser()
    parser.feed(_decode_html(content))
    title = re.sub(r"\s*\|\s*연합뉴스\s*$", "", parser.title or candidate.title.strip())
    published_at = parser.published_at or candidate.published_at.strip()
    body_text = "\n\n".join(parser.paragraphs)
    if not title:
        raise ValueError(f"기사 제목을 찾지 못했습니다: {candidate.url}")
    if not body_text:
        # 속보·시황처럼 본문 없이 제목만 제공되는 기사도 수집 대상으로 보관한다.
        body_text = title
    return ArticleContent(title=title, published_at=published_at, body_text=body_text)


def _page_url(base_url: str, page_number: int) -> str:
    if page_number == 1:
        return base_url
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/news/{page_number}", parts.query, ""))


def _decode_html(content: bytes) -> str:
    for encoding in ("utf-8", "euc-kr"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _clean_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.replace("\u00a0", " ").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _is_byline_or_notice(value: str) -> bool:
    if re.fullmatch(r"[^\s@]+@[^\s@]+", value):
        return True
    return "저작권자(c) 연합뉴스" in value or "무단 전재" in value
