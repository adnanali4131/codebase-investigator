"""
Two-layer audit:
  verify_citations — programmatic path:line check; cheap, catches hallucinated refs.
  llm_audit        — separate Claude call, fresh context, no access to the agent's reasoning.
The fresh-context separation is the point: self-scoring in the same call doesn't catch much.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from . import tools
from .prompts import AUDIT_SYSTEM


# Match `path/to/file.ext:42` or `path/to/file.ext:42-58`, optionally backticked.
# A file extension is required so we don't catch incidental `word:42` text.
_CITATION_RE = re.compile(
    r"`?([\w./\-]+\.[a-zA-Z][\w]{0,8}):(\d+)(?:-(\d+))?`?"
)


@dataclass
class CitationCheck:
    raw: str             # the citation text as found in the answer
    path: str
    start: int
    end: int
    ok: bool             # does the file exist and contain this range?
    reason: str = ""     # if not ok, why; if ok, may include a short snippet


@dataclass
class CitationReport:
    checks: list[CitationCheck] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def render(self) -> str:
        if not self.checks:
            return "(no citations found in answer)"
        lines = []
        for c in self.checks:
            mark = "OK" if c.ok else "BAD"
            lines.append(f"  [{mark}] {c.raw} — {c.reason}")
        return "Programmatic citation check:\n" + "\n".join(lines)


def _extract_citations(answer: str) -> list[tuple[str, str, int, int]]:
    """Returns list of (raw_match, path, start, end). Deduped."""
    seen: set[tuple[str, int, int]] = set()
    out = []
    for m in _CITATION_RE.finditer(answer):
        path = m.group(1)
        start = int(m.group(2))
        end = int(m.group(3)) if m.group(3) else start
        if end < start:
            start, end = end, start
        key = (path, start, end)
        if key in seen:
            continue
        seen.add(key)
        out.append((m.group(0), path, start, end))
    return out


def verify_citations(repo_root: Path, answer: str) -> CitationReport:
    report = CitationReport()
    for raw, path, start, end in _extract_citations(answer):
        try:
            target = (repo_root / path.lstrip("/")).resolve()
            target.relative_to(repo_root.resolve())
            if not target.exists():
                report.checks.append(CitationCheck(raw, path, start, end, ok=False,
                                                   reason=f"file does not exist"))
                continue
            if not target.is_file():
                report.checks.append(CitationCheck(raw, path, start, end, ok=False,
                                                   reason="path is not a file"))
                continue
            text = target.read_text(encoding="utf-8", errors="replace")
            n = len(text.splitlines())
            if start > n:
                report.checks.append(CitationCheck(raw, path, start, end, ok=False,
                                                   reason=f"line {start} past EOF (file has {n} lines)"))
                continue
            if end > n:
                report.checks.append(CitationCheck(raw, path, start, end, ok=False,
                                                   reason=f"line {end} past EOF (file has {n} lines); "
                                                          f"start={start} is valid"))
                continue
            if end - start > 400:
                report.checks.append(CitationCheck(raw, path, start, end, ok=True,
                                                   reason=f"range exists but is unusually wide ({end - start + 1} lines) — verify it's actually relevant"))
                continue
            report.checks.append(CitationCheck(raw, path, start, end, ok=True,
                                               reason=f"range exists ({end - start + 1} lines)"))
        except ValueError:
            report.checks.append(CitationCheck(raw, path, start, end, ok=False,
                                               reason="path escapes repo root"))
        except OSError as e:
            report.checks.append(CitationCheck(raw, path, start, end, ok=False,
                                               reason=f"read error: {e}"))
    return report


@dataclass
class AuditResult:
    verdict: str                     # "solid" | "caveats" | "problems" | "error"
    summary: str
    citation_report: CitationReport
    raw: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        out = [f"Verdict: {self.verdict.upper()}"]
        out.append(f"Summary: {self.summary}")
        out.append("")
        out.append(self.citation_report.render())
        if self.raw.get("issues"):
            out.append("")
            out.append("Issues:")
            for issue in self.raw["issues"]:
                sev = issue.get("severity", "?")
                msg = issue.get("issue", "")
                ev = issue.get("evidence", "")
                out.append(f"  [{sev}] {msg}  ({ev})")
        if self.raw.get("missed"):
            out.append("")
            out.append("Missed:")
            for m in self.raw["missed"]:
                out.append(f"  - {m}")
        return "\n".join(out)


def _tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": "list_dir",
            "description": "List directory contents (relative to repo root).",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
            },
        },
        {
            "name": "read_file",
            "description": "Read file contents with line numbers prepended. Defaults to whole file (capped at 800 lines).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer", "default": 1},
                    "end": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "search",
            "description": "Ripgrep-style search across the repo. Returns path:line:content hits.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path_glob": {"type": "string"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "outline",
            "description": "List top-level definitions in a source file.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    ]


def _dispatch_tool(repo_root: Path, name: str, inp: dict[str, Any]) -> str:
    try:
        if name == "list_dir":
            return tools.list_dir(repo_root, inp.get("path", "."))
        if name == "read_file":
            return tools.read_file(repo_root, inp["path"], inp.get("start", 1), inp.get("end"))
        if name == "search":
            return tools.search(repo_root, inp["query"], inp.get("path_glob"))
        if name == "outline":
            return tools.outline(repo_root, inp["path"])
        return f"unknown tool: {name}"
    except tools.ToolError as e:
        return f"tool error: {e}"
    except Exception as e:
        return f"unexpected error: {e}"


def llm_audit(
    client: Anthropic,
    model: str,
    repo_root: Path,
    question: str,
    answer: str,
    citation_report: CitationReport,
    max_tool_iters: int = 6,
) -> AuditResult:
    """Audit pass in a fresh context. Auditor sees the question, answer, and citation report — not the agent's reasoning."""
    user_block = (
        f"USER QUESTION:\n{question}\n\n"
        f"ANSWER UNDER REVIEW:\n{answer}\n\n"
        f"PROGRAMMATIC CITATION CHECK (already done for you):\n{citation_report.render()}\n\n"
        "Now audit this answer. Use the tools to verify the harder claims. "
        "Return only the JSON object specified in your instructions."
    )

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_block}]
    schemas = _tool_schemas()

    for _ in range(max_tool_iters):
        resp = client.messages.create(
            model=model,
            max_tokens=2000,
            system=AUDIT_SYSTEM,
            tools=schemas,
            messages=messages,
        )
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            return _parse_audit_json(text, citation_report)

        messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
        tool_results = []
        for tu in tool_uses:
            out = _dispatch_tool(repo_root, tu.name, tu.input or {})
            if len(out) > 12000:
                out = out[:12000] + "\n... (truncated)"
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": out,
            })
        messages.append({"role": "user", "content": tool_results})

    return AuditResult(
        verdict="error",
        summary="Audit exceeded maximum tool iterations without producing a verdict.",
        citation_report=citation_report,
    )


def _parse_audit_json(text: str, citation_report: CitationReport) -> AuditResult:
    # The model may wrap in ```json fences or include surrounding prose.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    brace = cleaned.find("{")
    if brace > 0:
        cleaned = cleaned[brace:]
    last_brace = cleaned.rfind("}")
    if last_brace > 0:
        cleaned = cleaned[: last_brace + 1]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return AuditResult(
            verdict="error",
            summary=f"Auditor returned non-JSON output: {text[:200]}",
            citation_report=citation_report,
        )

    verdict = data.get("verdict", "error")
    if verdict not in {"solid", "caveats", "problems"}:
        verdict = "error"
    summary = data.get("summary", "")

    # If the programmatic check found broken citations, "solid" is wrong by construction.
    if verdict == "solid" and not citation_report.all_ok:
        verdict = "problems"
        summary = (
            "Auditor returned 'solid' but programmatic check found broken citations. "
            "Downgrading. Original summary: " + summary
        )

    return AuditResult(
        verdict=verdict,
        summary=summary,
        citation_report=citation_report,
        raw=data,
    )
