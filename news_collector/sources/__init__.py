from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass

from ..http import HttpClient
from ..models import ArticleCandidate, ArticleContent, SourceConfig


SourceCollector = Callable[[SourceConfig, HttpClient], list[ArticleCandidate]]
ArticleParser = Callable[[bytes, ArticleCandidate], ArticleContent]


@dataclass(frozen=True, slots=True)
class SourceAdapter:
    collect: SourceCollector
    parse_article: ArticleParser


def get_adapter(kind: str) -> SourceAdapter:
    """sources/*.py에서 목록 수집기와 본문 파서를 자동으로 찾는다."""
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{module_info.name}")
        module_kind = getattr(module, "KIND", module_info.name)
        collector = getattr(module, "collect", None)
        article_parser = getattr(module, "parse_article", None)
        if module_kind == kind and callable(collector) and callable(article_parser):
            return SourceAdapter(collect=collector, parse_article=article_parser)
    raise ValueError(
        f"지원하지 않는 source kind: {kind}. "
        "news_collector/sources에 collect()와 parse_article()이 있는 모듈을 추가하세요."
    )


__all__ = ["SourceAdapter", "get_adapter"]
