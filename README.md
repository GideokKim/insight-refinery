# insight-refinery

SNS·RSS·뉴스레터 등 여러 소스에서 피드를 모아 LLM으로 **정형화 요약 / 분류 /
중요도 채점**한 뒤, 중요한 것만 골라 알림으로 보내는 범용 데이터 파이프라인.

현재 도메인은 **최신 AI 소식·트렌드**지만, 도메인 지식은 `config.yaml`(소스 목록)과
`core/processor.py`(프롬프트·카테고리)에만 몰려 있어 다른 주제로 바꾸기 쉽다.

## 파이프라인

```
collectors → dedup → processor(LLM) → 임계치 필터 → notifier
   RSS         JSON     Structured        중요도 N점    Discord/Email/
   Reddit      캐시      Outputs           이상만        Telegram
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
├── .github/workflows/
│   ├── pipeline.yml                # cron 실행 + 상태 자동 커밋
│   └── ci.yml                      # push·PR마다 테스트
├── core/
│   ├── collectors/                 # 소스별 수집기
│   │   ├── base.py                 #   Collector 인터페이스 + RawItem + 레지스트리
│   │   ├── rss.py                  #   RSS/Atom
│   │   └── reddit.py               #   Reddit (익명 JSON / 선택적 OAuth)
│   ├── config.py                   # config.yaml 로딩·검증 (Pydantic)
│   ├── dedup.py                    # 중복 판정 (ID·URL·제목) + 캐시
│   ├── digest.py                   # 이메일 다이제스트 대기열
│   ├── processor.py                # Pydantic 스키마 + LLM 구조화 요약 + provider 폴백
│   └── notifier.py                 # 알림 채널 (Discord / Email / Telegram)
├── data/                           # 실행 간 상태 (커밋 대상)
│   ├── processed_ids.json          #   중복 방지 캐시
│   └── digest_queue.json           #   발송 대기 중인 요약
├── tests/                          # pytest (네트워크·자격 증명 불필요)
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

## LLM provider

무료 티어를 우선 쓰고, 실패하면 다음으로 넘어가는 폴백 체인이다. 전부 OpenAI
호환 엔드포인트라 `config.yaml`의 `llm.providers`에 세 줄 추가하면 provider가
늘어난다. **API 키 환경 변수가 없는 provider는 조용히 건너뛴다.**

| 순서 | provider | 모델 | 키 | 비고 |
| --- | --- | --- | --- | --- |
| 1 | Gemini | `gemini-3.5-flash-lite` | `GEMINI_API_KEY` | 무료 티어. TPM 여유가 커서 스로틀링이 거의 없다 |
| 2 | Groq | `openai/gpt-oss-20b` | `GROQ_API_KEY` | 무료 티어(카드 불필요, 30 RPM / 14,400 RPD). 단 6K TPM이 병목 |
| 3 | OpenAI | `gpt-4o-mini` | `OPENAI_API_KEY` | 기본은 주석 처리. 유료지만 가장 안정적 (이 워크로드 기준 월 $2~3) |

무료 티어 한도는 계정·시점마다 다르다. Gemini는 [AI Studio](https://aistudio.google.com/rate-limit)에서
본인 한도를 직접 확인해야 하고, 한 번 크게 축소된 전례가 있다. Groq을 폴백에
같이 켜 두는 이유가 이것이다.

### 구조화 출력 폴백

provider마다 strict JSON 스키마 지원이 다르다. 기본은 `json_schema` 모드로
호출하고, provider가 400으로 거부하면 그 provider만 `json_object` 모드로
낮춰 재시도한다(스키마를 프롬프트에 넣고 응답을 직접 검증). 강등은 프로세스가
사는 동안 유지되므로 매 건마다 400을 다시 맞지 않는다.

> Groq에서 strict 스키마가 보장되는 모델은 `openai/gpt-oss-*` 계열뿐이라
> 폴백 모델로 그중 하나를 골랐다.

## 실행

### 로컬

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export GEMINI_API_KEY=...          # 또는 GROQ_API_KEY
python main.py --dry-run --limit 3        # 알림 없이 stdout 출력, 캐시도 안 건드림

export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
python main.py                            # 실제 발송 + 캐시 저장
```

옵션:

| 옵션 | 설명 |
| --- | --- |
| `--config PATH` | 설정 파일 경로 (기본 `config.yaml`) |
| `--dry-run` | 알림 대신 stdout 출력, 캐시 저장 안 함 |
| `--limit N` | 이번 실행의 최대 요약 건수 |
| `--source NAME` | 특정 소스만 실행 (반복 지정 가능) |
| `--send-digest` | 발송 시각이 아니어도 대기열을 지금 보낸다 |
| `--verbose` | 디버그 로그 |

### GitHub Actions

`Settings → Secrets and variables → Actions`에 등록:

| Secret | 필수 | 용도 |
| --- | --- | --- |
| `GEMINI_API_KEY` | ▲ | 요약 (1순위) |
| `GROQ_API_KEY` | ▲ | 요약 (2순위 폴백) |
| `OPENAI_API_KEY` | ⬜ | 요약 (유료, 기본 비활성) |
| `DISCORD_WEBHOOK_URL` | ▲ | 알림 (Discord) |
| `SMTP_USER` / `SMTP_PASSWORD` | ▲ | 알림 (이메일) |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | ▲ | 알림 (Telegram, 기본 비활성) |
| `REDDIT_CLIENT_ID` | ⬜ | Reddit OAuth (아래 참고) |
| `REDDIT_CLIENT_SECRET` | ⬜ | Reddit OAuth |

LLM 키(▲)는 최소 하나가 필요하며, 하나도 없으면 시작 시점에 에러로 멈춘다.
알림 키(▲)는 `notifier.channels`에 켜 둔 채널 것만 있으면 되고, 하나도 없으면
발송 대신 콘솔 출력으로 떨어진다.

기본 3시간 주기(`cron: "0 */3 * * *"`, UTC)로 돌고, 실행 후 바뀐
`data/processed_ids.json`을 자동 커밋·푸시한다. Actions 탭에서 수동 실행 시
`dry_run` 체크박스로 발송 없이 동작만 확인할 수 있다.

## 신선도

피드는 최신순 N개를 준다. 그런데 게시 빈도가 낮은 블로그는 그 N개가 몇 달 전까지
닿는다. Google Research 피드의 상위 15개는 두 달 전 글까지 포함한다. 소스를 새로
켜면 그 과거분이 통째로 "신규"로 들어와 알림으로 나가고, 뒤늦게 과거 날짜로
올라오는 글도 있다. 실제로 54일 지난 글이 발송된 적이 있다.

`run.max_age_days`(기본 5)보다 오래된 항목은 수집 단계에서 버린다. LLM 호출 전이라
토큰도 아낀다. 발행 시각이 없는 항목은 판단할 수 없으니 남긴다 — 날짜를 주지 않는
피드를 통째로 잃는 것이 더 나쁘다.

**3~4일로 좁히면 안 된다.** 금요일 오후 기사는 월요일 정오에 이미 70시간이 지나
있어서, 주말에 글을 올리지 않는 매체(OpenAI, Ars Technica 등)가 통째로 0건이 된다.
주간 뉴스레터처럼 주기가 긴 소스는 `options.max_age_days`로 창을 넓힌다.

## 중복 판정

같은 뉴스가 반복해서 오는 경로가 셋이라, 세 기준으로 각각 막는다. 판정은 LLM
호출 **전에** 이뤄지므로 토큰도 함께 아낀다.

| 기준 | 막는 상황 | 예 |
| --- | --- | --- |
| ID | 같은 피드에 같은 글이 다시 올라옴 | 같은 RSS 엔트리 |
| URL | 같은 기사를 여러 소스가 나름 | TechCrunch 기사가 HN에 올라옴 |
| 제목 유사도 | 같은 사건을 여러 매체가 각자 씀 | TechCrunch와 VentureBeat의 같은 소식 |

ID만으로는 부족하다. HN 항목의 ID는 `news.ycombinator.com/item?id=...`이고 실제
기사 링크는 따로이며, TechCrunch의 ID는 `?p=3153830` 꼴이라 기사 URL과 또 다르다.
그래서 URL은 스킴·`www.`·추적 파라미터(`utm_*`, `ref` 등)·파라미터 순서를
정규화해서 비교한다.

제목 판정은 `cache.similarity_threshold`(기본 0.85)로 조절한다. 낮추면 더 공격적으로
묶어 오탐(다른 소식을 같은 것으로 간주)이 늘고, `0`이면 제목 판정만 꺼진다
(ID·URL 판정은 계속 동작).

같은 실행 안에서 앞서 통과한 아이템과도 비교하므로, 한 번에 여러 소스에서 같은
기사가 들어와도 하나만 남는다.

## 알림 채널

`notifier.channels`에 나열한 **모든** 채널로 보낸다. 자격 증명이 없는 채널은
경고만 남기고 빠지므로, 채널 하나가 미설정이라고 실행이 죽지 않는다. 쓸 수 있는
채널이 하나도 없으면 콘솔로 떨어뜨린다 — 이미 끝낸 LLM 작업을 잃지 않기 위해서다.

| 채널 | 필요한 환경 변수 | 발송 주기 | 기본 임계치 |
| --- | --- | --- | --- |
| `discord` | `DISCORD_WEBHOOK_URL` | 실행마다 (embed 10개씩 묶음) | 3점 |
| `email` | `SMTP_USER`, `SMTP_PASSWORD` | 하루 1회 다이제스트 | 4점 |
| `telegram` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 실행마다 (건당 1메시지) | `run.min_importance` |

### 채널별 임계치

`notifier.min_importance`로 채널마다 다르게 준다. 실시간 채널은 넓게 받고
다이제스트는 엄선하는 식이다. 지정하지 않은 채널은 `run.min_importance`를 쓴다.

```yaml
notifier:
  min_importance:
    discord: 3
    email: 4
```

### 이메일 다이제스트가 동작하는 방식

배치는 3시간마다 도는데 메일은 하루 한 통만 받고 싶다. 그런데 처리한 아이템은
중복 방지 캐시에 기록돼 다시 요약되지 않으므로, 발송 시각까지 **요약 결과 자체를
들고 있어야** 한다. 그 보관소가 `data/digest_queue.json`이다.

- `digest_hour`가 아닌 실행 → 임계치를 넘긴 항목을 큐에 쌓기만 한다
- `digest_hour` 실행 → 큐에 쌓인 전부를 메일 한 통으로 보내고 큐를 비운다
- 발송에 **성공했을 때만** 비우므로, SMTP가 죽어 있으면 다음 실행에서 재시도한다

`digest_hour`는 UTC이고 **cron이 실제로 도는 시각 중 하나여야 한다.** 기본값 21은
21:00 UTC = 06:00 KST 아침 브리핑이다. 지금 바로 받아보려면 `--send-digest`를 쓴다.

### Discord 웹훅 만들기

채널 이름 우클릭 → **채널 편집** → **연동** → **웹후크** → **새 웹후크** →
**웹후크 URL 복사**. 봇 생성도 chat_id 조회도 필요 없다.

### 이메일(SMTP)

Gmail이라면 2단계 인증을 켠 뒤 [앱 비밀번호](https://myaccount.google.com/apppasswords)를
발급해 `SMTP_PASSWORD`에 넣는다. **계정 비밀번호로는 로그인되지 않는다.**
수신자는 `config.yaml`의 `notifier.email.recipients`에 적고, 비워 두면
`SMTP_USER` 본인에게 보낸다. 포트는 587(STARTTLS)이 기본이며 465를 적으면
SMTP_SSL로 붙는다.

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

## 테스트

```bash
pip install -r requirements-dev.txt
pytest
```

외부 호출은 전부 대역으로 바꾸므로 네트워크도 API 키도 필요 없고, 전체가 1초 안에
끝난다. push와 PR마다 CI에서 같은 명령이 돈다.

`tests/test_digest.py`와 `tests/test_pipeline.py`의 일부는 실제로 메일이 나가지
않았던 두 사고를 고정한 것이다. 하나는 이번 실행에 해당 항목이 없으면 채널을
통째로 건너뛰어 대기열이 남은 건이고, 다른 하나는 UTC 날짜로 발송 여부를 판단해
낮에 수동 발송을 하면 그날 저녁 정기 발송이 막힌 건이다.

## 알아둘 점

- **Reddit**: Data API는 Responsible Builder Policy(2026-06-05 개정) 이후 승인을
  받아야 쓸 수 있고, 실제로 CI 공용 IP에서 익명 JSON 요청은 403으로 막혔다.
  그래서 기본 설정은 인증이 필요 없는 **RSS 엔드포인트**(`/r/<sub>/top/.rss`)로
  우회한다. 이쪽도 연속 요청에 429가 잦지만 몇 초 뒤엔 통과하므로 수집기가
  재시도한다. 승인을 받으면 `type: reddit`으로 되돌리는 편이 낫다 — `min_score`
  같은 필터를 쓸 수 있다 (`REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` 필요).
- **RSS 수집 재시도**: 429·5xx는 `Retry-After`를 존중해 재시도하고, 다 실패하면
  예외를 던진다. 거절 응답도 본문만 비어 있을 뿐 파싱은 성공하므로, 그냥 두면
  "0건 수집"으로 보여 실패가 성공처럼 묻힌다.
- **중복 방지**: 요약에 성공한 항목은 중요도가 임계치 미만이어도 캐시에 기록한다.
  다음 실행에서 같은 글을 다시 요약하는 토큰 낭비를 막기 위해서다. 요약 자체가
  실패한 항목은 기록하지 않으므로 다음 실행에서 재시도된다.
- **RSS 피드 주소**: `config.yaml`의 기본 목록은 예시다. 사이트 개편으로 주소가
  바뀔 수 있으니 `--dry-run`으로 한 번 확인하고 쓰는 것을 권한다.
- **무료 티어 쿼터**: Gemini 무료 티어는 **분당 15요청**이라 한 번에 25건을 돌리면
  중간에 429가 난다. 429 응답에는 서버가 알려주는 정확한 대기 시간이 들어 있어,
  그 값만큼 기다렸다가 재시도한다(`llm.max_retry_delay`로 상한). 고정 백오프로는
  분당 제한을 넘길 수 없어서다. SDK 내장 재시도는 백오프가 1초 미만이라 끄고
  직접 처리한다. Groq은 6,000 TPM이 병목이라 주력이 되면 `max_items_per_run`을
  10 정도로 낮추는 편이 실행 시간(job timeout 20분) 면에서 안전하다.
- **캐시 크기**: `cache.max_entries`를 넘으면 오래된 키부터 잘라낸다. 잘라낸 글이
  피드에 다시 나타나면 재요약될 수 있으므로, 피드 회전 주기보다 넉넉히 잡는다.
