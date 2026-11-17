# Bizcardbot-v2

명함 사진을 Telegram으로 보내면 선택한 연락처 계정에 저장하는 봇이다.
내 Mac에서 실행하고, 저장 전에 초안을 보여 준다.
필요한 것은 켜 둔 Mac과 Telegram이다.

ChatGPT나 Claude 구독, 또는 별도 과금되는 OpenAI API 키도 필요하다.
Mac 연락처에서 Google 계정을 선택하면 Google Cloud 설정 없이 동기화한다.
기존 무료 `gws` 직접 연결도 지원한다.

처음 설치는 [Quick start](#quick-start)를 따른다.
v1 사용자는 [업그레이드 방법](CHANGELOG.md)을 따른다.

현재 버전: **2.1.0**

## 새 소식 (What's new)

- **v2.1.0**은 Mac 연락처 저장과 계정 선택을 추가한다.
  `python3 -m bizcard_intake setup-contacts`로 연결 방식을 고른다.

- **v2.0.0**은 새 저장소로 옮긴 첫 정식 버전이다.
- 명함 방향을 자동으로 바로잡는다. 명함 17장을 네 방향으로 돌린 68건을 모두 바로잡았다.
  다른 사진에서는 결과가 다를 수 있다.
- 연락처 사진의 종이색을 스캐너처럼 하얗게 맞춘다. 푸르거나 누렇게 찍힌 명함도 깨끗하게 저장된다.
  금색·검정처럼 진한 색 명함은 그대로 둔다.
- 명함 1장 처리 시간은 중앙값 약 4.5초→4.0초로 줄었다(17장 1회 측정, 네트워크 변동 포함).
  두 번째 확인 모델(Haiku)의 응답을 짧게 줄였다(중앙값 3.4초→2.8초).
  17장 비교에서 연락처 값 손실은 관측되지 않았다.
- `setup-telegram`으로 봇 생성과 허용 사용자 설정을 안내한다. 수동 경로도 지원한다.
- 봇 시작 스크립트와 CLI는 `.env`를 설정 값으로만 읽는다.
- 같은 봇이 다른 곳에서도 실행되면 봇 사용자에게 Telegram 메시지로 알린다.
  토큰 도용일 때도 같은 알림이 온다.
- Cloud 미리보기는 배포 전이다. 변경과 이전 방법은 [CHANGELOG](CHANGELOG.md)를 확인한다.

기여자는 [버전 관리와 릴리스 규칙](RELEASING.md)을 따른다.
`python3 scripts/release.py history`는 Git 전체 이력의 개인정보 패턴과 이미지 파일을 검사한다.

```
photo ─▶ Apple Vision rectify + OCR ─▶ codex / claude (JSON) ─▶ draft in Telegram
                                                                    │ reply to fix, 승인 to save
                                                                    ▼
                                                        Mac Contacts / Google Contacts (with card photo)
```

## Prerequisites

| Requirement | Why | Check |
|---|---|---|
| macOS with Xcode command line tools | Apple Vision does the card rectification and OCR | `xcode-select --install` |
| Python 3.9+ with `numpy`, `Pillow` (`opencv-python` optional) | image preparation | `python3 -m pip install -r requirements.txt` |
| Optional: [`gws`](https://github.com/googleworkspace/cli), contacts scope | advanced Google direct backend only | `brew install googleworkspace-cli` |
| **One of**: [`codex`](https://github.com/openai/codex) (ChatGPT Plus/Pro/Business) or [`claude`](https://docs.anthropic.com/en/docs/claude-code) (Claude subscription) | reads the card | `codex login` / `claude login` |
| Telegram bot connection | the chat channel | `python3 -m bizcard_intake setup-telegram` |

`python3 -m bizcard_intake doctor` checks all of the above and prints the fix for
anything missing. `python3 -m bizcard_intake setup` walks you through the
missing pieces.

## Quick start

```bash
git clone https://github.com/project820/Bizcardbot-v2.git && cd Bizcardbot-v2
python3 -m pip install -r requirements.txt
cp .env.example .env
export GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file   # before any gws call, see below
```

Then, in order:

1. Run `python3 -m bizcard_intake setup-telegram` and follow the Korean prompts.
   Enter a display name (Enter keeps `명함비서`), then open the Telegram link and
   create your bot. Enter the six-digit code sent in Telegram into the installer.
   The installer saves its token and your allowed chat ID in
   `.env` with owner-only permissions. Existing allowed chats are kept.
2. Add `OPENAI_API_KEY=` to `.env` if you want the API parser.
3. Run `python3 -m bizcard_intake setup` to choose a contacts backend and check the parser.
   CLI commands read the project's `.env` as data; process environment values take precedence.
4. Start the bot: `./scripts/start_bot.sh`. The script reads `.env` automatically.

### Telegram setup

```bash
python3 -m bizcard_intake setup-telegram
```

자동 설정은 프로젝트 운영자가 운영하는 설치 도우미(@BizcardSetupBot)를 거친다.
도우미는 내 봇의 토큰을 저장하지 않는다.
도우미는 명함 사진과 Google 계정에 접근하지 않는다.
다만 Telegram 구조상 도우미 운영자는 내 봇의 토큰을 다시 조회할 수 있다.
토큰이 있으면 봇에 온 메시지를 읽을 수 있다.
이 점이 걸리면 `python3 -m bizcard_intake setup-telegram --manual`로 직접 만든다.
수동 경로는 도우미를 거치지 않는다.

1. 봇 표시 이름을 입력한다. Enter를 누르면 `명함비서`를 쓴다.
2. 표시된 Telegram 링크를 열고 @BizcardSetupBot인지 확인한다.
3. 도우미 메시지의 `▶ 명함봇 만들기` 버튼을 누르고 봇을 만든다. 제안된 봇 아이디는 바꾸지 않는다.
4. 도우미가 보낸 6자리 코드를 터미널에 입력한다.
5. 설치기가 봇 정보와 허용 사용자를 확인하고 `.env`에 저장했는지 확인한다.

설치기는 최대 10분 동안 3초 간격으로 확인한다.
코드 입력을 5회 틀리면 연결이 만료된다.
`BIZCARD_ONBOARDING_URL`은 프로세스 환경 변수, 대상 `.env` 순으로 읽는다.
미설정이면 기본 도우미를 쓰고, 빈 값이면 BotFather 수동 경로를 쓴다.
연결 실패나 만료 시에도 수동 경로를 안내한다.
토큰 입력은 숨기며 화면에 출력하지 않는다.
기존 연결을 바꾸려면 확인을 받는다. `--yes`는 이 확인을 생략한다.
`--no-open`은 링크를 자동으로 열지 않는다.
선택 모듈 `qrcode`가 이미 설치되어 있으면 터미널 QR 코드도 표시한다.

새 봇 생성 직후 확인이 실패하면 설치기는 최대 3회 시도한다.
그래도 실패해 설치를 다시 실행하면 봇이 하나 더 만들어진다.
마지막 수령 확인이 3회 실패하면 오류로 종료하지만 저장한 연결은 유지한다.

문제가 있을 때만 아래 명령으로 점검한다. 프로젝트의 `.env`를 데이터로 읽는다.
Google 로그인 안내가 필요하면 `doctor`를 `setup`으로 바꾼다.

```sh
python3 -m bizcard_intake doctor
```

<details>
<summary>Appendix: manual Telegram setup</summary>

Run `python3 -m bizcard_intake setup-telegram --manual`.

1. Open the official `@BotFather` in Telegram and send `/newbot`.
2. Choose the display name and a username ending in `bot`.
3. Paste the token into the installer's hidden prompt. On macOS, the installer
   checks `pbpaste` for a token first, verifies its bot username, and asks before
   using it. Declining uses the hidden prompt; no token is printed.
4. Send the installer's random confirmation word (for example, `명함-4821abcdef12`)
   exactly as displayed in a private conversation with that bot. The installer
   waits up to 120 seconds and checks every 3 seconds. Only a matching message
   dated at or after the current installation started can register its sender.
   Old updates and unrelated messages are skipped while advancing the offset.

A pre-existing webhook, polling failure, or timeout stops setup without saving
an unverified chat ID. The installer does not remove the webhook. Coordinate
with the existing operator to remove it, stop other polling processes, and
rerun `setup-telegram --manual`. Do not paste tokens into chat messages or share
them.

The bot stores its bot ID and update offset in `data/cards/_telegram.state`.
Switching to another bot resets the offset on startup. Renewing a token for
the same bot preserves the offset. Legacy state without a bot ID resets once;
this can replay updates still retained by Telegram.

</details>

Only listed chats can use the bot; everyone else gets a one-line refusal that
shows their chat id, so adding more people later is easy.

### 연락처 연결 선택

```bash
python3 -m bizcard_intake setup-contacts
```

1. **Mac 연락처 또는 Google 직접 연결을 고른다.**
   Mac 연락처가 권장 경로다. gws와 Google Cloud 프로젝트가 필요 없다.
2. **Mac의 연락처 접근 요청을 허용한다.**
3. **저장할 계정 번호를 고른다.**
   Mac에 등록된 연락처 계정을 선택한다.

Google이 목록에 없으면 시스템 설정 > 인터넷 계정에서 Google을 추가한다.
해당 계정의 연락처 동기화를 켠 뒤 `setup-contacts`를 다시 실행한다.

Google 연락처와 사진의 서버 동기화는 macOS 계정 서비스가 담당한다.
앱은 Mac 연락처에 저장한 필드를 다시 읽어 확인한다.
Google 웹 연락처의 동기화 완료까지 확인하는 기능은 없다.

`setup-contacts`는 `.env`에 아래 선택을 저장한다.

```bash
BIZCARD_CONTACTS_BACKEND=apple
BIZCARD_APPLE_CONTAINER=  # chosen account identifier; empty = Mac default account
```

기존 사용자는 백엔드를 설정하지 않아도 gws 방식을 유지한다.
직접 `python -m bizcard_intake`를 실행하면 프로세스 환경 변수가 `.env`보다 우선한다.
`./scripts/start_bot.sh`는 `.env` 값을 export하므로 기존 환경 변수를 덮어쓴다.
Google 직접 연결을 고르면 `BIZCARD_CONTACTS_BACKEND=google`을 저장한다.

launchd 봇은 처음 실행할 때 연락처 접근을 다시 요청할 수 있다.
요청 창에는 `python3` 또는 실제 Python 실행 파일 이름이 표시된다.

거부했으면 시스템 설정 > 개인정보 보호 및 보안 > 연락처에서 Python을 허용한다.
실행 경로나 책임 프로세스가 바뀌면 다시 허용해야 할 수 있다.
부분 접근 권한은 지원하지 않으므로 전체 연락처 접근을 허용한다.

**Mac 연락처의 메모 필드는 지원하지 않는다.**
Apple의 `com.apple.developer.contacts.notes` entitlement가 필요하기 때문이다.
사용자 메모와 검증 경고는 명함 초안에 남긴다.
저장 결과에도 누락을 알리는 경고를 표시한다.

Google 직접 연결은 기존 메모 저장을 유지한다.

Mac 연락처는 생성과 갱신 전에 저장 작업을 명함 파일에 기록한다.
작업에는 원래 계정과 대상 식별자, 최초 일치 식별자 집합을 보관한다.
이름과 전체 전화번호·팩스·이메일도 대조 정보로 보관한다.
설정을 바꿔도 진행 중인 작업과 사진 재시도는 원래 계정을 쓴다.

생성 응답이 끊기면 최초 집합과 비교해 새 연락처 한 건을 채택한다.
최초 식별자가 사라졌거나 새 후보가 없거나 여러 건이면 확인을 요청한다.
조회에서 연락처가 보이지 않아도 자동으로 다시 생성하지 않는다.
갱신은 원래 대상에 값을 추가하며 기존 이름·조직 정보와 목록 값을 보존한다.
갱신 대상 식별자가 바뀌면 같은 계정의 대조 정보로 한 건을 찾는다.
대상을 확정하지 못하면 생성으로 전환하지 않고 확인을 요청한다.

Telegram의 **이미 저장됨 / 다시 저장 / 건너뛰기** 버튼으로 선택한다.
이미 저장됨을 선택하면 추가 저장과 사진 업로드를 생략한다.
다시 저장을 선택하면 원래 계정에서 새 저장 작업을 시작한다.
다시 저장 결과에도 사진 실패와 메모 누락, 필드 검증 경고를 표시한다.
저장 중에는 수정·뒷면 추가·인식 재시도 버튼으로 명함을 변경하지 않는다.
저장 결과를 명함 파일에 기록한 뒤 원본 이미지를 정리한다.
목록은 저장 중인 명함을 `저장 중`으로 표시한다.
새 확인 단계에서는 이전 버튼을 사용할 수 없다.
기존 v2.0.0 명함에는 새 작업 기록이 없어도 기존 Google 저장을 유지한다.
Mac 권한 알림은 같은 상태에서 24시간마다 재알림한다.
마지막 알림을 로컬에 보관하므로 봇 재시작으로 반복하지 않는다.
Google 점검 알림은 `BIZCARD_ALERT_CHAT_ID`가 있을 때만 보낸다.

### Google login, once (advanced gws backend)

```bash
GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file gws auth login --scopes https://www.googleapis.com/auth/contacts
```

Always run `gws` with `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file` (`.env.example`
sets it for the bot; add the export to your shell profile too). If one call uses
the macOS keychain instead, `gws` cannot decrypt the stored login, deletes it and
you have to log in again.

`gws` needs an OAuth client in **your own** Google Cloud project. With `gcloud`
installed, `gws auth setup --login` creates the project and client for you;
without it, `setup` prints the manual console steps. Two things keep the login
from expiring:

- Request only the contacts scope (command above). The default `cloud-platform`
  scope puts the token under Workspace reauth policies.
- Personal Gmail accounts can only create *External* apps: publish the app to
  **In production** in the console. In *Testing* status Google kills the refresh
  token every 7 days. Workspace accounts should pick *Internal*, which never
  expires.

If the token still dies (password change, admin revocation, 6 months unused),
the bot tells the `BIZCARD_ALERT_CHAT_ID` chat within a day, failed saves stay
on hold as cards and are saved automatically after you log in again
(`python3 -m bizcard_intake retry-pending` forces it).

Keep the encryption key in a file so every caller (launchd, Terminal, agents)
can read it: set `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file` in `.env` and in your
shell profile, and log in with the same variable set. A gws run in keychain mode
that cannot read the key deletes `credentials.enc`.

## Using the bot

1. Send one or more card photos (an album is fine). Every photo becomes its own
   card with a number, a preview image and a draft:
   ```
   [명함 12] 명함 초안
   표시 이름: 정호준 영업팀장 누리네트웍스

   - 이름: 정호준
   - 직함: 영업팀장
   - 직함원문: 영업팀장/컨설턴트 (Sales Executive)
   - 회사: 누리네트웍스
   - 휴대전화: 010-5555-0101
   ...
   ```
   Buttons: 저장 · 직접 수정 · 뒷면 추가 · 인식 재시도 · 취소. For an album the bot
   also sends a summary with "저장 가능한 N건 모두 저장".
2. To fix a card, reply to its draft with `항목: 값` lines (`직함: 과장`,
   `휴대전화: 010-1234-5678`, `팩스: -` to clear). These are applied as typed,
   without the AI, and survive re-recognition and back-side merges. Free text
   ("이메일은 kim@acme.com") still goes through the AI reviser.
   Cards are processed in parallel (`BIZCARD_WORKERS`, default 30): the model
   calls for every card in an album run at the same time, image prep and OCR
   use at most one slot per CPU core, and saves run in the background so buttons
   stay responsive. 30 real cards took 7 s from upload to finished drafts. An
   album gets one progress message (later the summary with "모두 저장") and one
   photo+draft message per card; messages to a chat are spaced out and
   Telegram's 429 retry_after is honoured. Batch saves go to Google one contact
   at a time (Google's guidance for one user) and share a single address-book
   scan. A per-card lock keeps double taps,
   batch saves and automatic retries from saving the same card twice.
3. `목록` lists every card that is not saved yet, with a save-all button.
4. If Google is unreachable or logged out, the card is kept as "저장 보류" and
   saved automatically once Google works again (backoff from 2 minutes up to 6
   hours; checked every minute and at the daily health check). A contact whose
   photo upload failed is saved anyway and the photo is retried.

Cards live in `data/cards/<id>.json` (one file each, atomic writes). Old
`data/sessions` and `data/pending_contacts` files are migrated on startup:
entries already saved are retired, approved-but-failed ones become retry
cards, and drafts nobody approved stay in review and are never saved on their
own.

### Design choices (Korean cards)

- The phone shows `이름 짧은직함 회사`, e.g. `최민준 차장 (주)다솔이엔지`.
  That whole string is Google's *given name*; *family name* stays empty so the
  device's first/last-name order cannot put the company first. The full title
  and company stay in the organization fields.
- No title is invented; a card without one gets a 확인 필요 note.
- `052-555-0126~7` is saved as two numbers without asking.
- The card is turned upright from the text itself: every line Apple Vision reads
  votes for its reading direction (characters x confidence). A card that comes out
  sideways or upside down after rectification is rotated, OCR runs once more to
  confirm, and vertical cards stay vertical. If the vote is unclear the image is
  left as is and the draft says 명함 방향 확인 필요. Check a folder of your own
  photos with `python3 scripts/eval_orientation.py <folder>`.
- The contact photo gets scanner-style paper whitening: the bot estimates the
  card's paper color from the card itself and turns light, slightly tinted
  paper (bluish, yellowish, dull gray) close to clean white. Logos and text
  largely keep their colors. Dark or strongly colored cards (gold, black, red stock)
  are left as they are, as are photos where no card rectangle was found. Only
  the contact photo changes; OCR and AI reading use the original pixels.
- Duplicate detection scans all contacts and needs a matching mobile number and
  exact name; a retry reuses the contact id the first attempt created.
- Card images are deleted after a successful save.

### Renaming contacts saved with the old layout

Contacts saved before November 2026 have the company in the family name. Run in
Terminal:

```bash
python3 -m bizcard_intake migrate-names                # preview
python3 -m bizcard_intake migrate-names --apply --limit 1
python3 -m bizcard_intake migrate-names --apply        # everything, backup first
python3 -m bizcard_intake rollback-names data/name-migration/<backup>.json
```

Only the name field changes; phones, company, photo and notes are untouched.

## Parser and tokens

| Backend | How | Per card |
|---|---|---|
| `openai` (default when an API key is stored) | Responses API, `gpt-6-luna`, reasoning `low`, strict JSON schema, `store: false` | billed on the OpenAI API account; token counts are logged as `openai model=... tokens in=... out=...` |
| `codex` | `codex exec` in an empty dir with `--ignore-user-config --ignore-rules`, reasoning `low` | ~5–8k tokens of the ChatGPT plan's Codex allowance |
| `claude` | `claude -p --json-schema --allowedTools Read` | Claude subscription |

**Cross-check (default when `ANTHROPIC_API_KEY` is in `.env`).** Every front
side is read by `gpt-6-luna` (reasoning `low`) and `claude-haiku-5-5` in
parallel. Phone numbers from both are merged and kept only when both models
agree, the digits appear in the Apple Vision OCR text, or they are the second
line of a `~` range; a dropped number is listed in the draft. Names and
companies that differ are shown as 확인 필요 with both readings. On 21 real
cards this gave 0 name/company/phone errors (Luna alone 1, Haiku alone 9) at
about 4 s per card and about 0.001 USD per card. If one API fails the other
result is used; if both fail the codex/claude CLI takes over.

Put the OpenAI API key in `.env` as `OPENAI_API_KEY=`, or point
`BIZCARD_OPENAI_ENV_FILE` at another env file; the bot reads only the
`OPENAI_API_KEY` line and never logs it. If no explicit file is configured,
it checks only `~/.config/openai/credentials.env`. It does not search other
projects for credentials. Without a key the bot uses Codex CLI first,
then Claude CLI when Codex is unavailable. If an API call
fails (network, quota, rate limit) the same prompt is retried once with the
codex CLI (or claude when codex is not installed). The image sent to the model is a 1024px copy; the 1600px
one is only for Apple Vision OCR. Typed `항목: 값` edits, saving and retries use no
model at all.

Set `BIZCARD_PARSER=openai|codex|claude`, `BIZCARD_OPENAI_MODEL`,
`BIZCARD_REASONING_EFFORT`, `BIZCARD_CODEX_MODEL`, `BIZCARD_CLAUDE_MODEL` in `.env`.
The default model ids are the ones this project was tuned with; if your account
does not offer them, set these variables to models you can use.

## Run as a launchd service (auto-start, auto-restart)

```bash
sed -e "s|__PROJECT_ROOT__|$PWD|g" -e "s|__HOME__|$HOME|g" scripts/com.bizcard.intake.plist \
  > ~/Library/LaunchAgents/com.bizcard.intake.plist
launchctl load -w ~/Library/LaunchAgents/com.bizcard.intake.plist

launchctl stop com.bizcard.intake        # restart = stop; launchd relaunches it
tail -f data/logs/bot.log
```

`KeepAlive` is unconditional: the bot comes back after crashes, reboots and
manual stops. At startup and every 24 hours it re-checks codex/claude and the
selected contacts backend and alerts `BIZCARD_ALERT_CHAT_ID` on failure.
When that setting is empty, only Apple Contacts permission alerts go to the
allowed chats. Google backend checks and other health alerts require
`BIZCARD_ALERT_CHAT_ID`.

## CLI

```bash
python3 -m bizcard_intake doctor              # environment report
python3 -m bizcard_intake setup               # guided fixes
python3 -m bizcard_intake setup-telegram      # create/connect a Telegram bot
python3 -m bizcard_intake setup-contacts      # choose a Mac account or gws
python3 -m bizcard_intake scan card.jpg       # parse one image locally, print the draft
python3 -m bizcard_intake save <card-id>
python3 -m bizcard_intake retry-pending     # retry saves on hold now
python3 -m bizcard_intake list              # cards not saved yet
python3 -m bizcard_intake bot                 # what start_bot.sh runs
```

## Environment variables

```bash
TELEGRAM_BOT_TOKEN=              # required Telegram bot token
TELEGRAM_ALLOWED_CHAT_IDS=       # required, comma-separated
# BIZCARD_ONBOARDING_URL=        # optional; unset = default setup helper, empty = manual BotFather path
BIZCARD_ALERT_CHAT_ID=           # health alerts
BIZCARD_PARSER=auto              # auto | openai | codex | claude
OPENAI_API_KEY=                  # openai parser (auto picks it when set)
BIZCARD_OPENAI_MODEL=gpt-6-luna
BIZCARD_REASONING_EFFORT=low
ANTHROPIC_API_KEY=               # optional cross-check with Claude Haiku
BIZCARD_ANTHROPIC_MODEL=claude-haiku-5-5
BIZCARD_CROSSCHECK=auto          # off disables the cross-check
BIZCARD_CODEX_MODEL=gpt-5.6-luna
BIZCARD_CLAUDE_MODEL=haiku
BIZCARD_PARSER_TIMEOUT=90
BIZCARD_WORKERS=30               # cards processed at the same time
BIZCARD_PYTHON=                  # interpreter with numpy/Pillow (default: python3 on PATH)
GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file
```

## Privacy

During optional managed Telegram setup, the setup service stores pairing and
bot identity data for up to 10 minutes. It stores hashes of the poll token and
six-digit receipt code, and retrieves the bot token only when it is claimed;
the bot token is not stored on the relay. After local `.env` storage, the
installer acknowledges receipt and the service removes the pairing's personal
data, retaining only a minimal claimed marker. The manager bot operator can
technically retrieve managed bot tokens again. Manual setup bypasses the relay
and sends the token only to Telegram; clipboard detection requires confirmation
before use.

Card photos arrive through Telegram, and drafts with preview images go back to
the same chat before you approve them. Card images and OCR text are sent to
the model provider you use: the OpenAI API or Codex CLI, and Anthropic (Claude
Haiku API for the cross-check, or the Claude Code CLI). The parsed fields and
card photo go to the selected contacts account (Mac Contacts or Google directly).
Mac account services sync a Google or iCloud container to that provider. Nothing else leaves the machine. Card
records, logs and images stay locally in `data/`, which is git-ignored; the
local card images are deleted after a successful save (the profile image is kept
until a failed photo upload succeeds on retry).

## Tests

```bash
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests
```

## Cloud mode (preview, no Mac needed)

`cloud/` holds a Cloudflare Workers version for people without a Mac: one photo
becomes a draft with Save/Cancel buttons, and Save creates the Google contact.
It runs on the Workers free plan and reads the card with the model alone (no
Apple Vision), so rectification and OCR cross-checks are missing and accuracy
is not yet measured. It needs its own bot token: this Mac bot deletes webhooks
on start, so the two cannot share one. Setup, limits and costs are in
[cloud/README.md](cloud/README.md). It has only been tested locally so far.

## Roadmap

- Ship a Google-verified shared OAuth client so users do not need their own
  Cloud project (rclone-style).
- Linux support: Tesseract OCR path and a systemd unit.
