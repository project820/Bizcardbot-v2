"""Command line entry: bot (default), scan, save, retry-pending, doctor, setup."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import apple_contacts, cards, doctor, migrate_names, onboarding, pipeline, telegram, writer
from .contacts import ContactSaveError
from .contacts import LOGIN_COMMAND
from .envfile import read_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANUAL_GWS_STEPS = """\
gcloud가 없어 수동 설정이 필요합니다 (한 번만):
  1. https://console.cloud.google.com 에서 새 프로젝트 생성
  2. API 및 서비스 > 라이브러리 > "People API" 사용 설정
  3. Google 인증 플랫폼 > 대상: 사용자 유형 선택
     - Workspace 계정: "내부" (만료 없음)
     - 개인 Gmail: "외부" 로 만든 뒤 반드시 "프로덕션"으로 게시 (테스트 상태는 7일마다 로그인 만료)
  4. 클라이언트 > 클라이언트 만들기 > 데스크톱 앱 > JSON 다운로드
  5. 내려받은 파일을 ~/.config/gws/client_secret.json 으로 저장
  6. 아래 로그인 명령 실행"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bizcard", description="Business card -> Contacts")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("bot", help="Telegram 봇 실행 (기본)")
    scan = sub.add_parser("scan", help="이미지 하나를 파싱해 초안 출력 (로컬 점검용)")
    scan.add_argument("image", type=Path)
    save = sub.add_parser("save", help="명함 ID(또는 예전 세션 JSON)를 선택한 연락처 계정에 저장")
    save.add_argument("session", type=Path)
    sub.add_parser("retry-pending", help="저장 보류된 명함 즉시 재시도")
    sub.add_parser("list", help="저장 안 된 명함 목록")
    migrate = sub.add_parser("migrate-names", help="기존 연락처 이름을 '이름 직함 회사' 형식으로 정리 (기본: 미리보기)")
    migrate.add_argument("--apply", action="store_true", help="실제로 변경 (백업 후)")
    migrate.add_argument("--only", action="append", default=[], help="people/c... 지정 (여러 번 가능)")
    migrate.add_argument("--limit", type=int, default=None)
    rollback = sub.add_parser("rollback-names", help="migrate-names 백업으로 이름 되돌리기")
    rollback.add_argument("backup", type=Path)
    sub.add_parser("doctor", help="준비 상태 점검")
    sub.add_parser("setup", help="준비 상태 점검 후 부족한 항목 안내/설정")
    sub.add_parser("setup-contacts", help="Mac 연락처 또는 Google 직접 연결 선택")
    setup_telegram = sub.add_parser("setup-telegram", help="Telegram 봇 만들기와 연결 설정")
    setup_telegram.add_argument("--manual", action="store_true", help="BotFather 수동 설정")
    setup_telegram.add_argument("--no-open", action="store_true", help="Telegram 링크 자동 열기 생략")
    setup_telegram.add_argument("--yes", action="store_true", help="기존 봇 연결 덮어쓰기 확인 생략")
    args = parser.parse_args(argv)

    command = args.command or "bot"
    # Setup/doctor also support direct function calls; onboarding reads its own
    # target file so freshly saved settings are not shadowed by stale values.
    if command not in ("setup", "doctor", "setup-telegram", "setup-contacts") and not _load_project_settings():
        return 1
    if command != "bot":
        telegram.setup_logging()
    if command == "bot":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is required")
        telegram.setup_logging()
        return cmd_bot()
    if command == "scan":
        return cmd_scan(args.image)
    if command == "save":
        return cmd_save(args.session)
    if command == "retry-pending":
        return cmd_retry_pending()
    if command == "list":
        return cmd_list()
    if command == "migrate-names":
        return cmd_migrate_names(args.apply, set(args.only) or None, args.limit)
    if command == "rollback-names":
        return cmd_rollback_names(args.backup)
    if command == "doctor":
        return cmd_doctor()
    if command == "setup":
        return cmd_setup()
    if command == "setup-contacts":
        return cmd_setup_contacts()
    if command == "setup-telegram":
        return onboarding.setup_telegram(env_path=PROJECT_ROOT / ".env", manual=args.manual,
                                         no_open=args.no_open, yes=args.yes)
    parser.print_help()
    return 2


@writer.required(bot_start=True)
def cmd_bot() -> int:
    warm_up = getattr(pipeline, "warm_up", None)
    if warm_up:
        warm_up()
    return telegram.main() or 0


@writer.required
def cmd_scan(image: Path) -> int:
    if not image.exists():
        print(f"파일 없음: {image}", file=sys.stderr)
        return 1
    card = cards.new_card("cli", source="cli")
    target = pipeline.DATA_DIR / "cli" / "front" / f"{card['id']}_{image.name}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(image, target)
    try:
        pipeline.recognize_front(card, target)
    except (pipeline.ContactSaveError, cards.StaleCardState) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(pipeline.draft_text(card))
    print(f"\n[rectifier={card.get('rectifier')}] 명함 ID: {card['id']}")
    print(f"저장하려면: python -m bizcard_intake save {card['id']}")
    return 0


@writer.required
def cmd_save(target: Path) -> int:
    card = cards.load_card(str(target))
    if card is None and target.exists():
        legacy = json.loads(target.read_text(encoding="utf-8"))
        card = cards.load_card(legacy.get("id", "")) if legacy.get("id") else None
        if card is None:
            card = pipeline._card_from_legacy("cli", legacy, "review")
            try:
                cards.save_card(card)
            except cards.StaleCardState:
                print("저장 상태가 변경되었습니다. 명함 ID로 다시 확인하세요.", file=sys.stderr)
                return 1
    if card is None:
        print(f"명함 ID 또는 세션 파일을 찾을 수 없음: {target}", file=sys.stderr)
        return 1
    outcome = pipeline.approve(card)
    print(("저장 완료: " if outcome["ok"] else "저장 실패: ") + outcome["message"])
    for warning in outcome.get("warnings") or []:
        print(f"  참고: {warning}")
    return 0 if outcome["ok"] else 1


@writer.required
def cmd_retry_pending() -> int:
    pipeline.migrate_legacy()
    saved, failures = pipeline.retry_due(force=True)
    print(f"saved {saved}, failed {len(failures)}")
    for failure in failures:
        print(f"  {failure}", file=sys.stderr)
    return 1 if failures else 0


def cmd_list() -> int:
    open_cards = cards.list_cards(statuses=cards.OPEN_STATUSES)
    if not open_cards:
        print("저장 안 된 명함 없음")
    for card in open_cards:
        draft = pipeline.current_draft(card)
        print(f"{card['id']}  {cards.label(card)}  {card['status']:<11} {draft.display_name}  {card.get('last_error') or ''}")
    return 0


@writer.required
def cmd_migrate_names(apply: bool, only, limit) -> int:
    plan = migrate_names.plan_migration()
    if only:
        plan = [item for item in plan if item["resourceName"] in only]
    if limit:
        plan = plan[:limit]
    print(migrate_names.format_plan(plan))
    if not apply:
        print(f"\n미리보기 {len(plan)}건. 적용하려면 --apply (먼저 --limit 1 권장)")
        return 0
    backup = migrate_names.apply_migration(plan, Path("data/name-migration"))
    print(f"적용 완료. 되돌리기: python -m bizcard_intake rollback-names {backup}")
    return 0


@writer.required
def cmd_rollback_names(backup: Path) -> int:
    results = migrate_names.rollback(backup)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


def _load_project_settings() -> bool:
    env_file = PROJECT_ROOT / ".env"
    try:
        settings = read_settings(env_file)
    except FileNotFoundError:
        return True
    except (OSError, ValueError, UnicodeError):
        print("Cannot read .env settings. Check assignment syntax and file permissions.", file=sys.stderr)
        return False
    for key, value in settings.items():
        os.environ.setdefault(key, value)
    return True


def cmd_doctor() -> int:
    if not _load_project_settings():
        return 1
    checks = doctor.run_checks()
    print(doctor.format_report(checks))
    return 0 if all(c.ok for c in checks) else 1


def cmd_setup() -> int:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        shutil.copyfile(PROJECT_ROOT / ".env.example", env_file)
        print(f".env 생성: {env_file}  ->  TELEGRAM_BOT_TOKEN 등을 채운 뒤 다시 실행하세요.")

    if not _load_project_settings():
        return 1
    if not os.environ.get("BIZCARD_CONTACTS_BACKEND", "").strip():
        if cmd_setup_contacts() != 0:
            return 1
        if not _load_project_settings():
            return 1
    checks = doctor.run_checks()
    print(doctor.format_report(checks))
    failed = {c.name: c for c in checks if not c.ok}
    if not failed:
        print("\n모든 준비가 끝났습니다. 실행: ./scripts/start_bot.sh")
        return 0

    gws = failed.get("gws")
    if gws and shutil.which("gws"):
        print("\n--- Google 로그인 ---")
        if shutil.which("gcloud"):
            if ask("gcloud가 있습니다. gws auth setup --login 으로 프로젝트/OAuth 클라이언트를 자동 생성할까요?"):
                subprocess.run(["gws", "auth", "setup", "--login"], check=False,
                               env={**os.environ, "GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND": "file"})
        elif not (Path.home() / ".config" / "gws" / "client_secret.json").exists():
            print(MANUAL_GWS_STEPS)
        if ask(f"지금 로그인할까요? ({LOGIN_COMMAND})"):
            subprocess.run(LOGIN_COMMAND.split(), check=False,
                           env={**os.environ, "GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND": "file"})
        print("개인 Gmail(외부 앱)이라면 콘솔에서 앱을 '프로덕션'으로 게시했는지 확인하세요. 테스트 상태는 7일마다 로그인이 풀립니다.")

    parser_check = failed.get("parser")
    if parser_check:
        print("\n--- 명함 파싱 모델 ---")
        print("ChatGPT(Plus 이상) 구독이면 codex, Claude 구독이면 claude CLI를 설치하고 로그인하세요. .env의 BIZCARD_PARSER로 선택합니다.")
        print(f"  {parser_check.fix}")

    print("\n남은 항목을 해결한 뒤 `python -m bizcard_intake doctor` 로 다시 확인하세요.")
    return 1


def cmd_setup_contacts() -> int:
    """Choose a destination without changing process-environment precedence."""
    path = PROJECT_ROOT / ".env"
    try:
        if path.exists():
            read_settings(path)  # Fail before any permission request for malformed files.
        print("명함을 저장할 연락처 연결을 선택하세요.")
        print("1. Mac 연락처(추천: Google·iCloud 계정 선택, Google Cloud 설정 불필요)")
        print("2. Google 직접 연결(gws, 고급)")
        choice = input("번호 [1]: ").strip() or "1"
        if choice == "2":
            settings = {"BIZCARD_CONTACTS_BACKEND": "google", "BIZCARD_APPLE_CONTAINER": ""}
            onboarding.save_settings(path, settings)
            print("Google 직접 연결을 저장했습니다. `python -m bizcard_intake setup`에서 gws 로그인을 완료하세요.")
        elif choice == "1":
            print("Mac의 연락처 접근 요청을 허용하세요.")
            print("launchd 봇의 첫 실행 때 'python3' 연락처 접근 요청이 한 번 더 표시됩니다.")
            status = apple_contacts.run_helper("request").get("status")
            if status != "authorized":
                print(apple_contacts.AUTH_MESSAGE)
                return 1
            result = apple_contacts.run_helper("containers")
            containers = result.get("containers", [])
            if not any("google" in item["name"].lower() for item in containers):
                print("Google 계정이 없습니다. 시스템 설정 > 인터넷 계정에서 Google을 추가하고 연락처를 켜세요.")
                print("지금은 아래 iCloud 또는 이 Mac 계정을 선택해도 됩니다. 추가 후 setup-contacts로 다시 선택하세요.")
            if not containers:
                print("저장 가능한 연락처 계정이 없습니다. 인터넷 계정을 설정한 뒤 다시 실행하세요.")
                return 1
            for index, item in enumerate(containers, 1):
                name = "이 Mac" if item.get("type") == 1 else item["name"]
                print(f"{index}. {name}")
            selected = input("저장할 계정 번호: ").strip()
            if not selected.isdigit() or not 1 <= int(selected) <= len(containers):
                print("목록에 있는 계정 번호를 입력하세요. 설정을 저장하지 않았습니다.")
                return 1
            container = containers[int(selected) - 1]
            settings = {"BIZCARD_CONTACTS_BACKEND": "apple", "BIZCARD_APPLE_CONTAINER": container["id"]}
            onboarding.save_settings(path, settings)
            print(f"Mac 연락처({container['name']})를 저장했습니다.")
            print("메모는 Mac 자동화 권한을 허용하면 저장 후 자동으로 추가합니다.")
        else:
            print("1 또는 2를 입력하세요. 설정을 저장하지 않았습니다.")
            return 1
        if any(key in os.environ and os.environ[key] != value for key, value in settings.items()):
            print("현재 프로세스의 환경 변수가 .env보다 우선합니다. 다음 봇 실행에도 환경 변수를 확인하세요.")
        return 0
    except ContactSaveError as exc:
        print(str(exc), file=sys.stderr)
    except (OSError, ValueError, UnicodeError, onboarding.SetupError):
        print("연락처 설정을 저장하지 못했습니다. .env 형식과 파일 권한을 확인하세요.", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("연락처 설정을 취소했습니다.")
    return 1


def ask(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
