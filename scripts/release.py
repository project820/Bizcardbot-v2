#!/usr/bin/env python3
"""Local release checks and changelog helpers; never commits or uses the network."""

import argparse
import ast
from datetime import date
import hashlib
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
TELEGRAM_TOKEN_LABEL = "Telegram 봇 토큰"
SECRET_PATTERNS = (
    (TELEGRAM_TOKEN_LABEL, re.compile(r"(?<![\w])\d{5,}:[A-Za-z0-9_-]{30,}")),
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
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"}
PUBLIC_IMAGES = Path("docs/assets/public-images.txt")


class ReleaseError(ValueError):
    """Safe, authored diagnostic that never includes raw file contents."""


def run_git(root, *arguments):
    result = subprocess.run(["git", *arguments], cwd=root, capture_output=True)
    if result.returncode:
        # Git stderr can include local paths or credentials: do not echo it.
        raise ReleaseError(f"Git 확인에 실패했다: {arguments[0]}")
    return result.stdout.decode("utf-8", errors="surrogateescape")


def public_image_path(filename):
    path = Path(filename)
    return (len(path.parts) > 2 and path.parts[:2] == ("docs", "assets")
            and ".." not in path.parts and path.as_posix() == filename
            and filename == filename.strip()
            and not any(character in filename for character in "\\\t\r\n\0"))


def read_public_images(root):
    """Read reviewed path/blob pairs from the current worktree, never from history."""
    target = root / PUBLIC_IMAGES
    if not target.exists():
        return set(), []
    allowed, lines = set(), target.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"([0-9a-f]{40})  (.+)", line)
        if not match or not public_image_path(match.group(2)):
            raise ReleaseError(f"{PUBLIC_IMAGES}:{number}: blob SHA-1과 docs/assets/ 아래 경로를 적는다")
        allowed.add((match.group(2), match.group(1)))
    return allowed, lines


def allow_image(root, filename):
    if not public_image_path(filename) or Path(filename).suffix.lower() not in IMAGE_SUFFIXES:
        raise ReleaseError("allow-image: docs/assets/ 아래 이미지 경로를 적는다")
    target = root / filename
    if not target.is_file() or target.resolve() != root.resolve() / filename:
        raise ReleaseError("allow-image: 실제 이미지 파일을 지정한다. 심볼릭 링크는 허용하지 않는다")
    _allowed, lines = read_public_images(root)
    blob = run_git(root, "hash-object", "--", filename).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", blob):
        raise ReleaseError("allow-image: Git blob SHA-1을 확인하지 못했다")
    # Keep earlier reviewed pairs: published commits still contain the previous image blobs.
    entry = f"{blob}  {filename}"
    updated = list(lines)
    if entry not in (line.strip() for line in lines):
        updated.append(entry)
    destination = root / PUBLIC_IMAGES
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(updated) + "\n", encoding="utf-8")


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


def is_binary(data):
    if b"\0" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def git_blob_sha1(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


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
    allowed_images, _lines = read_public_images(root)
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
        is_image = path.suffix.lower() in IMAGE_SUFFIXES
        contents = []
        for mode, blob in indexed.get(filename, []):
            if mode not in ("100644", "100755"):
                problems.append(f"{filename}: Git 인덱스의 특수 파일은 공개 안전성을 직접 확인한다")
                continue
            if is_image and (filename, blob) not in allowed_images:
                problems.append(f"{filename}: 검토하지 않은 이미지 파일이다. 검토 후 allow-image로 등록한다 (Git 인덱스)")
            if is_image:
                # Scan staged image bytes too, so a staged blob cannot hide behind a clean worktree copy.
                staged = run_git(root, "cat-file", "blob", blob)
                contents.append((staged, " (Git 인덱스)",
                                 (filename, blob) in allowed_images and blob_text(root, blob) is None))
                continue
            content = blob_text(root, blob)
            if content is not None:
                contents.append((content, " (Git 인덱스)", False))
        if target.is_file():
            data = target.read_bytes()
            reviewed = is_image and (filename, git_blob_sha1(data)) in allowed_images
            if is_image and not reviewed:
                problems.append(f"{filename}: 검토하지 않은 이미지 파일이다. 검토 후 allow-image로 등록한다")
            # Baseline JPEG Huffman tables contain token-shaped ASCII. For a reviewed binary
            # image, skip only the bot-token pattern; paths and e-mails in metadata are still caught.
            contents.append((data.decode("utf-8", errors="replace"), "", reviewed and is_binary(data)))
        for content, source, skip_token in contents:
            for number, line in enumerate(content.splitlines(), 1):
                for label, pattern in SECRET_PATTERNS:
                    if skip_token and label == TELEGRAM_TOKEN_LABEL:
                        continue
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
    # 010-0000-xxxx is never assigned to subscribers, so tests may use any last four digits.
    return digits in synthetic or bool(re.fullmatch(r"01055550\d{3}|010(?:0{4})\d{4}", digits))


def scan_history(root):
    allowed_images, _lines = read_public_images(root)
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
            if (Path(filename).suffix.lower() in IMAGE_SUFFIXES
                    and (filename, blob) not in allowed_images
                    and (filename, blob) not in seen_images):
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
    read_public_images(root)
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
    commands.add_parser("allow-image", help="검토한 공개 이미지의 경로와 blob을 허용목록에 등록한다").add_argument("path")
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
        elif args.command == "allow-image":
            allow_image(root, args.path)
            print(f"PASS: {PUBLIC_IMAGES}의 이미지 허용목록을 갱신했다")
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
