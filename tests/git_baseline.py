"""Skip baseline comparisons when the v2.1.1 tag is unavailable (for example, a ZIP download)."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def require_tag(test, tag="v2.1.1"):
    try:
        found = subprocess.run(["git", "rev-parse", "-q", "--verify", f"{tag}^{{commit}}"], cwd=ROOT,
                               capture_output=True, check=False).returncode == 0
    except OSError:
        found = False
    if not found:
        test.skipTest(f"git tag {tag} unavailable; run tests from a git clone with tags")
