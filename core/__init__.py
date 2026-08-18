"""insight-refinery 코어 모듈.

수집(collectors) → 요약(processor) → 알림(notifier) 세 단계는 실행 환경에
의존하지 않는다. Phase 1의 GitHub Actions 배치도, Phase 2의 상시 워커도
이 모듈들을 그대로 조립해 쓴다.
"""

from __future__ import annotations

__version__ = "0.1.0"
