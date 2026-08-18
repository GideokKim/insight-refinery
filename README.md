# insight-refinery

SNS·RSS·뉴스레터 등 여러 소스에서 피드를 모아 LLM으로 **정형화 요약 / 분류 /
중요도 채점**한 뒤, 중요한 것만 골라 알림으로 보내는 범용 데이터 파이프라인.

현재 도메인은 **최신 AI 소식·트렌드**지만, 도메인 지식은 `config.yaml`(소스 목록)과
`core/processor.py`(프롬프트·카테고리)에만 몰려 있어 다른 주제로 바꾸기 쉽다.

## 파이프라인

```
collectors → dedup → processor(LLM) → 임계치 필터 → notifier
   RSS         JSON     Structured        중요도 N점       Telegram
   Reddit      캐시      Outputs           이상만
```

## 개발 단계

| 단계 | 실행 방식 | 상태 |
| --- | --- | --- |
| Phase 1 | GitHub Actions cron + Python 배치 | 현재 |
| Phase 2 | n8n / 상시 워커(FastAPI·Celery) 스트리밍 | 예정 |

`core/` 아래 모듈은 실행 방식(배치/스트리밍)에 의존하지 않는다. `main.py`가
조립만 담당하므로, Phase 2에서는 `main.py` 자리에 워커를 두고 같은 모듈을 쓰면 된다.

## 구조

```
insight-refinery/
├── .github/workflows/pipeline.yml  # cron 실행 + 캐시 자동 커밋
├── core/
│   ├── collectors/                 # 소스별 수집기
│   │   ├── base.py                 #   Collector 인터페이스 + RawItem + 레지스트리
│   │   ├── rss.py                  #   RSS/Atom
│   │   └── reddit.py               #   Reddit (익명 JSON / 선택적 OAuth)
│   ├── config.py                   # config.yaml 로딩·검증 (Pydantic)
│   ├── dedup.py                    # 처리 완료 ID 캐시 (JSON)
│   ├── processor.py                # Pydantic 스키마 + LLM 구조화 요약
│   └── notifier.py                 # Telegram MarkdownV2 발송
├── data/processed_ids.json         # 중복 방지 캐시 (커밋 대상)
├── config.yaml                     # 소스 목록 및 임계치
├── main.py                         # 엔트리포인트
└── requirements.txt
```

## LLM 출력 스키마

`core/processor.py`의 `Insight` 모델로 강제 파싱한다.

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `title_ko` | `str` | 한국어 제목 |
| `summary_ko` | `list[str]` | 핵심 요약 3줄 |
| `category` | `Category` | LLM / Vision / Infra / Research / Industry / Product / Policy / Other |
| `importance` | `1~5` | 실무 영향도 |

> 스키마 설계 메모: strict 스키마에서 지원 여부가 SDK·모델 버전마다 갈리는
> `minimum` / `maxItems` 같은 제약 키워드는 쓰지 않는다. LLM에 노출되는 스키마는
> `Literal`과 평범한 배열로만 구성하고, 3줄 보정은 파싱 후 파이썬에서 처리한다.

## 실행

### 로컬

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export OPENAI_API_KEY=sk-...
python main.py --dry-run --limit 3        # 알림 없이 stdout 출력, 캐시도 안 건드림

export TELEGRAM_BOT_TOKEN=123:abc
export TELEGRAM_CHAT_ID=-100...
python main.py                            # 실제 발송 + 캐시 저장
```

옵션:

| 옵션 | 설명 |
| --- | --- |
| `--config PATH` | 설정 파일 경로 (기본 `config.yaml`) |
| `--dry-run` | 알림 대신 stdout 출력, 캐시 저장 안 함 |
| `--limit N` | 이번 실행의 최대 요약 건수 |
| `--source NAME` | 특정 소스만 실행 (반복 지정 가능) |
| `--verbose` | 디버그 로그 |

### GitHub Actions

`Settings → Secrets and variables → Actions`에 등록:

| Secret | 필수 | 용도 |
| --- | --- | --- |
| `OPENAI_API_KEY` | ✅ | 요약 |
| `TELEGRAM_BOT_TOKEN` | ✅ | 알림 |
| `TELEGRAM_CHAT_ID` | ✅ | 알림 대상 채팅 |
| `REDDIT_CLIENT_ID` | ⬜ | Reddit OAuth (아래 참고) |
| `REDDIT_CLIENT_SECRET` | ⬜ | Reddit OAuth |

기본 3시간 주기(`cron: "0 */3 * * *"`, UTC)로 돌고, 실행 후 바뀐
`data/processed_ids.json`을 자동 커밋·푸시한다. Actions 탭에서 수동 실행 시
`dry_run` 체크박스로 발송 없이 동작만 확인할 수 있다.

## 설정 (`config.yaml`)

```yaml
run:
  max_items_per_run: 25   # 한 실행에서 LLM에 넘길 상한 = 비용 상한
  min_importance: 3       # 이 점수 이상만 발송

sources:
  - name: OpenAI News
    type: rss             # core/collectors 에 등록된 수집기 이름
    enabled: true
    options:
      url: https://openai.com/news/rss.xml
      limit: 15
```

비밀 값은 설정 파일에 넣지 않는다. 전부 환경 변수로만 주입한다.

## 소스 추가하기

1. `core/collectors/<name>.py`에 `Collector`를 상속한 클래스를 만들고 `type` 선언
2. `collect()`에서 `RawItem`을 yield (`external_id`는 소스 내에서 유일해야 한다)
3. `core/collectors/__init__.py`에 import 한 줄 추가
4. `config.yaml`의 `sources`에 항목 추가

`type` 선언과 동시에 레지스트리에 등록되므로 그 외 배선은 필요 없다.

## 알아둘 점

- **Reddit 익명 호출**: GitHub Actions 같은 공용 IP에서는 익명 JSON 요청이 403/429로
  막히는 경우가 있다. `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`(script 앱)을
  넣어 두면 application-only OAuth로 붙는다. 없으면 익명으로 시도하고, 실패한
  소스는 건너뛴 뒤 나머지 소스로 계속 진행한다.
- **중복 방지**: 요약에 성공한 항목은 중요도가 임계치 미만이어도 캐시에 기록한다.
  다음 실행에서 같은 글을 다시 요약하는 토큰 낭비를 막기 위해서다. 요약 자체가
  실패한 항목은 기록하지 않으므로 다음 실행에서 재시도된다.
- **RSS 피드 주소**: `config.yaml`의 기본 목록은 예시다. 사이트 개편으로 주소가
  바뀔 수 있으니 `--dry-run`으로 한 번 확인하고 쓰는 것을 권한다.
- **캐시 크기**: `cache.max_entries`를 넘으면 오래된 키부터 잘라낸다. 잘라낸 글이
  피드에 다시 나타나면 재요약될 수 있으므로, 피드 회전 주기보다 넉넉히 잡는다.
