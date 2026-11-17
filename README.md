# Bizcardbot-v2

Telegram bot that turns a photo of a business card into a Google Contacts entry.
Runs on a Mac. Parsing goes through the **OpenAI API** when `OPENAI_API_KEY` is
set (billed per card, optional Claude Haiku cross-check with `ANTHROPIC_API_KEY`),
otherwise through the **Codex CLI (ChatGPT Plus or higher)** or the **Claude Code
CLI (Claude subscription)** at no extra cost. Saving goes through the free `gws` CLI.

```
photo ─▶ Apple Vision rectify + OCR ─▶ codex / claude (JSON) ─▶ draft in Telegram
                                                                    │ reply to fix, 승인 to save
                                                                    ▼
                                                        Google Contacts (with card photo)
```

## Prerequisites

| Requirement | Why | Check |
|---|---|---|
| macOS with Xcode command line tools | Apple Vision does the card rectification and OCR | `xcode-select --install` |
| Python 3.10+ with `numpy`, `Pillow` (`opencv-python` optional) | image preparation | `pip install -r requirements.txt` |
| [`gws`](https://github.com/googleworkspace/cli) logged in with the contacts scope | writes to Google Contacts | `brew install googleworkspace-cli` |
| **One of**: [`codex`](https://github.com/openai/codex) (ChatGPT Plus/Pro/Business) or [`claude`](https://docs.anthropic.com/en/docs/claude-code) (Claude subscription) | reads the card | `codex login` / `claude login` |
| Telegram bot connection | the chat channel | `python -m bizcard_intake setup-telegram` |

`python -m bizcard_intake doctor` checks all of the above and prints the fix for
anything missing. `python -m bizcard_intake setup` walks you through the
missing pieces.

## Quick start

```bash
git clone https://github.com/project820/Bizcardbot-v2.git && cd Bizcardbot-v2
pip install -r requirements.txt
cp .env.example .env
export GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file   # before any gws call, see below
```

Then, in order:

1. Run `python -m bizcard_intake setup-telegram` and follow the Korean prompts.
   Enter a display name (Enter keeps `명함비서`), then open the Telegram link and
   create your bot. Enter the six-digit code sent in Telegram into the installer.
   The installer saves its token and your allowed chat ID in
   `.env` with owner-only permissions. Existing allowed chats are kept.
2. Add `OPENAI_API_KEY=` to `.env` if you want the API parser.
3. Load settings: `set -a; source .env; set +a`.
4. Run `python -m bizcard_intake setup` for Google login and parser checks.
5. Start the bot: `./scripts/start_bot.sh`.

### Telegram setup

```bash
python -m bizcard_intake setup-telegram
```

When the hosted setup service is configured through `BIZCARD_ONBOARDING_URL`,
this command displays a Telegram link and a terminal QR code when the optional
`qrcode` module is already installed, then opens the link on macOS. It waits up
to 10 minutes and polls the setup service every 3 seconds. You can change the
suggested username in Telegram. Once your bot is created, enter the six-digit
code sent by the manager bot into this installer's prompt. A wrong code shows
the remaining attempts; five failures expire the pairing. The installer claims
the token, checks `getMe` against the returned bot ID and username, saves `.env`,
and only then acknowledges receipt to the setup service. Without a configured service,
or if pairing fails or expires, the command guides you through BotFather instead.
Tokens are entered invisibly and never printed. Existing bot connections require
confirmation before replacement (`--yes` skips that confirmation).
Use `--no-open` to open the displayed link yourself; the link and optional QR
code are still displayed. Before automatic setup, the installer explains that
the setup helper server is involved. Its operator can technically retrieve the
managed bot's token again. Use `--manual` to connect without that server.
If the final acknowledgement fails after three attempts, the installer reports
that `.env` was saved and exits unsuccessfully; the saved bot connection remains
available for verification and startup.

<details>
<summary>Appendix: manual Telegram setup</summary>

Run `python -m bizcard_intake setup-telegram --manual`.

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

### Google login, once

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
(`python -m bizcard_intake retry-pending` forces it).

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
  photos with `python scripts/eval_orientation.py <folder>`.
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
projects for credentials. Without a key the bot uses the codex or
claude CLI. If an API call
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
Google token and alerts `BIZCARD_ALERT_CHAT_ID` on failure.

## CLI

```bash
python -m bizcard_intake doctor              # environment report
python -m bizcard_intake setup               # guided fixes
python -m bizcard_intake setup-telegram      # create/connect a Telegram bot
python -m bizcard_intake scan card.jpg       # parse one image locally, print the draft
python -m bizcard_intake save <card-id>
python -m bizcard_intake retry-pending     # retry saves on hold now
python -m bizcard_intake list              # cards not saved yet
python -m bizcard_intake bot                 # what start_bot.sh runs
```

## Environment variables

```bash
TELEGRAM_BOT_TOKEN=              # required Telegram bot token
TELEGRAM_ALLOWED_CHAT_IDS=       # required, comma-separated
BIZCARD_ONBOARDING_URL=          # optional HTTPS setup service; unset uses BotFather
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
card photo go to your Google Contacts. Nothing else leaves the machine. Card
records, logs and images stay locally in `data/`, which is git-ignored; the
local card images are deleted after a successful save (the profile image is kept
until a failed photo upload succeeds on retry).

## Tests

```bash
pip install -r requirements.txt    # image tests need Pillow and numpy
python3 -m unittest discover -s tests
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
