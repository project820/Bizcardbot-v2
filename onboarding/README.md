# 명함봇 설치 도우미 relay

프로젝트 운영자가 Cloudflare Workers에 설치 도우미 하나를 배포한다.
설치기는 설치 도우미에 pairing을 만들고 관리 봇으로 사용자를 안내한다.
관리 봇은 Bot Management Mode를 켠 운영자 봇을 뜻한다.
예시 관리 봇 username은 `BizcardSetupBot`으로 설정한다.

일반 사용자는 Worker를 직접 배포하지 않는다.
관리 봇이 만든 개인 명함봇의 토큰은 수령 코드 확인 후 전달한다.
명함 이미지 처리와 Google 연락처 저장은 이 Worker가 수행하지 않는다.

## 신뢰 범위

관리 봇 운영자는 `getManagedBotToken`으로 개인 명함봇 토큰을 다시 조회할 수 있다.
이 운영자 권한을 원하지 않으면 설치기의 `--manual` 경로를 사용한다.
relay가 토큰을 저장하지 않아도 운영자의 재조회 권한은 남는다.
사용자가 BotFather에서 토큰을 재발급할 수 있는지는 미확인이다.
재발급이 가능하더라도 관리 권한에 미치는 영향은 별도로 확인해야 한다.

`setManagedBotAccessSettings`는 `is_access_restricted=true`로 시도한다.
이 설정이 실패해도 토큰 전달은 계속한다.
설치기는 `access_restricted=false`를 확인하고 사용자에게 알린다.

Workers와 Durable Objects의 무료 요청 한도는 각각 일 100,000건이다.
설치기는 3초 간격으로 조회하므로 600초 동안 상태 조회만 최대 200건이다.
생성과 webhook, 수령과 ack 요청도 한도에 포함된다.
운영자는 배포 시점의 무료 한도와 추가 요금을 다시 확인한다.
유료 전환은 이 구현의 전제가 아니다.
이 저장소의 테스트는 로컬에서 실행하며 실제 Telegram API를 호출하지 않는다.

## 운영자 배포 절차

다음 절차는 운영자가 별도로 실행한다.
이 구현 작업에서는 로그인과 배포를 실행하지 않았다.

1. BotFather에서 관리 봇을 생성하고 토큰을 안전하게 보관한다.
2. BotFather Mini App에서 해당 봇의 Bot Management Mode를 켠다.
3. 아래 `getMe` 확인 코드로 `can_manage_bots=true`를 확인한다.
4. `onboarding/`에서 의존성을 설치하고 테스트를 실행한다.
5. `wrangler.toml`의 `MANAGER_BOT_USERNAME`을 실제 username으로 변경한다.
6. Cloudflare에서 관리하는 도메인을 연결하고 Wrangler에 로그인한다.
7. 관리 봇 토큰과 webhook secret을 Worker secret으로 등록한다.
8. Worker를 배포하고 webhook을 등록한다.
9. 설치기의 `BASE`를 배포한 HTTPS 주소로 설정한다.

환경 변수 입력 예시는 Bash에서 실행한다.
입력한 토큰과 secret은 터미널에 표시하지 않는다.
셸의 명령 추적 기능은 끈 상태로 실행한다.

```bash
set +x
read -rsp '관리 봇 토큰: ' MANAGER_BOT_TOKEN
printf '\n'
export MANAGER_BOT_TOKEN
python3 - <<'PY'
import json, os, urllib.request
try:
    url = 'https://api.telegram.org/bot' + os.environ['MANAGER_BOT_TOKEN'] + '/getMe'
    with urllib.request.urlopen(url, timeout=15) as response:
        result = json.load(response)
    assert result.get('ok') and result.get('result', {}).get('can_manage_bots') is True
    print('can_manage_bots=true 확인')
except Exception:
    raise SystemExit('관리 봇 권한을 확인하지 못했다. BotFather 설정을 확인한다.') from None
PY
```

로컬 검증과 Cloudflare 로그인은 다음 명령으로 실행한다.

```bash
cd onboarding
npm install --no-audit --no-fund
npx vitest run
npx tsc --noEmit
npx wrangler login
```

`workers_dev=false`이므로 기본 `workers.dev` 주소를 사용하지 않는다.
다음 사용자 도메인 예시를 `wrangler.toml`에 추가한다.
`setup.example.com`은 운영자가 소유한 실제 도메인으로 바꾼다.

```toml
[[routes]]
pattern = "setup.example.com"
custom_domain = true
```

관리 봇 username에는 `@`를 붙이지 않는다.
관리 봇 토큰을 `[vars]`나 저장소 파일에 넣지 않는다.
아래 대화형 명령에 관리 봇 토큰을 입력한다.

```bash
npx wrangler secret put MANAGER_BOT_TOKEN
```

webhook secret은 32바이트 이상 무작위 값으로 별도 생성한다.
Telegram의 secret 형식인 `[A-Za-z0-9_-]{1,256}`을 사용한다.
비밀번호 관리자 등에 저장하고 다음 명령에 입력한다.

```bash
npx wrangler secret put MANAGER_WEBHOOK_SECRET
npx wrangler deploy
```

webhook 등록 시 Worker에 등록한 것과 같은 secret을 입력한다.
`BASE`에는 마지막 `/`를 제외한 HTTPS 주소를 입력한다.
`allowed_updates`에는 `message`와 `managed_bot`을 모두 포함한다.

```bash
read -rp '설치 도우미 HTTPS 주소: ' BASE
read -rsp 'Webhook secret: ' MANAGER_WEBHOOK_SECRET
printf '\n'
export BASE MANAGER_WEBHOOK_SECRET
python3 - <<'PY'
import json, os, urllib.request
try:
    base = os.environ['BASE'].rstrip('/')
    assert base.startswith('https://')
    body = json.dumps({
        'url': base + '/telegram/webhook',
        'secret_token': os.environ['MANAGER_WEBHOOK_SECRET'],
        'allowed_updates': ['message', 'managed_bot'],
    }).encode()
    url = 'https://api.telegram.org/bot' + os.environ['MANAGER_BOT_TOKEN'] + '/setWebhook'
    request = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)
    assert result.get('ok') and result.get('result') is True
    print('Webhook 등록 완료')
except Exception:
    raise SystemExit('Webhook 등록에 실패했다. 주소와 secret을 확인한다.') from None
PY
unset MANAGER_BOT_TOKEN MANAGER_WEBHOOK_SECRET
```

## 설치기 계약 v2

pairing 유효기간은 생성 시점부터 600초다.
`/start`와 수령 재시도는 유효기간을 늘리지 않는다.
모든 POST 요청은 `Content-Type: application/json`을 사용한다.

1. `POST {BASE}/v1/pairings`에 `{"bot_name":"내 명함봇"}`을 보낸다.
2. `201` 응답의 `poll_token`은 설치기 메모리에 보관한다.
3. `deep_link`를 열고 관리 봇에서 시작 버튼을 누른다.
4. `내 명함봇 만들기` 버튼이나 별도 보조 링크로 봇을 만든다.
5. `POST {BASE}/v1/pairings/status`에 `{}`를 보내 3초 간격으로 조회한다.
6. `created` 상태에서 Telegram이 보낸 6자리 수령 코드를 입력한다.
7. `POST {BASE}/v1/pairings/claim`에 `{"code":"123456"}`을 보낸다.
8. `ready` 응답의 토큰을 설치기가 안전하게 저장한다.
9. `POST {BASE}/v1/pairings/ack`에 `{}`를 보내 설치 완료를 확인한다.

`status`, `claim`, `ack` 요청은 `Authorization: Bearer <poll_token>`을 사용한다.
설치기가 토큰을 받았더라도 ack 전에는 TTL 안에서 claim을 재시도할 수 있다.
재시도에도 올바른 코드를 보내야 한다.
`status`는 토큰을 반환하거나 수령을 확정하지 않는다.

생성 응답은 아래 네 필드만 포함한다.
`pairing_id`는 `deep_link` 안에만 있으며 별도 필드로 반환하지 않는다.

```json
{
  "poll_token": "<32바이트 무작위 base64url>",
  "suggested_username": "bizcard_abc123_bot",
  "deep_link": "https://t.me/BizcardSetupBot?start=<pairing_id>",
  "expires_at": 1800000600
}
```

| 경로 | HTTP | 상태와 동작 |
| --- | --- | --- |
| `status` | 200 | `pending`, `started`, `created` 중 현재 상태를 반환한다. |
| `claim` | 200 | 코드가 맞으면 `ready`와 아래 토큰 정보를 반환한다. |
| `claim` | 403 | `bad_code`와 `attempts_left`를 반환한다. |
| `claim` | 409 | 생성 전이면 현재 `pending` 또는 `started` 상태를 반환한다. |
| `ack` | 200 | `claimed`를 반환하고 개인 데이터를 제거한다. |
| `ack` | 409 | 성공한 claim이 없으면 현재 상태를 반환한다. |
| 세 경로 | 410 | `expired` 또는 `claimed`를 반환한다. |
| 세 경로 | 404 | 모르는 토큰이나 잘못된 Bearer 형식을 거절한다. |

코드 5회 실패 시 pairing을 무효화하고 `410 {"status":"expired"}`를 반환한다.
코드 누락과 잘못된 형식도 실패 횟수에 포함한다.
ack 뒤 재요청은 `410 {"status":"claimed"}`를 반환한다.

```json
{
  "status": "ready",
  "bot_token": "<개인 명함봇 토큰>",
  "bot_id": 123,
  "bot_username": "my_card_bot",
  "owner_user_id": 456,
  "access_restricted": true
}
```

`bot_name`은 1~64자로 제한하며 빈 이름과 제어 문자를 거절한다.
앞뒤 공백을 제거하고 `parse_mode` 없이 Telegram에 전달한다.
`GET /`는 한국어 안내문을 반환한다.
Telegram 업데이트는 `POST /telegram/webhook`에서만 처리한다.

## 저장과 보안

SQLite Durable Object 바인딩 이름은 `PAIRINGS`다.
모든 정상 요청을 하나의 `pairings-v1` 객체로 보낸다.
객체 입력 게이트가 결합과 코드 검증, 수령 상태 변경을 직렬화한다.
저장소 트랜잭션으로 pairing과 조회 인덱스를 함께 갱신한다.

Worker는 외부 본문을 5초 안에 모두 읽고 JSON 객체로 검증한다.
일반 API 본문은 1,024바이트, webhook은 65,536바이트로 제한한다.
검증된 값만 새 JSON 봉투로 직렬화해 DO에 전달한다.
외부 요청 스트림과 인증 헤더는 DO에 그대로 전달하지 않는다.
DO도 내부 봉투를 입력 게이트 밖에서 파싱한다.
느린 외부 본문 하나가 다른 사용자의 상태 조회를 막지 않는다.

`poll_token`은 SHA-256 해시만 저장하며 해시로 pairing을 찾는다.
link 인덱스는 `/start` 결합에만 사용한다.
수령 코드는 무작위 salt를 더한 SHA-256 해시만 저장한다.
코드 원문과 개인 명함봇 토큰은 저장하지 않는다.
`getManagedBotToken(user_id=bot_id)`은 올바른 코드로 claim할 때만 호출한다.
접근 제한 API도 claim에서 호출하며 실패 여부를 설치기에 알린다.
같은 코드를 재수령해도 토큰은 Telegram에서 다시 조회한다.

ack와 만료 처리에서 이름과 사용자 정보를 제거한다.
봇 ID, 코드 해시, link 인덱스와 owner 인덱스도 함께 제거한다.
`410` 응답을 위한 해시 키와 상태, 만료·정리 시각만 남긴다.
최초 만료 시점부터 24시간 뒤 alarm이 이 기록을 삭제한다.
그 뒤 해당 토큰은 `404`를 반환한다.
요청 시에도 만료를 검사하므로 alarm 지연이 유효기간을 늘리지는 않는다.

`/start`는 private chat의 `message.from.id == message.chat.id`만 허용한다.
다른 사용자에게 결합된 링크에는 `이미 사용된 링크` 안내를 보낸다.
같은 사용자가 새 pairing을 연결하면 이전 pairing은 `expired`가 된다.
생성 주 신호는 결합 사용자의 private `message.managed_bot_created`다.
보조 `managed_bot` 신호는 결합 사용자가 일치하고 상태가 `started`일 때만 받는다.
이 보조 신호의 `bot.username`은 pairing의 `suggested_username`과 대소문자를 무시하고 일치해야 한다.
결합 사용자가 보조 링크에서 핸들을 바꿔 만들었다면 설치를 다시 시작하고 새 제안 핸들로 재시도한다.
`created` 이후 생성 중복과 `managed_bot` 토큰·소유자 변경은 무시한다.
상태 전이가 중복 코드 전송을 막는다.

코드 전송이 실패하면 상태를 `started`로 유지해 webhook 재시도를 허용한다.
전송 성공 뒤 코드 해시와 `created` 상태를 기록한다.
전송 직후 저장 전에 장애가 나면 재시도에서 새 코드가 전송될 수 있다.
이 경우 설치기는 마지막으로 받은 코드를 사용한다.
ack는 개인 데이터를 지운 뒤 완료 안내와 키보드 제거를 전송한다.
완료 안내가 실패하더라도 삭제를 취소하지 않는다.
이 안내는 자동 재전송하지 않으므로 설치기에서도 완료를 표시해야 한다.

webhook secret은 Web Crypto HMAC 검증으로 상수시간 비교한다.
발신 주소는 Cloudflare의 `CF-Connecting-IP`로 검사한다.
허용 대역은 `149.154.160.0/20`과 `91.108.4.0/22`다.
`TELEGRAM_IP_CHECK="1"`이 기본이며 생략해도 검사를 수행한다.
검사를 의도적으로 끄려면 `TELEGRAM_IP_CHECK="0"`으로 설정한다.
secret 검사는 이 설정과 관계없이 유지한다.
일반 프록시에서 클라이언트가 넣은 같은 이름의 헤더를 신뢰하면 안 된다.

생성 속도 제한도 Cloudflare의 `CF-Connecting-IP`를 기준으로 한다.
IP 해시별 DO 카운터로 첫 생성부터 10분 동안 10회를 허용한다.
IP 헤더가 없으면 공통 제한 묶음을 사용한다.
카운터는 객체 재시작 후에도 유지한다.
초과 요청에는 `429`와 `--manual` 안내를 반환한다.
Worker 실행 자체도 요청으로 계산되므로 이 제한이 무료 한도 소진을 막지는 못한다.

애플리케이션은 요청과 예외를 로그로 출력하지 않는다.
Wrangler 로그와 추적 수집도 명시적으로 비활성화한다.
운영자는 별도 프록시 로그에도 토큰과 코드, deep link를 기록하지 않는다.
상태 조회 URL에는 pairing ID가 들어가지 않는다.
Telegram 호출은 주입 가능한 `Transport`를 사용하며 호출당 5초로 제한한다.

## 검증 범위와 남은 확인

`test/relay.test.ts`는 메모리 저장소로 DO 입력 게이트와 트랜잭션을 모사한다.
외부 `fetch`는 실패하도록 막고 Telegram 전송에는 가짜 응답을 주입한다.
다음 명령으로 로컬 검증한다.

```bash
cd onboarding
npx vitest run
npx tsc --noEmit
```

실제 Cloudflare SQLite 런타임과 Telegram 연동은 미검증이다.
배포 전 실제 런타임에서 정상 연결과 병렬 수령을 확인한다.
모바일·데스크톱의 생성 버튼과 보조 링크도 실제 계정으로 확인한다.
`managed_bot`은 토큰·소유자 변경에도 전달되는 이벤트다.
보조 신호의 핸들 일치 검사로 제안 핸들과 다른 기존 봇의 변경을 제외한다.
핸들 일치는 생성 자체를 증명하지 않으며 별도 토큰 교체는 수행하지 않는다.
운영자가 토큰을 재조회할 수 있는 권한은 남는다.

개발 의존성 버전은 `../cloud/package.json`과 동일하게 유지한다.
새 production dependency는 추가하지 않는다.
