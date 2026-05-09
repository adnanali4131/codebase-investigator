"""CLI entry point. See README for usage."""

from __future__ import annotations

import argparse
import os
import sys
import textwrap

from anthropic import Anthropic

from .agent import Investigator
from .repo import clone_or_fetch


def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


_USE_COLOR = _supports_color()


def _c(s: str, code: str) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if _USE_COLOR else s


def cyan(s: str) -> str:   return _c(s, "36")
def green(s: str) -> str:  return _c(s, "32")
def yellow(s: str) -> str: return _c(s, "33")
def red(s: str) -> str:    return _c(s, "31")
def dim(s: str) -> str:    return _c(s, "2")
def bold(s: str) -> str:   return _c(s, "1")


def verdict_color(v: str) -> str:
    return {"solid": green, "caveats": yellow, "problems": red}.get(v, dim)(v.upper())


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="investigator",
        description="Ask questions about a public GitHub repo. REPL commands: :audit :claims :clear :quit",
    )
    parser.add_argument("url", help="public GitHub repo URL")
    parser.add_argument("--model", default="claude-opus-4-5", help="agent model")
    parser.add_argument("--audit-model", default="claude-sonnet-4-5", help="audit model")
    parser.add_argument("--no-audit-display", action="store_true",
                        help="run audits but don't print them automatically")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(red("ANTHROPIC_API_KEY is not set."), file=sys.stderr)
        return 1

    print(dim(f"Cloning {args.url} ..."))
    try:
        repo = clone_or_fetch(args.url)
    except (ValueError, RuntimeError) as e:
        print(red(f"error: {e}"), file=sys.stderr)
        return 1
    print(dim(f"Ready: {repo.slug}  ({repo.path})"))
    print(dim("Ask questions in plain English. Commands: :audit  :claims  :clear  :quit"))
    print()

    client = Anthropic()
    inv = Investigator(
        client=client,
        repo_root=repo.path,
        repo_slug=repo.slug,
        model=args.model,
        audit_model=args.audit_model,
    )

    while True:
        try:
            line = input(bold("you> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue

        if line == ":quit" or line == ":exit":
            return 0
        if line == ":audit":
            if inv.last_audit is None:
                print(dim("(no audit yet — ask a question first)"))
            else:
                print(_render_audit(inv.last_audit))
            print()
            continue
        if line == ":claims":
            text = inv.ledger.render() or "(no claims yet)"
            print(dim(text))
            print()
            continue
        if line == ":clear":
            inv.messages.clear()
            inv.ledger.claims.clear()
            inv.turn = 0
            inv.last_audit = None
            print(dim("(conversation cleared)"))
            print()
            continue
        if line.startswith(":"):
            print(dim(f"unknown command {line!r}"))
            continue

        def on_tool(name, inp, preview):
            arg_str = ", ".join(f"{k}={v!r}" for k, v in inp.items())
            print(dim(f"  · {name}({arg_str})"))

        try:
            result = inv.ask(line, on_tool_call=on_tool)
        except KeyboardInterrupt:
            print(dim("\n(interrupted)"))
            continue
        except Exception as e:
            print(red(f"error: {e}"))
            continue

        print()
        print(cyan("claude>"))
        print(_indent(result.answer))
        print()

        if result.audit and not args.no_audit_display:
            print(_render_audit_compact(result.audit))
            print(dim("(type :audit for full audit details)"))
            print()


def _indent(s: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in s.splitlines())


def _render_audit_compact(audit) -> str:
    head = f"  audit: {verdict_color(audit.verdict)}  {audit.summary}"
    bad = [c for c in audit.citation_report.checks if not c.ok]
    if bad:
        head += dim(f"  [{len(bad)} citation issue(s)]")
    return head


def _render_audit(audit) -> str:
    head = f"  AUDIT VERDICT: {verdict_color(audit.verdict)}"
    body = audit.render()
    return head + "\n" + _indent(body, "  ")


if __name__ == "__main__":
    sys.exit(main())
