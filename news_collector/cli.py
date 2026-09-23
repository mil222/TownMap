from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, load_config
from .database import open_database
from .locking import AlreadyRunning, ProcessLock
from .service import collect_once


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TownMap 뉴스 증분 수집기")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="TOML 설정 파일")
    parser.add_argument("--verbose", action="store_true", help="상세 로그 출력")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("collect", help="한 번 수집")

    daemon = subparsers.add_parser("daemon", help="일정 간격으로 계속 수집")
    daemon.add_argument("--every", type=int, default=300, help="실행 간격(초, 기본 300)")
    subparsers.add_parser("status", help="저장 현황 출력")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config(args.config)

    if args.command == "status":
        with open_database(config.database) as database:
            stats = database.stats()
        print(
            f"전체 {stats['total'] or 0}건 | 저장 {stats['fetched'] or 0}건 | "
            f"대기 {stats['pending'] or 0}건 | 실패 {stats['failed'] or 0}건"
        )
        return 0

    lock_path = Path(config.database).with_suffix(".lock")
    try:
        with ProcessLock(lock_path):
            if args.command == "collect":
                _run_and_print(config)
                return 0

            interval = max(60, args.every)
            print(f"자동 수집 시작: {interval}초 간격 (중지: Ctrl+C)")
            while True:
                _run_and_print(config)
                time.sleep(interval)
    except AlreadyRunning as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n자동 수집을 종료했습니다.")
        return 0


def _run_and_print(config) -> None:
    summary = collect_once(config)
    print(
        f"발견 {summary.discovered}건 | 신규 {summary.inserted}건 | "
        f"본문 저장 {summary.fetched}건 | 실패 {summary.failed}건"
    )


if __name__ == "__main__":
    raise SystemExit(main())
