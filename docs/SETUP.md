# 설정 가이드

키를 발급하고 등록하는 절차. 전부 무료 티어로 돌아간다.

키 값을 셸 히스토리나 화면에 남기지 않는 방법을 함께 적었다. 이미 노출했다면
발급처에서 폐기하고 새로 만드는 편이 빠르다.

## 필요한 것

| 용도 | 환경 변수 | 필수 |
| --- | --- | --- |
| 요약 (1순위) | `GEMINI_API_KEY` | 셋 중 최소 하나 |
| 요약 (2순위 폴백) | `GROQ_API_KEY` | 〃 |
| 요약 (유료, 기본 비활성) | `OPENAI_API_KEY` | 〃 |
| Discord 알림 | `DISCORD_WEBHOOK_URL` | 켠 채널만 |
| 이메일 다이제스트 | `SMTP_USER`, `SMTP_PASSWORD` | 켠 채널만 |
| Telegram 알림 | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | 켠 채널만 |
| Reddit Data API | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | 선택 |

알림 채널은 `config.yaml`의 `notifier.channels`에 켜 둔 것만 자격 증명이 필요하다.
없는 채널은 경고만 남기고 건너뛴다.

## 1. Gemini (요약 1순위)

1. https://aistudio.google.com/apikey 접속 후 Google 계정 로그인
2. **Create API key** → 프로젝트 선택 또는 새로 생성
3. 키 복사 (카드 등록 없음)

동작 확인:

```bash
read -rs GEMINI_API_KEY && export GEMINI_API_KEY   # 붙여넣고 Enter, 화면에 찍히지 않는다

curl -s https://generativelanguage.googleapis.com/v1beta/openai/chat/completions \
  -H "Authorization: Bearer $GEMINI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.5-flash-lite","messages":[{"role":"user","content":"ping"}]}'
```

`model_not_found`가 나오면 계정에 열려 있는 모델이 다르다. 아래에서 확인해
`config.yaml`의 `llm.providers[].model`을 맞춘다.

```bash
curl -s "https://generativelanguage.googleapis.com/v1beta/models" \
  -H "X-goog-api-key: $GEMINI_API_KEY" \
| python3 -c "import json,sys; [print(m['name'].replace('models/','')) for m in json.load(sys.stdin)['models'] if 'generateContent' in m.get('supportedGenerationMethods',[])]"
```

무료 티어 한도는 계정·시점마다 다르고 한 번 크게 축소된 적이 있다. 본인 한도는
[AI Studio](https://aistudio.google.com/rate-limit)에서 확인한다. 분당 요청 제한에
걸리면 429 응답이 알려주는 시간만큼 기다렸다가 재시도하며, 그래도 안 되면 다음
provider로 넘어간다.

> 무료 티어는 입력·출력이 Google의 제품 개선에 사용될 수 있다. 공개된 기사만
> 보내는 지금 구성에서는 문제되지 않지만, 비공개 문서를 넣을 계획이면 유료
> 티어로 올리거나 provider를 바꿔야 한다.

## 2. Groq (요약 2순위 폴백)

1. https://console.groq.com 가입 (GitHub/Google 계정, 카드 등록 없음)
2. **API Keys** → **Create API Key** → 이름 입력 후 생성
3. `gsk_`로 시작하는 키 복사 — **이때 한 번만 보인다**

```bash
read -rs GROQ_API_KEY && export GROQ_API_KEY

curl -s https://api.groq.com/openai/v1/chat/completions \
  -H "Authorization: Bearer $GROQ_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"openai/gpt-oss-20b","messages":[{"role":"user","content":"ping"}]}'
```

모델을 바꿀 때 주의할 점: Groq에서 strict JSON 스키마가 보장되는 것은
`openai/gpt-oss-*` 계열뿐이다. 다른 모델을 쓰면 스키마가 거부되어 `json_object`
모드로 강등되는데, 동작은 하지만 출력 안정성이 떨어진다.

무료 티어는 분당 30요청·일 14,400요청이지만 **6,000 TPM**이 병목이라 분당 3건
남짓으로 묶인다. Gemini가 막혔을 때만 쓰는 폴백이라 실사용에 지장은 없다.

## 3. Discord (실시간 알림)

채널 이름 우클릭 → **채널 편집** → **연동** → **웹후크** → **새 웹후크** →
**웹후크 URL 복사**. 봇을 만들 필요도, 채널 ID를 찾을 필요도 없다.

```bash
read -rs DISCORD_WEBHOOK_URL && export DISCORD_WEBHOOK_URL
curl -s -X POST "$DISCORD_WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d '{"content":"insight-refinery 연결 테스트"}'
```

## 4. 이메일 다이제스트

Gmail 기준이다. 2단계 인증을 켠 뒤
[앱 비밀번호](https://myaccount.google.com/apppasswords)를 발급한다.
**계정 비밀번호로는 SMTP 로그인이 되지 않는다.**

- `SMTP_USER`: 앱 비밀번호를 발급받은 계정 주소
- `SMTP_PASSWORD`: 앱 비밀번호 16자, **공백 없이**

구글이 `abcd efgh ijkl mnop`처럼 4자씩 끊어 보여주는 것은 읽기 편하라는 것뿐이다.
값 앞뒤의 공백·개행은 코드가 잘라내지만, 가운데 공백은 비밀번호의 일부일 수 있어
그대로 둔다.

수신자는 `config.yaml`의 `notifier.email.recipients`에 적는다. 비우면
`SMTP_USER` 본인에게 보낸다. 포트는 587(STARTTLS)이 기본이고 465를 적으면
SMTP_SSL로 붙는다.

> Google Workspace 계정은 관리자가 앱 비밀번호를 막아둔 경우가 있다.

## 5. Telegram (선택, 기본 비활성)

1. Telegram에서 **@BotFather** → `/newbot` → 이름과 username 입력
   (username은 `bot`으로 끝나야 한다)
2. 받은 토큰을 `TELEGRAM_BOT_TOKEN`에 넣는다
3. Chat ID를 찾는다. **봇은 먼저 말을 걸어주기 전엔 상대를 모른다.**
   - 개인: `t.me/<봇username>`에서 **Start** 후 아무 메시지나 전송
   - 그룹: 봇을 초대하고 그룹에서 메시지 전송. `chat.id`는 음수다
   - 그룹인데 `result`가 비어 있으면 BotFather `/setprivacy` → **Disable**

```bash
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates" | python3 -m json.tool
```

`result[0].message.chat.id`가 `TELEGRAM_CHAT_ID`다.

## 6. Reddit (선택)

Data API는 Responsible Builder Policy(2026-06-05 개정) 이후 승인을 받아야 한다.
승인 전까지는 인증이 필요 없는 RSS 엔드포인트로 우회하도록 설정돼 있다.

승인을 신청하려면 https://www.reddit.com/prefs/apps 에서 **create another app** →
타입 **script** → redirect uri `http://localhost:8080`. 앱 이름 바로 아래 문자열이
`REDDIT_CLIENT_ID`, `secret` 항목이 `REDDIT_CLIENT_SECRET`이다.

승인이 나면 `config.yaml`에서 해당 소스를 `type: reddit`으로 되돌리는 편이 낫다.
`min_score` 같은 필터를 쓸 수 있다.

## 7. 로컬 실행

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # 값을 채우면 자동으로 읽는다
python main.py --dry-run --limit 3
```

`--dry-run`은 알림을 보내지 않고 stdout에 출력하며 캐시도 저장하지 않는다.
이미 설정된 환경 변수가 `.env`보다 우선한다.

## 8. GitHub Actions 등록

```bash
printf '%s' "$GEMINI_API_KEY" | gh secret set GEMINI_API_KEY
printf '%s' "$GROQ_API_KEY" | gh secret set GROQ_API_KEY
printf '%s' "$DISCORD_WEBHOOK_URL" | gh secret set DISCORD_WEBHOOK_URL
gh secret set SMTP_USER
gh secret set SMTP_PASSWORD

gh secret list
```

`echo` 대신 `printf '%s'`를 쓰는 이유는 끝에 개행을 붙이지 않기 때문이다. 개행이
섞인 값은 목록에서는 멀쩡해 보이면서 인증만 실패해 원인을 찾기 어렵다. 코드에서도
양끝 공백을 잘라내지만 애초에 안 붙이는 편이 낫다.

웹 UI로 하려면 저장소 → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**. 등록 후에는 값을 다시 볼 수 없고 덮어쓰기만 된다.

## 9. 동작 확인

Actions 탭에서 **pipeline** → **Run workflow**. 처음에는 `dry_run`을 켜고 돌려
발송 없이 로그만 본다.

```bash
gh workflow run pipeline.yml -f dry_run=true
gh run watch
```

로그 첫 부분에 provider 체인이 찍히면 키가 정상 인식된 것이다.

```
LLM provider 순서: gemini(gemini-3.5-flash-lite) → groq(openai/gpt-oss-20b)
```

`GEMINI_API_KEY 미설정 → provider 'gemini' 건너뜀`이 뜨면 Secret 이름이 다르거나
등록되지 않은 것이다.

이메일 다이제스트는 `digest_hour`(기본 23 UTC = 08:00 KST) 실행에서만 나간다.
지금 바로 받아보려면 `send_digest`를 켜고 수동 실행한다.

```bash
gh workflow run pipeline.yml -f dry_run=false -f send_digest=true
```
