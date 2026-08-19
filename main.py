"""insight-refinery 엔트리포인트.

수집 → 중복 제거 → LLM 요약 → 임계치 필터 → 알림 → 캐시 저장 순서로
한 사이클을 실행한다. 이 파일은 "조립"만 담당하고 실제 로직은 `core/`에 있다.

사용 예:
    python main.py                     # 기본 실행
    python main.py --dry-run           # 알림 대신 stdout 출력, 캐시 저장 안 함
    python main.py --limit 3 --source "OpenAI News"
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from core.collectors import build_collectors
from core.collectors.base import RawItem
from core.config import DEFAULT_CONFIG_PATH, Config, load_config
from core.dedup import ProcessedStore
from core.notifier import build_notifiers
from core.processor import ProcessedItem, Processor

logger = logging.getLogger("insight_refinery")

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="insight-refinery 파이프라인 실행")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="설정 파일 경로")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="알림을 보내지 않고 stdout에 출력하며 캐시도 저장하지 않는다",
    )
    parser.add_argument("--limit", type=int, default=None, help="이번 실행의 최대 요약 건수")
    parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        default=None,
        help="지정한 이름의 소스만 실행한다 (반복 지정 가능)",
    )
    parser.add_argument(
        "--send-digest",
        action="store_true",
        help="다이제스트 발송 시각이 아니어도 대기열을 지금 발송한다",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="디버그 로그 출력")
    return parser.parse_args(argv)


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def collect(config: Config, only: list[str] | None) -> list[RawItem]:
    """활성화된 모든 소스에서 아이템을 모아 최신순으로 정렬한다."""
    sources = config.enabled_sources
    if only:
        wanted = set(only)
        sources = [source for source in sources if source.name in wanted]
        missing = wanted - {source.name for source in sources}
        if missing:
            logger.warning("설정에 없거나 비활성화된 소스입니다: %s", ", ".join(sorted(missing)))

    items: list[RawItem] = []
    for collector in build_collectors(sources):
        items.extend(collector.safe_collect())

    items.sort(key=lambda item: item.published_at or _EPOCH, reverse=True)
    return items


def filter_new(items: list[RawItem], store: ProcessedStore) -> list[RawItem]:
    """캐시에 없는 아이템만 남긴다 (같은 실행 내 중복도 제거)."""
    seen: set[str] = set()
    fresh: list[RawItem] = []
    for item in items:
        key = item.dedup_key
        if key in store or key in seen:
            continue
        seen.add(key)
        fresh.append(item)
    return fresh


def run(config: Config, args: argparse.Namespace) -> int:
    store = ProcessedStore(config.cache.path, config.cache.max_entries).load()

    collected = collect(config, args.sources)
    fresh = filter_new(collected, store)
    limit = args.limit or config.run.max_items_per_run
    targets = fresh[:limit]

    logger.info(
        "수집 %d건 → 신규 %d건 → 이번 실행 대상 %d건 (상한 %d)",
        len(collected),
        len(fresh),
        len(targets),
        limit,
    )
    if not targets:
        logger.info("처리할 신규 아이템이 없습니다. 종료합니다.")
        return 0

    processor = Processor(
        providers=config.llm.providers,
        temperature=config.llm.temperature,
        max_retries=config.llm.max_retries,
        max_content_chars=config.llm.max_content_chars,
        max_retry_delay=config.llm.max_retry_delay,
    )
    processed = processor.process_many(targets)

    processed.sort(key=lambda p: p.importance, reverse=True)
    logger.info("요약 성공 %d/%d건", len(processed), len(targets))

    if processed:
        _notify(config, args, processed)

    # 알림 여부와 무관하게, 요약에 성공한 것은 모두 처리 완료로 기록한다.
    # (임계치 미만이라 안 보낸 항목을 다음 실행에서 다시 요약하면 토큰 낭비)
    if args.dry_run:
        logger.info("드라이런이므로 캐시를 저장하지 않습니다")
    else:
        store.add_many(item.raw.dedup_key for item in processed)
        store.save()

    return 0


def _notify(
    config: Config, args: argparse.Namespace, processed: list[ProcessedItem]
) -> None:
    """채널마다 자기 임계치로 걸러 보낸다."""
    notifiers = build_notifiers(
        config.notifier,
        dry_run=args.dry_run,
        force_digest=args.send_digest,
        now=datetime.now(timezone.utc),
    )
    for notifier in notifiers:
        threshold = config.notifier.threshold_for(
            notifier.name, config.run.min_importance
        )
        selected = [item for item in processed if item.importance >= threshold]
        if not selected:
            logger.info("[%s] %d점 이상 없음, 건너뜀", notifier.name, threshold)
            continue
        sent = notifier.send_many(selected)
        logger.info(
            "[%s] %d점 이상 %d건 중 %d건 전송",
            notifier.name, threshold, len(selected), sent,
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001 - 설정 오류는 즉시 실패시킨다
        logger.error("설정 로드 실패: %s", exc)
        return 2

    try:
        return run(config, args)
    except KeyboardInterrupt:
        logger.warning("사용자 중단")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
