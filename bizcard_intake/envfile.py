"""Read dotenv settings as data; never expand or execute shell expressions."""
import re
import shlex
import sys
from pathlib import Path


ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(\s*)(.*)$")


def read_settings(path):
    settings = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = ASSIGNMENT.fullmatch(line)
        if not match:
            raise ValueError("invalid assignment")
        raw = match[3]
        # Bash starts a comment only at a word boundary: an unescaped space,
        # or whitespace right after '='. Escaped or quoted spaces join words.
        quote, escaped, boundary, end = None, False, bool(match[2]), 0
        for index, character in enumerate(raw):
            if escaped:
                escaped, boundary, end = False, False, index + 1
                continue
            if character == "\\" and quote != "'":
                escaped, boundary, end = True, False, index + 1
                continue
            if quote is None and character == "#" and boundary:
                break
            if character in ("'", '"'):
                if quote == character:
                    quote = None
                elif quote is None:
                    quote = character
            boundary = quote is None and character.isspace()
            if not boundary:
                end = index + 1
        raw = raw[:end]
        lexer = shlex.shlex(raw, posix=True)
        lexer.whitespace = ""
        lexer.commenters = ""
        value = "".join(lexer)
        if "\0" in value:
            raise ValueError("invalid value")
        settings[match[1]] = value
    return settings


def main():
    try:
        settings = read_settings(sys.argv[1])
        # Parse everything before emitting any values. An empty final record
        # lets the shell detect a failed parser despite process substitution.
        records = [f"{key}={value}".encode("utf-8") for key, value in settings.items()]
    except (OSError, ValueError, UnicodeError):
        print("Cannot read .env settings. Check assignment syntax and file permissions.", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(b"\0".join(records + [b"", b""]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
