#!/usr/bin/env python3
"""Local release checks and changelog helpers; never commits or uses the network."""

import argparse
import ast
from datetime import date
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
UNRELEASED = re.compile(r"^## \[Unreleased\][ \t]*$", re.MULTILINE)
REPO_BLOB_URL = "https://github.com/project820/Bizcardbot-v2/blob"
RELATIVE_LINK = re.compile(r"\]\((?![a-z][a-z0-9+.-]*:|#|/)([^)\s]+)\)", re.IGNORECASE)
PERSONAL_DOMAINS = (
    "gmail.com", "googlemail.com", "naver.com", "daum.net", "hanmail.net",
    "nate.com", "kakao.com", "outlook.com", "hotmail.com", "live.com",
    "icloud.com", "me.com", "mac.com", "yahoo.com", "yahoo.co.kr",
    "proton.me", "protonmail.com", "hey.com", "aol.com",
)
SECRET_PATTERNS = (
    ("Telegram 봇 토큰", re.compile(r"(?<![\w])\d{5,}:[A-Za-z0-9_-]{30,}")),
    ("API 비밀키", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("Google API 키", re.compile(r"\bAIza[A-Za-z0-9_-]{20,}")),
    ("Google OAuth 비밀값", re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{15,}")),
    ("개인 로컬 경로", re.compile("/" + r"Users/")),
    ("개인 이메일", re.compile(
        r"[A-Za-z0-9._%+-]+@(?:" + "|".join(re.escape(d) for d in PERSONAL_DOMAINS)
        + r")(?![A-Za-z0-9.-])", re.IGNORECASE)),
)
MOBILE_PATTERN = re.compile(
    r"(?<![\w+])(?:01[016789]|(?:\+?82|0082)[ ()-]*(?:\(0\)[ ()-]*)?0?1[016789])"
    r"[ .()-]*\d{3,4}[ .()-]*\d{4}(?!\d)"
)
HISTORY_PATTERNS = SECRET_PATTERNS + (("한국 휴대폰 번호", MOBILE_PATTERN),)
IMAGE_SUFFIXES = {".jpg", ".png", ".heic"}


class ReleaseError(ValueError):
    """Safe, authored diagnostic that never includes raw file contents."""


def run_git(root, *arguments):
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True)
    if result.returncode:
        # Git stderr can include local paths or credentials: do not echo it.
        raise ReleaseError(f"Git 확인에 실패했다: {arguments[0]}")
    return result.stdout.decode("utf-8", errors="surrogateescape")


def read_version(root):
    text = (root / "bizcard_intake/__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    values = [node.value.value for node in tree.body
              if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "__version__"
                      for target in node.targets)
              and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
    if len(values) != 1 or not re.fullmatch(VERSION, values[0]):
        raise ReleaseError("bizcard_intake/__init__.py: __version__은 X.Y.Z 문자열 하나여야 한다")
    return values[0]


def absolute_links(text, version):
    """GitHub release bodies cannot resolve repository-relative links; pin them to the tag."""
    return RELATIVE_LINK.sub(lambda m: f"]({REPO_BLOB_URL}/v{version}/{m.group(1).removeprefix('./')})", text)


def release_section(changelog, version):
    if not re.fullmatch(VERSION, version):
        raise ReleaseError("버전은 X.Y.Z 형식이어야 한다")
    matches = list(re.finditer(
        rf"^## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})[ \t]*$",
        changelog, re.MULTILINE))
    if len(matches) != 1:
        raise ReleaseError("CHANGELOG.md: 요청한 버전 절과 날짜가 하나 있어야 한다")
    match = matches[0]
    try:
        date.fromisoformat(match.group(1))
    except ValueError:
        raise ReleaseError("CHANGELOG.md: 요청한 버전의 날짜가 유효하지 않다") from None
    following = re.search(r"^## ", changelog[match.end():], re.MULTILINE)
    end = match.end() + following.start() if following else len(changelog)
    return changelog[match.start():end].rstrip() + "\n"


def manifests(root, version):
    """Validate every version mirror before check or bump writes anything."""
    result = {}
    for folder in ("onboarding", "cloud"):
        for name in ("package.json", "package-lock.json"):
            path = Path(folder) / name
            content = json.loads((root / path).read_text(encoding="utf-8"))
            if content.get("version") != version:
                raise ReleaseError(f"{path}: version이 __version__과 다르다")
            if name == "package-lock.json" and content.get("packages", {}).get("", {}).get("version") != version:
                raise ReleaseError(f"{path}: packages 루트 version이 __version__과 다르다")
            result[path] = content
    return result


def blob_text(root, blob):
    content = run_git(root, "cat-file", "blob", blob)
    if "\0" in content:
        return None
    try:
        content.encode("utf-8")  # run_git preserves non-UTF-8 bytes as surrogates.
    except UnicodeError:
        return None
    return content


def scan_public_files(root):
    problems = []
    filenames = run_git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    indexed = {}
    for entry in run_git(root, "ls-files", "-z", "-s").split("\0"):
        if entry:
            metadata, filename = entry.split("\t", 1)
            mode, blob, _stage = metadata.split()
            indexed.setdefault(filename, []).append((mode, blob))
    for filename in sorted((set(filenames.split("\0")) | indexed.keys()) - {""}):
        path = Path(filename)
        if ("data" in path.parts or path.name == ".env"
                or (path.name.startswith(".env.") and path.name != ".env.example")
                or path.name == ".dev.vars"
                or (path.name.startswith(".dev.vars.") and path.name != ".dev.vars.example")):
            problems.append(f"{filename}: 운영 데이터 또는 비밀 설정 파일을 공개 대상에서 제외한다")
            continue
        target = root / path
        if target.is_symlink():
            problems.append(f"{filename}: 심볼릭 링크의 공개 안전성을 직접 확인한다")
            continue
        contents = []
        for mode, blob in indexed.get(filename, []):
            if mode not in ("100644", "100755"):
                problems.append(f"{filename}: Git 인덱스의 특수 파일은 공개 안전성을 직접 확인한다")
                continue
            content = blob_text(root, blob)
            if content is not None:
                contents.append((content, " (Git 인덱스)"))
        if target.is_file():
            contents.append((target.read_bytes().decode("utf-8", errors="replace"), ""))
        for content, source in contents:
            for number, line in enumerate(content.splitlines(), 1):
                for label, pattern in SECRET_PATTERNS:
                    if pattern.search(line):
                        problems.append(f"{filename}:{number}: {label} 패턴을 발견했다{source}")
    return problems


def fixture_mobile(value):
    digits = re.sub(r"\D", "", value)
    if digits.startswith("0082"):
        digits = "0" + digits[4:].lstrip("0")
    elif digits.startswith("82"):
        digits = "0" + digits[2:].lstrip("0")
    synthetic = ("01012345678", "01011112222", "01099990000")
    return digits in synthetic or bool(re.fullmatch(r"01055550\d{3}|010(?:0{7})\d", digits))


def scan_history(root):
    problems, seen_blobs, seen_images = [], set(), set()
    for commit in run_git(root, "rev-list", "--all").splitlines():
        for entry in run_git(root, "ls-tree", "-r", "-z", commit).split("\0"):
            if not entry:
                continue
            metadata, filename = entry.split("\t", 1)
            _mode, kind, blob = metadata.split()
            if kind != "blob":
                continue
            # File names can themselves contain secrets; never echo those values.
            safe_filename = filename
            for _label, pattern in HISTORY_PATTERNS:
                safe_filename = pattern.sub("[redacted]", safe_filename)
            location = f"{commit[:12]} {safe_filename}"
            if Path(filename).suffix.lower() in IMAGE_SUFFIXES and (filename, blob) not in seen_images:
                seen_images.add((filename, blob))
                problems.append(f"{location}: 이미지 파일")
            if blob in seen_blobs:
                continue
            seen_blobs.add(blob)
            content = blob_text(root, blob)
            if content is None:
                continue
            for label, pattern in HISTORY_PATTERNS:
                if any(label != "한국 휴대폰 번호" or not fixture_mobile(match.group())
                       for match in pattern.finditer(content)):
                    problems.append(f"{location}: {label}")
    return problems


def check(root, *, allow_dirty=False, allow_existing_tag=False):
    problems = []
    version = read_version(root)
    manifests(root, version)
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if len(UNRELEASED.findall(changelog)) != 1:
        problems.append("CHANGELOG.md: [Unreleased] 절이 하나 있어야 한다")
    release_section(changelog, version)
    readme = (root / "README.md").read_text(encoding="utf-8")
    heading = re.search(r"^## (?:새 소식(?: \(What's new\))?|What's new)[ \t]*$", readme, re.MULTILINE)
    news = readme[heading.end():].split("\n## ", 1)[0] if heading else ""
    if not re.search(rf"(?<![A-Za-z0-9_.])v?{re.escape(version)}(?![A-Za-z0-9_.])", news):
        problems.append(f"README.md: 새 소식 절에 v{version}을 적는다")
    if not allow_existing_tag and run_git(root, "tag", "--list", f"v{version}").strip():
        problems.append(f"태그 v{version}이 이미 존재한다")
    if not allow_dirty and run_git(root, "status", "--porcelain", "-z"):
        problems.append("작업트리가 깨끗하지 않다")
    problems.extend(scan_public_files(root))
    return problems


def bump(root, part):
    current = read_version(root)
    documents = manifests(root, current)
    numbers = [int(number) for number in current.split(".")]
    index = {"major": 0, "minor": 1, "patch": 2}[part]
    numbers[index] += 1
    numbers[index + 1:] = [0] * (2 - index)
    version = ".".join(map(str, numbers))
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    matches = list(UNRELEASED.finditer(changelog))
    if len(matches) != 1:
        raise ReleaseError("CHANGELOG.md: [Unreleased] 절이 하나 있어야 한다")
    if re.search(rf"^## \[{re.escape(version)}\]", changelog, re.MULTILINE):
        raise ReleaseError("CHANGELOG.md: 다음 버전 절이 이미 존재한다")
    match = matches[0]
    changelog = (changelog[:match.end()] + f"\n\n## [{version}] - {date.today().isoformat()}"
                 + changelog[match.end():])
    init_path = root / "bizcard_intake/__init__.py"
    init_text, count = re.subn(r"(?m)^__version__\s*=\s*(['\"]).*?\1[ \t]*$",
                               f'__version__ = "{version}"', init_path.read_text(encoding="utf-8"))
    if count != 1:
        raise ReleaseError("bizcard_intake/__init__.py: __version__을 한 줄로 작성한다")
    # Prepare all edits before writing, so malformed input never causes partial updates.
    updates = {init_path: init_text, root / "CHANGELOG.md": changelog}
    for path, content in documents.items():
        content["version"] = version
        if path.name == "package-lock.json":
            content["packages"][""]["version"] = version
        updates[root / path] = json.dumps(content, ensure_ascii=False, indent=2) + "\n"
    for path, text in updates.items():
        path.write_text(text, encoding="utf-8")
    return version


def main(argv=None, *, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check_parser = commands.add_parser("check", help="릴리스 준비 상태를 확인한다")
    check_parser.add_argument("--allow-dirty", action="store_true", help="커밋 전 작업트리 검사를 생략한다")
    check_parser.add_argument("--allow-existing-tag", action="store_true", help="검토용으로 기존 태그 검사를 생략한다")
    commands.add_parser("history", help="모든 Git 이력의 비밀값과 개인정보 패턴을 확인한다")
    commands.add_parser("notes", help="해당 버전의 CHANGELOG 절을 출력한다").add_argument("version")
    commands.add_parser("bump", help="버전과 CHANGELOG를 올린다").add_argument("part", choices=("major", "minor", "patch"))
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            problems = check(root, allow_dirty=args.allow_dirty, allow_existing_tag=args.allow_existing_tag)
            for problem in problems:
                print(f"FAIL: {problem}", file=sys.stderr)
            if problems:
                return 1
            print(f"PASS: v{read_version(root)} 릴리스 검사를 통과했다")
        elif args.command == "history":
            problems = scan_history(root)
            for problem in problems:
                print(f"FAIL: {problem}", file=sys.stderr)
            if problems:
                return 1
            print("PASS: Git 이력 검사를 통과했다")
        elif args.command == "notes":
            section = release_section((root / "CHANGELOG.md").read_text(encoding="utf-8"), args.version)
            print(absolute_links(section, args.version), end="")
        else:
            print(f"v{bump(root, args.part)}: README와 관련 문서를 갱신한 뒤 check로 확인한다")
    except ReleaseError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    except (ValueError, OSError, SyntaxError, KeyError, TypeError, AttributeError):
        # Input contents (including invalid JSON and file paths) can contain secrets.
        print("FAIL: 릴리스 파일 또는 Git 상태가 유효하지 않다. 버전·날짜·JSON·Git을 확인한다.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
