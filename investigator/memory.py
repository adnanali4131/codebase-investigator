"""
Claims ledger: structured record of what the agent has committed to in past turns,
injected into the system prompt so contradictions over many turns become visible.
Populated by an extraction call after each answer (see prompts.CLAIMS_SYSTEM).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Claim:
    turn: int
    claim: str
    citation: str | None  # "path:lines" or None


@dataclass
class Ledger:
    claims: list[Claim] = field(default_factory=list)

    def add(self, turn: int, claims_json: list[dict[str, Any]]) -> None:
        for c in claims_json:
            text = (c.get("claim") or "").strip()
            if not text:
                continue
            cite = c.get("citation")
            if cite is not None and not isinstance(cite, str):
                cite = None
            self.claims.append(Claim(turn=turn, claim=text, citation=cite))

    def render(self) -> str:
        if not self.claims:
            return ""
        lines = ["Prior claims you have made in this conversation:"]
        for c in self.claims:
            cite = f"  [{c.citation}]" if c.citation else ""
            lines.append(f"  (turn {c.turn}) {c.claim}{cite}")
        lines.append(
            "If you find any of these are wrong, say so explicitly — don't silently change "
            "your story."
        )
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps([asdict(c) for c in self.claims], indent=2)
