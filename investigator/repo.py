"""Clone and cache public GitHub repos under ~/.cache/codebase-investigator, keyed by owner/repo/branch."""

from __future__ import annotations

import re
import subprocess
import shutil
from dataclasses import dataclass
from pathlib import Path

CACHE_ROOT = Path.home() / ".cache" / "codebase-investigator"
MAX_REPO_MB = 200


@dataclass
class Repo:
    url: str
    owner: str
    name: str
    branch: str
    path: Path

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}@{self.branch}"


_GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[\w.-]+)/(?P<name>[\w.-]+?)(?:\.git)?/?(?:/tree/(?P<branch>[\w./-]+))?/?$"
)


def parse_github_url(url: str) -> tuple[str, str, str | None]:
    """Returns (owner, name, branch_or_None). Raises ValueError on unparseable URLs."""
    url = url.strip()
    m = _GITHUB_URL_RE.match(url)
    if not m:
        raise ValueError(
            f"Not a recognizable GitHub URL: {url!r}. "
            f"Expected something like https://github.com/owner/repo"
        )
    return m["owner"], m["name"], m["branch"]


def _detect_default_branch(remote_url: str) -> str:
    """Ask the remote for HEAD; fall back to 'main'."""
    try:
        out = subprocess.run(
            ["git", "ls-remote", "--symref", remote_url, "HEAD"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
        m = re.search(r"refs/heads/(\S+)", out)
        return m.group(1) if m else "main"
    except (subprocess.SubprocessError, subprocess.TimeoutExpired):
        return "main"


def _dir_size_mb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total / (1024 * 1024)


def clone_or_fetch(url: str) -> Repo:
    """Clone into the cache if missing; otherwise reuse. No auto-pull — same URL = same snapshot."""
    owner, name, branch = parse_github_url(url)
    remote = f"https://github.com/{owner}/{name}.git"

    if branch is None:
        branch = _detect_default_branch(remote)

    target = CACHE_ROOT / owner / name / branch
    if target.exists() and any(target.iterdir()):
        return Repo(url=url, owner=owner, name=name, branch=branch, path=target)

    target.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "git", "clone",
                "--depth=1",
                "--branch", branch,
                "--single-branch",
                remote,
                str(target),
            ],
            capture_output=True, text=True, timeout=180, check=True,
        )
    except subprocess.CalledProcessError as e:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(
            f"git clone failed for {remote} (branch {branch}): {e.stderr.strip()}"
        ) from e
    except subprocess.TimeoutExpired as e:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(f"git clone timed out for {remote}") from e

    size_mb = _dir_size_mb(target)
    if size_mb > MAX_REPO_MB:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(
            f"Repo is {size_mb:.0f} MB, over the {MAX_REPO_MB} MB limit. "
            f"This tool isn't built for repos this big."
        )

    return Repo(url=url, owner=owner, name=name, branch=branch, path=target)
