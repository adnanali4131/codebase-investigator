# Codebase Investigator

A CLI agent that answers questions about a public GitHub repo, with citations.

Point it at a repo, ask things in plain English, and it answers with `file:line` references grounded in the code. Every non-trivial answer gets an independent audit pass that re-checks the citations and flags reasoning holes. Example: *"how does auth work in this repo?"* → answer with `auth.py:40-72` style references, plus an audit verdict.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python -m investigator https://github.com/tiangolo/fastapi
```

Needs `git` and `ripgrep` on PATH.

## Usage

| Command   | What it does                                     |
|-----------|--------------------------------------------------|
| `:audit`  | show the audit for the last answer               |
| `:claims` | show the running claims ledger                   |
| `:clear`  | reset the conversation, keep the cloned repo     |
| `:quit`   | exit                                             |

Anything else is treated as a question.

```
you> where is the dependency injection wired up?
  · search(query='Depends', path_glob='*.py')
  · read_file(path='fastapi/dependencies/utils.py', start=1, end=120)

claude>
  Dependencies are resolved in `fastapi/dependencies/utils.py:solve_dependencies`,
  which walks the parameter tree and calls each `Depends(...)` provider. The entry
  point is `fastapi/routing.py:218-260` where the route handler invokes it before
  calling the endpoint.

  audit: SOLID  citations check out; one minor caveat noted
```

## How it works

**Agent loop.** Four tools — `list_dir`, `read_file`, `search`, `outline` — all return line-numbered output. The agent reads code until it has an answer; no embeddings, no vector index. Ripgrep is fast enough that smarter retrieval doesn't pay for itself at this scale, and line numbers from real reads make citations checkable.

**Audit.** Self-scoring in the same call doesn't count, so the audit is a separate Claude call with a fresh context and a different system prompt. It sees the question, the answer, and a programmatic citation check (file exists? line range valid?) — but not the agent's reasoning trace. It has the same tools and re-verifies the harder claims itself. Verdict is `solid`, `caveats`, or `problems`.

**Claims ledger.** After each answer, a small extraction call pulls structured `{claim, citation}` entries into a ledger that's injected into the system prompt on every subsequent turn. So when the user pushes back on turn 9 — *"earlier you said Z"* — the agent sees its own prior commitments as data, not buried in scrollback.

## Tradeoffs

- No web UI. The CLI is the demo.
- No embeddings. Ripgrep + the agent picking files beats vector search for code Q&A and gives line-grounded citations for free.
- One repo per session. In-memory state, gone on exit.
- `outline` uses regexes for ~6 languages. Tree-sitter would be cleaner but wasn't worth the build complexity here.
- Repos cap at 200 MB cloned. Bigger repos need shallow strategies this isn't built for.
