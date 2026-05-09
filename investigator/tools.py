"""
Agent tools: list_dir, read_file, search, outline.

Code-returning tools prepend line numbers so the citations the model emits match
what the audit will see. All paths are resolved under the repo root to refuse escapes.
read_file caps at 800 lines per call to keep context bounded across long sessions.
search prefers ripgrep, falls back to a pure-Python grep when it's not on PATH.
"""

from __future__ import annotations

import re
import subprocess
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

MAX_READ_LINES = 800
MAX_SEARCH_HITS = 80
MAX_LIST_ENTRIES = 200

_SKIP_FILE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".7z",
    ".pyc", ".pyo", ".class", ".jar", ".war", ".so", ".dll", ".dylib",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".mov", ".webm",
    ".exe", ".bin",
}
_SKIP_DIR_NAMES = {
    ".git", "node_modules", "venv", ".venv", "__pycache__",
    "dist", "build", ".next", ".nuxt", "target", ".gradle",
    ".idea", ".vscode", "vendor",
}


@dataclass
class ToolError(Exception):
    """Surfaced to the model as a tool result, not raised through the agent loop."""
    message: str

    def __str__(self) -> str:
        return self.message


def _safe_resolve(repo_root: Path, rel_path: str) -> Path:
    if not rel_path or rel_path == ".":
        return repo_root
    rel_path = rel_path.lstrip("/")
    candidate = (repo_root / rel_path).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError:
        raise ToolError(f"path {rel_path!r} escapes the repo root")
    return candidate


def _looks_binary(path: Path) -> bool:
    if path.suffix.lower() in _SKIP_FILE_SUFFIXES:
        return True
    try:
        with path.open("rb") as f:
            chunk = f.read(2048)
        return b"\x00" in chunk
    except OSError:
        return True


def list_dir(repo_root: Path, path: str = ".") -> str:
    repo_root = repo_root.resolve()
    target = _safe_resolve(repo_root, path)
    if not target.exists():
        raise ToolError(f"{path!r} does not exist")
    if not target.is_dir():
        raise ToolError(f"{path!r} is not a directory")

    entries: list[tuple[str, str]] = []  # (label, sort_key)
    children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))

    for child in children:
        if child.name in _SKIP_DIR_NAMES:
            continue
        rel = child.relative_to(repo_root).as_posix()
        if child.is_dir():
            entries.append((f"{rel}/", rel))
        else:
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            size_label = _human_size(size)
            entries.append((f"{rel}  ({size_label})", rel))

    if not entries:
        return f"(empty directory: {path})"
    if len(entries) > MAX_LIST_ENTRIES:
        head = entries[:MAX_LIST_ENTRIES]
        return "\n".join(label for label, _ in head) + f"\n... ({len(entries) - MAX_LIST_ENTRIES} more entries truncated)"
    return "\n".join(label for label, _ in entries)


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def read_file(repo_root: Path, path: str, start: int = 1, end: int | None = None) -> str:
    # Models sometimes pass ints as strings even when the schema says integer; coerce.
    try:
        start = int(start) if start is not None else 1
        end = int(end) if end is not None else None
    except (TypeError, ValueError):
        raise ToolError(f"start and end must be integers (got start={start!r}, end={end!r})")

    target = _safe_resolve(repo_root, path)
    if not target.exists():
        raise ToolError(f"{path!r} does not exist")
    if not target.is_file():
        raise ToolError(f"{path!r} is not a file")
    if _looks_binary(target):
        raise ToolError(f"{path!r} looks binary; refusing to read")

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ToolError(f"could not read {path!r}: {e}")

    lines = text.splitlines()
    n = len(lines)
    if start < 1:
        start = 1
    if end is None or end > n:
        end = n
    if start > n:
        return f"(file has {n} lines; start={start} is past EOF)"

    requested = end - start + 1
    truncated_note = ""
    if requested > MAX_READ_LINES:
        end = start + MAX_READ_LINES - 1
        truncated_note = (
            f"\n... (truncated at {MAX_READ_LINES} lines; "
            f"file has {n} total. Call read_file again with start={end + 1} for more.)"
        )

    width = len(str(end))
    out = []
    for i in range(start, end + 1):
        out.append(f"{i:>{width}}  {lines[i-1]}")
    header = f"# {path}  (lines {start}-{end} of {n})"
    return header + "\n" + "\n".join(out) + truncated_note


_HAS_RG = shutil.which("rg") is not None


def search(repo_root: Path, query: str, path_glob: str | None = None) -> str:
    if not query.strip():
        raise ToolError("empty search query")

    if _HAS_RG:
        cmd = [
            "rg", "--no-heading", "--line-number", "--color=never",
            "--max-count=8",  # per-file cap so one mega-file can't dominate the result
            "--max-columns=300",
        ]
        for d in _SKIP_DIR_NAMES:
            cmd += ["--glob", f"!{d}/**"]
        if path_glob:
            cmd += ["--glob", path_glob]
        cmd += ["--", query, str(repo_root)]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise ToolError("search timed out")
        if res.returncode not in (0, 1):  # rg returns 1 when there are no matches
            raise ToolError(f"ripgrep failed: {res.stderr.strip()}")
        hits = res.stdout.splitlines()
    else:
        hits = list(_python_grep(repo_root, query, path_glob))

    if not hits:
        return f"(no matches for {query!r}{f' in {path_glob}' if path_glob else ''})"

    cleaned = []
    repo_str = str(repo_root) + "/"
    for h in hits:
        if h.startswith(repo_str):
            h = h[len(repo_str):]
        cleaned.append(h)

    if len(cleaned) > MAX_SEARCH_HITS:
        head = cleaned[:MAX_SEARCH_HITS]
        return "\n".join(head) + f"\n... ({len(cleaned) - MAX_SEARCH_HITS} more hits truncated; refine your query)"
    return "\n".join(cleaned)


def _python_grep(repo_root: Path, query: str, path_glob: str | None) -> Iterable[str]:
    pat = re.compile(re.escape(query)) if not _looks_like_regex(query) else re.compile(query)
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix.lower() in _SKIP_FILE_SUFFIXES:
            continue
        if path_glob and not path.match(path_glob):
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if pat.search(line):
                        rel = path.relative_to(repo_root).as_posix()
                        yield f"{rel}:{i}:{line.rstrip()[:300]}"
        except OSError:
            continue


def _looks_like_regex(s: str) -> bool:
    return any(c in s for c in r".*+?[](){}|\^$")


# Regex-based top-level definition extraction. tree-sitter would handle more
# languages cleanly; these patterns cover Python, JS/TS, Go, Rust, Java, Ruby.
_OUTLINE_PATTERNS = {
    ".py":   [r"^(class\s+\w+.*?:)", r"^(def\s+\w+.*?:)", r"^(async\s+def\s+\w+.*?:)"],
    ".js":   [r"^(export\s+(?:default\s+)?(?:async\s+)?function\s+\w+.*)", r"^(class\s+\w+.*)", r"^(function\s+\w+.*)", r"^(export\s+const\s+\w+\s*=.*)"],
    ".jsx":  [r"^(export\s+(?:default\s+)?(?:async\s+)?function\s+\w+.*)", r"^(class\s+\w+.*)", r"^(function\s+\w+.*)", r"^(export\s+const\s+\w+\s*=.*)"],
    ".ts":   [r"^(export\s+(?:default\s+)?(?:async\s+)?function\s+\w+.*)", r"^(export\s+(?:abstract\s+)?class\s+\w+.*)", r"^(class\s+\w+.*)", r"^(function\s+\w+.*)", r"^(export\s+const\s+\w+\s*=.*)", r"^(interface\s+\w+.*)", r"^(type\s+\w+\s*=.*)"],
    ".tsx":  [r"^(export\s+(?:default\s+)?(?:async\s+)?function\s+\w+.*)", r"^(export\s+(?:abstract\s+)?class\s+\w+.*)", r"^(class\s+\w+.*)", r"^(function\s+\w+.*)", r"^(export\s+const\s+\w+\s*=.*)", r"^(interface\s+\w+.*)"],
    ".go":   [r"^(func\s+(?:\([^)]*\)\s+)?\w+.*)", r"^(type\s+\w+\s+(?:struct|interface).*)"],
    ".rs":   [r"^(pub\s+(?:async\s+)?fn\s+\w+.*)", r"^(fn\s+\w+.*)", r"^(pub\s+struct\s+\w+.*)", r"^(struct\s+\w+.*)", r"^(pub\s+enum\s+\w+.*)", r"^(impl(?:<[^>]*>)?\s+.*)"],
    ".java": [r"^\s*(public\s+(?:abstract\s+)?class\s+\w+.*)", r"^\s*(public\s+\w+\s+\w+\s*\([^)]*\).*)"],
    ".rb":   [r"^(class\s+\w+.*)", r"^(def\s+\w+.*)", r"^(module\s+\w+.*)"],
}


def outline(repo_root: Path, path: str) -> str:
    target = _safe_resolve(repo_root, path)
    if not target.exists() or not target.is_file():
        raise ToolError(f"{path!r} is not a file")
    if _looks_binary(target):
        raise ToolError(f"{path!r} looks binary")
    suffix = target.suffix.lower()
    patterns = _OUTLINE_PATTERNS.get(suffix)
    if not patterns:
        return f"(outline not supported for {suffix} files; use read_file)"

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise ToolError(f"could not read: {e}")
    lines = text.splitlines()

    compiled = [re.compile(p) for p in patterns]
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(lines, 1):
        for pat in compiled:
            m = pat.match(line)
            if m:
                hits.append((i, m.group(1).rstrip()))
                break

    if not hits:
        return f"(no top-level definitions found in {path})"
    width = len(str(hits[-1][0]))
    return f"# outline: {path}\n" + "\n".join(f"{ln:>{width}}  {sig}" for ln, sig in hits)
