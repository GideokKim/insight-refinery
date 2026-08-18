"""Reddit 서브레딧 수집기.

인증 없이 공개 JSON 엔드포인트(`https://www.reddit.com/r/<sub>/<listing>.json`)를
사용한다. GitHub Actions 등 공용 IP에서는 익명 요청이 429/403으로 막히는 일이
잦으므로, `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` 이 설정돼 있으면
application-only OAuth 토큰을 받아 `oauth.reddit.com`으로 붙는다.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from .base import Collector, RawItem, iter_limited

logger = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
PUBLIC_BASE = "https://www.reddit.com"
OAUTH_BASE = "https://oauth.reddit.com"
DEFAULT_USER_AGENT = "python:insight-refinery:0.1.0 (by /u/insight-refinery)"


class RedditCollector(Collector):
    """서브레딧의 글 목록을 `RawItem`으로 변환한다.

    options:
        subreddit (str, 필수): 서브레딧 이름 ("r/" 없이)
        listing (str): hot | new | top | rising (기본 "hot")
        time_filter (str): listing이 top일 때의 기간 (기본 "day")
        limit (int): 가져올 글 수 (기본 25, 최대 100)
        min_score (int): 이 점수 미만은 버린다 (기본 0)
        skip_stickied (bool): 공지/고정글 제외 (기본 True)
        max_content_chars (int): 본문 길이 상한 (기본 4000)
        timeout (float): HTTP 타임아웃 초 (기본 20)
    """

    type = "reddit"

    def collect(self) -> Iterable[RawItem]:
        subreddit = self._option("subreddit")
        if not subreddit:
            raise ValueError(f"[{self.name}] reddit 소스에는 'subreddit' 옵션이 필요합니다")

        listing = self._option("listing", "hot")
        limit = min(int(self._option("limit", 25)), 100)
        min_score = int(self._option("min_score", 0))
        skip_stickied = bool(self._option("skip_stickied", True))
        max_chars = int(self._option("max_content_chars", 4000))
        timeout = float(self._option("timeout", 20))

        params: dict[str, Any] = {"limit": limit, "raw_json": 1}
        if listing == "top":
            params["t"] = self._option("time_filter", "day")

        headers = {"User-Agent": self._option("user_agent", DEFAULT_USER_AGENT)}
        base = PUBLIC_BASE
        token = self._fetch_token(headers["User-Agent"], timeout)
        if token:
            headers["Authorization"] = f"bearer {token}"
            base = OAUTH_BASE

        url = f"{base}/r/{subreddit}/{listing}"
        if base == PUBLIC_BASE:
            url += ".json"

        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()

        children = payload.get("data", {}).get("children", [])
        for child in iter_limited(self._filter(children, min_score, skip_stickied), limit):
            yield child

    def _filter(
        self, children: list[dict[str, Any]], min_score: int, skip_stickied: bool
    ) -> Iterable[RawItem]:
        for child in children:
            post = child.get("data") or {}
            if skip_stickied and post.get("stickied"):
                continue
            if int(post.get("score", 0)) < min_score:
                continue
            item = self._to_item(post)
            if item is not None:
                yield item

    def _to_item(self, post: dict[str, Any]) -> RawItem | None:
        post_id = post.get("id")
        title = (post.get("title") or "").strip()
        if not post_id or not title:
            return None

        max_chars = int(self._option("max_content_chars", 4000))
        created = post.get("created_utc")
        published = (
            datetime.fromtimestamp(float(created), tz=timezone.utc) if created else None
        )
        permalink = post.get("permalink") or ""

        return RawItem(
            source=self.name,
            source_type=self.type,
            external_id=post_id,
            title=title,
            url=f"https://www.reddit.com{permalink}" if permalink else post.get("url", ""),
            content=(post.get("selftext") or "").strip()[:max_chars],
            author=post.get("author"),
            published_at=published,
            extra={
                "score": post.get("score"),
                "num_comments": post.get("num_comments"),
                "subreddit": post.get("subreddit"),
                "link_url": post.get("url"),
            },
        )

    @staticmethod
    def _fetch_token(user_agent: str, timeout: float) -> str | None:
        """자격 증명이 있으면 application-only OAuth 토큰을 받는다."""
        client_id = os.getenv("REDDIT_CLIENT_ID")
        client_secret = os.getenv("REDDIT_CLIENT_SECRET")
        if not client_id or not client_secret:
            return None

        try:
            response = httpx.post(
                TOKEN_URL,
                auth=(client_id, client_secret),
                data={"grant_type": "client_credentials"},
                headers={"User-Agent": user_agent},
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json().get("access_token")
        except Exception:  # noqa: BLE001 - 토큰 실패 시 익명으로 폴백
            logger.warning("Reddit OAuth 토큰 발급 실패, 익명 요청으로 진행합니다")
            return None
