from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from ..http import HttpClient
from ..models import ArticleCandidate, ArticleContent, SourceConfig


KIND = "newsis_realtime"
_ARTICLE_PATH = re.compile(r"^/view/NISX\d+_\d+")
_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class _RealtimeNewsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_list = False
        self._list_tag = ""
        self._list_tag_depth = 0
        self._in_title = False
        self._current_url = ""
        self._title_parts: list[str] = []
        self.items: list[ArticleCandidate] = []
        self.last_page = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        href = attributes.get("href") or ""
        page_match = re.search(r"[?&]page=(\d+)", href)
        if page_match:
            self.last_page = max(self.last_page, int(page_match.group(1)))

        if self._in_list and tag == self._list_tag:
            self._list_tag_depth += 1
        elif not self._in_list and "articleList2" in classes:
            self._in_list = True
            self._list_tag = tag
            self._list_tag_depth = 1

        if self._in_list and tag == "p" and "tit" in classes:
            self._in_title = True

        if self._in_title and tag == "a" and _ARTICLE_PATH.match(href):
            self._current_url = href
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

        if tag == "p" and self._in_title:
            self._in_title = False

        if tag == self._list_tag and self._in_list:
            self._list_tag_depth -= 1
            if self._list_tag_depth == 0:
                self._in_list = False
                self._list_tag = ""


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.published_at = ""
        self.body_parts: list[str] = []
        self._capture_title = False
        self._title_parts: list[str] = []
        self._in_article = False
        self._body_started = False
        self._body_finished = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())

        if tag == "meta":
            property_name = attributes.get("property", "")
            if property_name == "og:title":
                self.title = (attributes.get("content") or "").strip()
            elif property_name == "article:published_time":
                self.published_at = (attributes.get("content") or "").strip()

        if tag == "h1" and "title_area" in classes:
            self._capture_title = True
            self._title_parts = []

        if tag == "article":
            self._in_article = True

        if not self._in_article or self._body_finished:
            return

        if attributes.get("id") == "textBody":
            self._body_started = True

        if not self._body_started:
            return

        if self._skip_depth:
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return

        skip_classes = {"article_photo", "desc", "photojournal"}
        if tag in {"script", "style", "iframe", "noscript"} or classes & skip_classes:
            if tag not in _VOID_TAGS:
                self._skip_depth = 1
            return

        if tag == "br":
            self.body_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._title_parts.append(data)

        if (
            self._in_article
            and self._body_started
            and not self._body_finished
            and not self._skip_depth
        ):
            if "◎공감언론 뉴시스" in data:
                before_notice = data.split("◎공감언론 뉴시스", 1)[0]
                if before_notice:
                    self.body_parts.append(before_notice)
                self._body_finished = True
            else:
                self.body_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._capture_title:
            heading = " ".join("".join(self._title_parts).split())
            if heading:
                self.title = heading
            self._capture_title = False
            self._title_parts = []

        if self._skip_depth:
            self._skip_depth -= 1

        if tag == "article":
            self._in_article = False


def collect(source: SourceConfig, client: HttpClient) -> list[ArticleCandidate]:
    all_items: dict[str, ArticleCandidate] = {}
    days = ("today", "yesterday") if source.initial_backfill else ("today",)

    for day in days:
        page_number = 1
        page_limit = source.backfill_pages
        while page_number <= page_limit:
            page_url = _page_url(source.url, day, page_number)
            response = client.get(page_url)
            parser = _RealtimeNewsParser()
            parser.feed(_decode_html(response.body))
            if not parser.items:
                break
            if page_number == 1:
                page_limit = min(page_limit, parser.last_page)

            new_count = 0
            for article in parser.items:
                absolute_url = urljoin(source.url, article.url)
                if absolute_url not in all_items:
                    all_items[absolute_url] = ArticleCandidate(
                        url=absolute_url,
                        title=article.title,
                        source_url=page_url,
                    )
                    new_count += 1
            if new_count == 0:
                break
            page_number += 1

    return list(all_items.values())


def parse_article(content: bytes, candidate: ArticleCandidate) -> ArticleContent:
    parser = _ArticleParser()
    parser.feed(_decode_html(content))
    title = parser.title or candidate.title.strip()
    title = re.sub(r"\s*::\s*공감언론\s+뉴시스통신사\s*::\s*$", "", title).strip()
    title = re.sub(r"\s*[|-]\s*뉴시스\s*$", "", title).strip()
    published_at = parser.published_at or candidate.published_at.strip()
    body_text = _clean_body("".join(parser.body_parts))
    if not title:
        raise ValueError(f"기사 제목을 찾지 못했습니다: {candidate.url}")
    if not body_text:
        body_text = title
    return ArticleContent(title=title, published_at=published_at, body_text=body_text)


def _page_url(base_url: str, day: str, page_number: int) -> str:
    parts = urlsplit(base_url)
    query: dict[str, str | int] = {"cid": "realnews", "day": day}
    if page_number > 1:
        query["page"] = page_number
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _decode_html(content: bytes) -> str:
    for encoding in ("utf-8", "euc-kr"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _clean_body(value: str) -> str:
    paragraphs = []
    for part in re.split(r"\n+", value.replace("\u00a0", " ")):
        cleaned = " ".join(part.split())
        if cleaned:
            paragraphs.append(cleaned)
    return "\n\n".join(paragraphs)
