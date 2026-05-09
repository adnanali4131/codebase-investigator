from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from . import tools
from .audit import AuditResult, llm_audit, verify_citations
from .memory import Ledger
from .prompts import AGENT_SYSTEM, CLAIMS_SYSTEM


# Skip the LLM audit for trivially short answers; the programmatic citation check still runs.
_MIN_ANSWER_CHARS_FOR_AUDIT = 220
_MAX_TOOL_ITERS_PER_TURN = 18


@dataclass
class TurnResult:
    question: str
    answer: str
    audit: AuditResult | None
    tool_calls: int


@dataclass
class Investigator:
    client: Anthropic
    repo_root: Path
    repo_slug: str
    model: str = "claude-opus-4-5"          # for the agent
    audit_model: str = "claude-sonnet-4-5"  # cheaper, fine for audit
    messages: list[dict[str, Any]] = field(default_factory=list)
    ledger: Ledger = field(default_factory=Ledger)
    last_audit: AuditResult | None = None
    turn: int = 0

    def _system(self) -> str:
        parts = [AGENT_SYSTEM]
        parts.append(f"\nRepository under investigation: {self.repo_slug}")
        parts.append(f"All paths are relative to the repo root.\n")
        ledger_text = self.ledger.render()
        if ledger_text:
            parts.append("\n" + ledger_text)
        return "\n".join(parts)

    def _tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "list_dir",
                "description": "List directory contents (relative to repo root). Use '.' for the root.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "default": "."}},
                },
            },
            {
                "name": "read_file",
                "description": (
                    "Read file contents with line numbers prepended. Defaults to whole file "
                    "(capped at 800 lines per call). Specify start/end to read a slice."
                ),
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
                "description": (
                    "Ripgrep-style search across the repo. Returns path:line:content hits. "
                    "Use path_glob to scope (e.g. '*.py', 'src/**/*.ts')."
                ),
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
                "description": (
                    "List top-level definitions (functions, classes, types) in a source file "
                    "with line numbers. Faster than read_file when you just want structure."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        ]

    def _dispatch(self, name: str, inp: dict[str, Any]) -> str:
        try:
            if name == "list_dir":
                return tools.list_dir(self.repo_root, inp.get("path", "."))
            if name == "read_file":
                return tools.read_file(self.repo_root, inp["path"], inp.get("start", 1), inp.get("end"))
            if name == "search":
                return tools.search(self.repo_root, inp["query"], inp.get("path_glob"))
            if name == "outline":
                return tools.outline(self.repo_root, inp["path"])
            return f"unknown tool: {name}"
        except tools.ToolError as e:
            return f"tool error: {e}"
        except KeyError as e:
            return f"tool error: missing required argument {e}"
        except Exception as e:
            return f"unexpected error: {e}"

    def ask(self, question: str, on_tool_call=None) -> TurnResult:
        """One turn: user question -> agent answer (+ audit). on_tool_call is an optional UI callback."""
        self.turn += 1
        self.messages.append({"role": "user", "content": question})

        schemas = self._tool_schemas()
        tool_calls = 0

        for _ in range(_MAX_TOOL_ITERS_PER_TURN):
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=2500,
                system=self._system(),
                tools=schemas,
                messages=self.messages,
            )
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            if not tool_uses:
                answer_text = "".join(b.text for b in resp.content if b.type == "text").strip()
                self.messages.append(
                    {"role": "assistant", "content": [b.model_dump() for b in resp.content]}
                )
                audit = self._maybe_audit(question, answer_text)
                self.last_audit = audit
                self._update_ledger(answer_text)
                return TurnResult(
                    question=question,
                    answer=answer_text,
                    audit=audit,
                    tool_calls=tool_calls,
                )

            self.messages.append(
                {"role": "assistant", "content": [b.model_dump() for b in resp.content]}
            )
            tool_results = []
            for tu in tool_uses:
                tool_calls += 1
                out = self._dispatch(tu.name, tu.input or {})
                if len(out) > 14000:
                    out = out[:14000] + "\n... (truncated; ask for a narrower range)"
                if on_tool_call:
                    on_tool_call(tu.name, tu.input or {}, out[:120].replace("\n", " "))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": out,
                })
            self.messages.append({"role": "user", "content": tool_results})

        fallback = (
            "I hit my tool-iteration budget without finishing the investigation. "
            "Let me know if you want me to keep going or narrow the question."
        )
        self.messages.append({"role": "assistant", "content": fallback})
        return TurnResult(question=question, answer=fallback, audit=None, tool_calls=tool_calls)

    def _maybe_audit(self, question: str, answer: str) -> AuditResult | None:
        cit_report = verify_citations(self.repo_root, answer)

        if len(answer) < _MIN_ANSWER_CHARS_FOR_AUDIT and cit_report.all_ok:
            return AuditResult(
                verdict="solid",
                summary="Answer too short to warrant a full audit; citations check out.",
                citation_report=cit_report,
            )

        return llm_audit(
            client=self.client,
            model=self.audit_model,
            repo_root=self.repo_root,
            question=question,
            answer=answer,
            citation_report=cit_report,
        )

    def _update_ledger(self, answer: str) -> None:
        if len(answer) < 80:
            return
        try:
            resp = self.client.messages.create(
                model=self.audit_model,
                max_tokens=600,
                system=CLAIMS_SYSTEM,
                messages=[{"role": "user", "content": answer}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            data = json.loads(text)
            if isinstance(data, list):
                self.ledger.add(self.turn, data)
        except Exception:
            # Best-effort: a failed extraction shouldn't break the turn.
            pass
