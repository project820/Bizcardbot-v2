"""Interactive Telegram setup, using only the standard library and an optional QR module.

The relay retrieves bot credentials only when claimed; acknowledgement follows local
storage. Never log HTTP URLs, payloads or exceptions containing credentials.
"""
from __future__ import annotations

import getpass
import fcntl
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import warnings
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .envfile import read_settings

DEFAULT_ONBOARDING_URL = "https://bizcard-onboarding-relay.bizcardbot.workers.dev"
USER_AGENT = f"Bizcardbot-setup/{__version__}"
TELEGRAM_API_BASE = "https://api.telegram.org"
PAIRING_TIMEOUT = 600
MANUAL_TIMEOUT = 120
POLL_INTERVAL = 3
MAX_RESPONSE_BYTES = 1_048_576
ENV_ASSIGNMENT = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$")
TOKEN_PATTERN = re.compile(r"[0-9]+:[A-Za-z0-9_-]+")
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{5,32}")


class SetupError(Exception):
    """A safe, fixed user-facing message (never include an HTTP exception)."""


class RelayError(SetupError):
    """The user can continue with BotFather after this failure."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must not forward Authorization or a Telegram bot-token URL.
        return None


def validate_base_url(url: str, *, allow_insecure_localhost: bool = False) -> str:
    try:
        parsed = urlsplit(url)
        valid_transport = parsed.scheme == "https" or (
            allow_insecure_localhost and parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
        )
        valid = (valid_transport and parsed.hostname and not parsed.username and not parsed.password
                 and not parsed.query and not parsed.fragment and parsed.port != 0
                 and not any(character.isspace() for character in url))
    except (ValueError, AttributeError):
        valid = False
    if not valid:
        raise SetupError("연결 주소는 사용자 정보가 없는 HTTPS 주소여야 합니다.")
    return url.rstrip("/")


def _request_json(url, *, data=None, bearer=None, timeout=10):
    # Cloudflare rejects the default Python-urllib agent (error 1010).
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    encoded = None
    if data is not None:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = Request(url, data=encoded, headers=headers)
    try:
        try:
            response = build_opener(_NoRedirect()).open(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            status = response.code
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise SetupError("서버 응답이 너무 큽니다. 다시 실행하세요.")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
        return status, payload
    except (URLError, HTTPException, OSError, ValueError, UnicodeError):
        raise SetupError("서버에 연결하거나 응답을 확인하지 못했습니다. 다시 실행하세요.") from None


def _telegram(base, token, operation, data=None, *, timeout=10):
    status, payload = _request_json(f"{base}/bot{token}/{operation}", data=data or {}, timeout=timeout)
    if status != 200 or payload.get("ok") is not True:
        raise SetupError("Telegram 요청에 실패했습니다. 토큰과 연결 상태를 확인하세요.")
    return payload.get("result")


def _verify_token(base, token):
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise SetupError("봇 토큰 형식이 올바르지 않습니다. BotFather에서 다시 복사하세요.")
    result = _telegram(base, token, "getMe")
    if (not isinstance(result, dict) or result.get("is_bot") is not True
            or not _positive_id(result.get("id"))
            or not isinstance(result.get("username"), str)
            or not USERNAME_PATTERN.fullmatch(result["username"])):
        raise SetupError("getMe에서 봇 정보를 확인하지 못했습니다. 토큰을 다시 확인하세요.")
    return result


def _verify_claimed_token(base, token, *, sleep):
    # A freshly created bot can briefly fail getMe; one transient error must not
    # discard a successful claim (the relay keeps it until ack or expiry).
    for attempt in range(3):
        try:
            return _verify_token(base, token)
        except SetupError:
            if attempt == 2:
                raise SetupError("새 봇 확인(getMe)에 실패했습니다. 잠시 후 setup-telegram을 다시 실행하세요.") from None
            sleep(POLL_INTERVAL)


def _positive_id(value):
    return type(value) is int and value > 0


def _show_link(link, *, no_open, output):
    print(f"Telegram에서 링크를 열고 시작을 누르세요:\n{link}", file=output)
    try:
        import qrcode
    except ImportError:
        pass
    else:
        try:
            qr = qrcode.QRCode()
            qr.add_data(link)
            qr.print_ascii(out=output, tty=False)
        except Exception:
            # Optional decoration must not prevent setup or expose errors.
            pass
    if sys.platform == "darwin" and not no_open:
        try:
            subprocess.run(["open", link], check=False, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            print("링크를 직접 열어 주세요.", file=output)


def _pair(base, name, *, no_open, input_fn, output, sleep, clock):
    began = clock()
    try:
        status, pairing = _request_json(f"{base}/v1/pairings", data={"bot_name": name})
        poll_token = pairing.get("poll_token", "")
        link = pairing.get("deep_link", "")
        expiry = pairing.get("expires_at")
        parsed = urlsplit(link)
        query = parse_qs(parsed.query, keep_blank_values=True)
        identifiers = query.get("start", [])
        if (status != 201 or not isinstance(poll_token, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{43,}", poll_token) is None
                or parsed.scheme != "https" or parsed.netloc != "t.me"
                or not USERNAME_PATTERN.fullmatch(parsed.path.lstrip("/"))
                or len(identifiers) != 1
                or re.fullmatch(r"[A-Za-z0-9_-]{22,64}", identifiers[0]) is None
                or set(query) != {"start"} or parsed.fragment
                or re.fullmatch(r"bizcard_[a-z0-9]{6}_bot", pairing.get("suggested_username", "")) is None
                or type(expiry) not in (int, float) or not math.isfinite(expiry)):
            raise RelayError("설치 도우미 응답을 확인하지 못했습니다.")
        remaining = min(PAIRING_TIMEOUT, expiry - time.time())
        deadline = min(began + PAIRING_TIMEOUT, clock() + remaining)
        if deadline <= clock():
            raise RelayError("설치 연결이 만료됐습니다.")
        _show_link(link, no_open=no_open, output=output)
        last_status = None
        while clock() < deadline:
            status, result = _request_json(
                f"{base}/v1/pairings/status", data={}, bearer=poll_token,
                timeout=min(10, max(0.1, deadline - clock())))
            state = result.get("status")
            if status == 410:
                if state == "claimed":
                    raise RelayError("설치 정보가 이미 수령됐습니다. 자동 수령을 다시 시도하지 않습니다.")
                raise RelayError("설치 연결이 만료됐습니다.")
            if status != 200:
                raise RelayError("설치 도우미 연결에 실패했습니다.")
            if state == "created":
                ready = _claim(base, poll_token, deadline=deadline, input_fn=input_fn,
                               output=output, clock=clock, sleep=sleep)
                return ready, poll_token
            if state not in ("pending", "started"):
                raise RelayError("설치 도우미 응답을 확인하지 못했습니다.")
            if state != last_status:
                print("Telegram 연결을 기다립니다." if state == "pending"
                      else "사용자 연결을 확인했습니다. Telegram에서 봇 만들기를 완료하세요.", file=output)
                last_status = state
            print(".", end="", file=output, flush=True)
            sleep(min(POLL_INTERVAL, max(0, deadline - clock())))
        raise RelayError("설치 연결이 만료됐습니다.")
    except RelayError:
        raise
    except (SetupError, ValueError, TypeError):
        raise RelayError("설치 도우미 연결에 실패했습니다.") from None
    finally:
        print(file=output)


def _claim(base, poll_token, *, deadline, input_fn, output, clock, sleep):
    while clock() < deadline:
        code = input_fn("텔레그램에 온 6자리 코드를 입력하세요: ").strip()
        if clock() >= deadline:
            break
        if not re.fullmatch(r"[0-9]{6}", code):
            print("숫자 6자리를 입력하세요.", file=output)
            continue
        # The relay allows the same code to be reclaimed until ack or expiry.
        for attempt in range(3):
            if clock() >= deadline:
                raise RelayError("설치 연결이 만료됐습니다.")
            try:
                status, result = _request_json(
                    f"{base}/v1/pairings/claim", data={"code": code}, bearer=poll_token,
                    timeout=min(10, max(0.1, deadline - clock())))
                if status < 500:
                    break
            except SetupError:
                pass
            if attempt == 2:
                raise RelayError("봇 수령에 실패했습니다. 수동 경로로 연결하세요.")
            sleep(min(POLL_INTERVAL, max(0, deadline - clock())))
        if status == 403 and result.get("status") == "bad_code":
            attempts = result.get("attempts_left")
            if type(attempts) is not int or not 0 < attempts < 5:
                raise RelayError("설치 연결이 만료됐습니다.")
            print(f"코드가 다릅니다. 남은 횟수: {attempts}", file=output)
            continue
        if status == 410:
            raise RelayError("설치 연결이 만료됐거나 이미 수령됐습니다.")
        if (status != 200 or result.get("status") != "ready"
                or not _positive_id(result.get("owner_user_id"))
                or not _positive_id(result.get("bot_id"))
                or not isinstance(result.get("bot_username"), str)
                or not USERNAME_PATTERN.fullmatch(result["bot_username"])
                or type(result.get("access_restricted")) is not bool):
            raise RelayError("설치 도우미의 봇 정보를 확인하지 못했습니다.")
        return result
    raise RelayError("설치 연결이 만료됐습니다.")


def _ack(base, poll_token, *, sleep):
    # Retrying ack is safe: a lost success response leaves a claimed tombstone.
    for attempt in range(3):
        try:
            status, result = _request_json(f"{base}/v1/pairings/ack", data={}, bearer=poll_token)
            if result.get("status") == "claimed" and status in (200, 410):
                return
        except SetupError:
            pass
        if attempt < 2:
            sleep(POLL_INTERVAL)
    raise SetupError(".env는 저장했습니다. 설치 도우미 완료 확인에 실패했습니다. 봇 연결을 확인하세요.")


def _discover_chat(base, token, username, *, output, sleep, clock, started_at):
    challenge = "명함-" + secrets.token_hex(6)
    print(f"@{username} 봇의 개인 대화에 다음 확인 단어를 보내세요: {challenge}", file=output)
    info = _telegram(base, token, "getWebhookInfo")
    if not isinstance(info, dict):
        raise SetupError("webhook 설정을 확인하지 못했습니다. 다시 실행하세요.")
    if info.get("url"):
        raise SetupError("기존 webhook이 있어 본인을 확인할 수 없습니다. 기존 운영자와 webhook을 해제한 뒤 다시 실행하세요.")
    print("확인 단어를 최대 120초 동안 기다립니다. 다른 폴링 프로세스는 중지하세요.", file=output)
    deadline = clock() + MANUAL_TIMEOUT
    offset = None
    while clock() < deadline:
        remaining = deadline - clock()
        data = {"timeout": min(20, max(0, int(remaining) - 1)), "allowed_updates": ["message"]}
        if offset is not None:
            data["offset"] = offset
        try:
            updates = _telegram(base, token, "getUpdates", data,
                                timeout=min(25, max(0.1, remaining)))
        except SetupError:
            raise SetupError("확인 메시지를 받지 못했습니다. 다른 폴링 프로세스를 중지하고 다시 실행하세요.") from None
        if not isinstance(updates, list):
            raise SetupError("개인 채팅 응답을 확인하지 못했습니다.")
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            if type(update_id) is int and update_id >= 0:
                offset = max(offset or 0, update_id + 1)
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            chat = message.get("chat")
            sender = message.get("from")
            if not isinstance(chat, dict) or not isinstance(sender, dict):
                continue
            date = message.get("date")
            if (chat.get("type") == "private" and _positive_id(chat.get("id"))
                    and sender.get("id") == chat["id"] and sender.get("is_bot") is False
                    and message.get("text") == challenge and type(date) is int
                    and date >= started_at):
                return chat["id"]
        sleep(min(POLL_INTERVAL, max(0, deadline - clock())))
    raise SetupError("확인 단어 대기 시간이 끝났습니다. setup-telegram --manual을 다시 실행하세요.")


def _clipboard_token():
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(["pbpaste"], capture_output=True, text=True,
                                timeout=3, check=False)
        token = result.stdout.strip()
        if result.returncode == 0 and TOKEN_PATTERN.fullmatch(token):
            return token
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        pass
    return None


def _manual(base, name, *, input_fn, secret_input, output, sleep, clock, started_at):
    print("BotFather 수동 설정을 진행합니다.", file=output)
    print("1. Telegram에서 공식 @BotFather를 열고 /newbot을 보내세요.", file=output)
    print(f"2. 표시 이름을 입력하세요. 예: {name}", file=output)
    print("3. bot으로 끝나는 영문 사용자 이름을 입력하세요.", file=output)
    print("4. BotFather가 준 토큰을 아래에 붙여넣으세요. 입력은 숨겨집니다.", file=output)
    token = _clipboard_token()
    bot = None
    if token:
        try:
            bot = _verify_token(base, token)
        except SetupError:
            token = None
        else:
            answer = input_fn(f"클립보드의 봇 토큰(@{bot['username']} 확인)을 쓸까요? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                token = None
    if not token:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                token = secret_input("봇 토큰 (숨김): ").strip()
            except getpass.GetPassWarning:
                raise SetupError("토큰 입력을 숨길 수 없습니다. 대화형 터미널에서 다시 실행하세요.") from None
        bot = _verify_token(base, token)
    chat_id = _discover_chat(base, token, bot["username"],
                             output=output, sleep=sleep, clock=clock, started_at=started_at)
    return token, bot, chat_id


def _comment_suffix(raw):
    quote = None
    escaped = False
    for index, character in enumerate(raw):
        if escaped:
            escaped = False
        elif character == "\\" and quote != "'":
            escaped = True
        elif character in ("'", '"'):
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
        elif character == "#" and quote is None and (index == 0 or raw[index - 1].isspace()):
            beginning = index
            while beginning > 0 and raw[beginning - 1].isspace():
                beginning -= 1
            return raw[beginning:]
    return ""


def _env_values(text, key):
    values = []
    for line in text.splitlines():
        match = ENV_ASSIGNMENT.match(line)
        if match and match[2] == key:
            try:
                values.append(" ".join(shlex.split(match[4], comments=True)))
            except ValueError:
                raise SetupError(".env의 따옴표가 닫히지 않았습니다. 해당 줄을 수정하세요.") from None
    return values


def _read_env(path):
    if not path.exists():
        return ""
    with path.open(encoding="utf-8", newline="") as source:
        return source.read()


def _save_env(path, token, chat_id, *, expected_tokens=None):
    # Lock a stable sibling inode: locking .env itself would break on replace.
    descriptor = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        _save_env_locked(path, token, chat_id, expected_tokens=expected_tokens)


def _save_env_locked(path, token, chat_id, *, expected_tokens):
    text = _read_env(path)
    if expected_tokens is not None and _env_values(text, "TELEGRAM_BOT_TOKEN") != expected_tokens:
        raise SetupError("다른 설치가 봇 연결을 변경했습니다. 저장하지 않았습니다. setup-telegram을 다시 실행하세요.")
    values = _env_values(text, "TELEGRAM_ALLOWED_CHAT_IDS")
    values.append(os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))
    ids = {chat_id}
    for value in values:
        for item in value.replace(";", ",").split(","):
            if item.strip():
                try:
                    ids.add(int(item.strip()))
                except ValueError:
                    raise SetupError("기존 TELEGRAM_ALLOWED_CHAT_IDS에 숫자가 아닌 값이 있습니다.") from None
    replacements = {"TELEGRAM_BOT_TOKEN": token,
                    "TELEGRAM_ALLOWED_CHAT_IDS": ",".join(str(value) for value in sorted(ids))}
    _write_env_locked(path, text, replacements)


def save_settings(path, replacements):
    """Update arbitrary onboarding settings using the same lock and atomic writer."""
    import shlex
    path = Path(path)
    descriptor = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        text = _read_env(path)
        # Validate the current file before replacing any assignment.
        if path.exists():
            read_settings(path)
        _write_env_locked(path, text, {key: shlex.quote(value) for key, value in replacements.items()})


def _write_env_locked(path, text, replacements):
    lines = []
    seen = set()
    newline = "\r\n" if "\r\n" in text else "\n"
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        match = ENV_ASSIGNMENT.match(body)
        if match and match[2] in replacements:
            key = match[2]
            comment = _comment_suffix(match[4])
            if comment and not comment[0].isspace():
                comment = " " + comment
            lines.append(f"{match[1]}{key}={replacements[key]}{comment}{ending}")
            seen.add(key)
        else:
            lines.append(line)
    for key, value in replacements.items():
        if key not in seen:
            if lines and not lines[-1].endswith(("\r", "\n")):
                lines.append(newline)
            lines.append(f"{key}={value}{newline}")
    descriptor, temporary = tempfile.mkstemp(prefix=".bizcard-env-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as destination:
            os.fchmod(destination.fileno(), 0o600)
            destination.write("".join(lines))
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _decorate(base, token, output):
    operations = {
        "setMyCommands": {"commands": [{"command": "list", "description": "저장 안 된 명함"},
                                        {"command": "help", "description": "사용법"}]},
        "setMyDescription": {"description": "명함 사진을 보내면 내용을 확인하고 Google 연락처에 저장합니다."},
        "setMyShortDescription": {"short_description": "명함 사진을 Google 연락처로 저장하는 명함비서입니다."},
    }
    for operation, data in operations.items():
        try:
            _telegram(base, token, operation, data)
        except SetupError:
            print(f"경고: {operation} 설정에 실패했습니다. 봇 연결은 저장했습니다.", file=output)


def setup_telegram(*, env_path=None, relay_url=None, telegram_api_base=TELEGRAM_API_BASE,
                   manual=False, no_open=False, yes=False, allow_insecure_localhost=False,
                   input_fn=None, secret_input=None, output=None, sleep=None, clock=None):
    """Return a CLI exit status; injectable HTTP bases keep tests entirely local."""
    started_at = int(time.time())
    poll_token = None
    input_fn = input_fn or input
    secret_input = secret_input or getpass.getpass
    output = output or sys.stdout
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    path = Path(env_path) if env_path is not None else Path(__file__).resolve().parent.parent / ".env"
    try:
        name = input_fn("봇 표시 이름 [명함비서]: ").strip() or "명함비서"
        if not 1 <= len(name) <= 64 or any(ord(character) < 32 for character in name):
            raise SetupError("표시 이름은 줄바꿈 없이 1~64자로 입력하세요.")
        text = _read_env(path)
        expected_tokens = _env_values(text, "TELEGRAM_BOT_TOKEN")
        existing = any(_env_values(text, "TELEGRAM_BOT_TOKEN")) or os.environ.get("TELEGRAM_BOT_TOKEN")
        if existing and not yes:
            answer = input_fn("기존 Telegram 봇 연결을 덮어쓸까요? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("기존 봇 연결을 유지했습니다.", file=output)
                return 1
        base = validate_base_url(telegram_api_base, allow_insecure_localhost=allow_insecure_localhost)
        relay = relay_url
        if relay is None:
            relay = os.environ.get("BIZCARD_ONBOARDING_URL")
        if relay is None:
            try:
                settings = read_settings(path) if path.exists() else {}
            except (ValueError, UnicodeError):
                raise SetupError(".env 설정을 읽지 못했습니다. 대입문과 따옴표를 확인하세요.") from None
            relay = settings.get("BIZCARD_ONBOARDING_URL", DEFAULT_ONBOARDING_URL)
        if not manual and relay:
            relay = validate_base_url(relay, allow_insecure_localhost=allow_insecure_localhost)
            print("자동 연결은 프로젝트 운영자의 설치 도우미(@BizcardSetupBot)를 거칩니다.", file=output)
            print("도우미는 내 봇의 토큰을 저장하지 않습니다.", file=output)
            print("도우미는 명함 사진과 Google 계정에 접근하지 않습니다.", file=output)
            print("다만 Telegram 구조상 도우미 운영자는 내 봇의 토큰을 다시 조회할 수 있습니다.", file=output)
            print("토큰이 있으면 봇에 온 메시지를 읽을 수 있습니다.", file=output)
            print("원치 않으면 python3 -m bizcard_intake setup-telegram --manual 로 직접 만드세요.", file=output)
            try:
                ready, poll_token = _pair(relay, name, no_open=no_open, input_fn=input_fn,
                                          output=output, sleep=sleep, clock=clock)
            except RelayError as error:
                print(str(error), file=output)
                manual = True
            else:
                token = ready.get("bot_token")
                bot = _verify_claimed_token(base, token, sleep=sleep)
                if bot["id"] != ready["bot_id"] or bot["username"] != ready.get("bot_username"):
                    raise SetupError("검증한 봇과 설치 도우미의 봇 정보가 다릅니다. 저장하지 않았습니다.")
                chat_id = ready["owner_user_id"]
                if not ready["access_restricted"]:
                    print("경고: 관리 봇 접근 제한을 확인하지 못했습니다.", file=output)
        else:
            if not manual:
                print("자동 설치 연결이 설정되지 않아 수동 경로를 안내합니다.", file=output)
            manual = True
        if manual:
            token, bot, chat_id = _manual(base, name, input_fn=input_fn, secret_input=secret_input,
                                          output=output, sleep=sleep, clock=clock, started_at=started_at)
        _save_env(path, token, chat_id, expected_tokens=expected_tokens)
        if poll_token:
            _ack(relay, poll_token, sleep=sleep)
        _decorate(base, token, output)
        print(f"설정 완료: @{bot['username']} 연결을 .env에 저장했습니다.", file=output)
        print("봇 실행: ./scripts/start_bot.sh", file=output)
        return 0
    except SetupError as error:
        print(str(error), file=output)
    except (EOFError, KeyboardInterrupt):
        print("입력을 취소했습니다. 설정을 완료하지 않았습니다.", file=output)
    except OSError:
        print(".env 파일을 저장하거나 읽지 못했습니다. 파일 권한을 확인하세요.", file=output)
    return 1
