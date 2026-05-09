"""
Demo: shows what the programmatic audit layer does without calling an LLM.
Run:  python tests/demo_audit.py

Builds a tiny fake repo, then runs verify_citations on a few answers — one
honest, one with bad citations, one with citations past EOF — and prints
the result. Useful for showing a reviewer the audit is real.

The full audit (with the LLM pass) requires ANTHROPIC_API_KEY and a network
connection; this is the deterministic half.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from investigator.audit import verify_citations  # noqa: E402


def make_repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="demo-repo-"))
    (root / "src").mkdir()
    (root / "src" / "auth.py").write_text(
        "import jwt\n"
        "\n"
        "SECRET = 'todo-rotate'\n"
        "\n"
        "def verify_token(token: str) -> dict:\n"
        "    return jwt.decode(token, SECRET, algorithms=['HS256'])\n"
        "\n"
        "async def login(username, password):\n"
        "    return jwt.encode({'sub': username}, SECRET, algorithm='HS256')\n"
    )
    return root


def show(label: str, answer: str, repo: Path) -> None:
    print(f"=== {label} ===")
    print("Answer:")
    for line in answer.splitlines():
        print(f"  {line}")
    print()
    report = verify_citations(repo, answer)
    if not report.checks:
        print("  (no citations to check)")
    else:
        for c in report.checks:
            mark = "OK " if c.ok else "BAD"
            print(f"  [{mark}] {c.raw} — {c.reason}")
    print(f"  all_ok = {report.all_ok}")
    print()


def main() -> None:
    repo = make_repo()
    try:
        show(
            "honest answer",
            "Auth lives in `src/auth.py:5-6`. The login flow is at `src/auth.py:8-9`.",
            repo,
        )
        show(
            "answer with hallucinated file",
            "The token rotation logic is in `src/rotation.py:12-18`.",
            repo,
        )
        show(
            "answer with line numbers past EOF",
            "See `src/auth.py:200-250` for the validation step.",
            repo,
        )
        show(
            "mixed answer — one good, one bad citation",
            "Tokens are signed with HS256 (`src/auth.py:5-6`). "
            "Rate limiting is in `src/ratelimit.py:1-30`.",
            repo,
        )
        show(
            "no citations at all",
            "It uses JWT for auth. I don't have a citation handy.",
            repo,
        )
    finally:
        shutil.rmtree(repo, ignore_errors=True)


if __name__ == "__main__":
    main()
