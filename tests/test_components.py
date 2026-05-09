"""
Tests for everything that doesn't need a Claude API call:
  - URL parsing
  - tools (list_dir, read_file, search, outline) on a fixture repo
  - citation extraction & verification
  - claims ledger
  - audit JSON parsing edge cases

Run with:  python -m unittest tests.test_components
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from investigator import tools
from investigator.audit import (
    CitationReport, _extract_citations, _parse_audit_json, verify_citations,
)
from investigator.memory import Ledger
from investigator.repo import parse_github_url


# ---------- fixture repo ----------

def make_fixture_repo() -> Path:
    """Create a small fake repo on disk for tool tests."""
    root = Path(tempfile.mkdtemp(prefix="investigator-test-"))
    (root / "src").mkdir()
    (root / "src" / "auth.py").write_text(
        "import jwt\n"
        "\n"
        "SECRET = 'hardcoded-please-fix'  # line 3\n"
        "\n"
        "def verify_token(token: str) -> dict:\n"
        "    return jwt.decode(token, SECRET, algorithms=['HS256'])\n"
        "\n"
        "async def login(username, password):\n"
        "    user = await db.find_user(username)\n"
        "    if not user:\n"
        "        return None\n"
        "    return jwt.encode({'sub': user.id}, SECRET, algorithm='HS256')\n"
    )
    (root / "src" / "middleware.js").write_text(
        "export function authMiddleware(req, res, next) {\n"
        "  const token = req.headers.authorization;\n"
        "  next();\n"
        "}\n"
        "\n"
        "export const PUBLIC_PATHS = ['/login', '/health'];\n"
    )
    (root / "README.md").write_text("# Fixture repo\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_text("should not be searched")
    return root


# ---------- URL parsing ----------

class TestURLParsing(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse_github_url("https://github.com/foo/bar"), ("foo", "bar", None))

    def test_with_dot_git(self):
        self.assertEqual(parse_github_url("https://github.com/foo/bar.git"), ("foo", "bar", None))

    def test_with_branch(self):
        self.assertEqual(
            parse_github_url("https://github.com/foo/bar/tree/develop"),
            ("foo", "bar", "develop"),
        )

    def test_trailing_slash(self):
        self.assertEqual(parse_github_url("https://github.com/foo/bar/"), ("foo", "bar", None))

    def test_http_works(self):
        self.assertEqual(parse_github_url("http://github.com/foo/bar"), ("foo", "bar", None))

    def test_rejects_non_github(self):
        with self.assertRaises(ValueError):
            parse_github_url("https://gitlab.com/foo/bar")

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_github_url("not a url at all")


# ---------- tools ----------

class TestTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = make_fixture_repo()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.repo, ignore_errors=True)

    def test_list_dir_root(self):
        out = tools.list_dir(self.repo, ".")
        self.assertIn("src/", out)
        self.assertIn("README.md", out)
        # node_modules should be hidden
        self.assertNotIn("node_modules", out)

    def test_list_dir_subdir(self):
        out = tools.list_dir(self.repo, "src")
        self.assertIn("src/auth.py", out)
        self.assertIn("src/middleware.js", out)

    def test_list_dir_rejects_escape(self):
        with self.assertRaises(tools.ToolError):
            tools.list_dir(self.repo, "../../../etc")

    def test_list_dir_missing(self):
        with self.assertRaises(tools.ToolError):
            tools.list_dir(self.repo, "does-not-exist")

    def test_read_file_full(self):
        out = tools.read_file(self.repo, "src/auth.py")
        # has line numbers
        self.assertIn("1  import jwt", out)
        self.assertIn("3  SECRET = 'hardcoded-please-fix'", out)
        # has header
        self.assertIn("src/auth.py", out)

    def test_read_file_range(self):
        out = tools.read_file(self.repo, "src/auth.py", start=5, end=6)
        self.assertIn("5  def verify_token", out)
        self.assertIn("6  ", out)
        # should not contain line 1
        self.assertNotIn("1  import jwt", out)

    def test_read_file_past_eof(self):
        out = tools.read_file(self.repo, "src/auth.py", start=9999)
        self.assertIn("past EOF", out)

    def test_read_file_missing(self):
        with self.assertRaises(tools.ToolError):
            tools.read_file(self.repo, "nope.py")

    def test_search_finds_match(self):
        out = tools.search(self.repo, "hardcoded-please-fix")
        self.assertIn("auth.py", out)
        self.assertIn(":3:", out)

    def test_search_skips_node_modules(self):
        out = tools.search(self.repo, "should not be searched")
        # the string itself should not appear in the results because the file
        # lives under node_modules and we ignore that dir
        self.assertIn("no matches", out.lower())

    def test_search_with_glob(self):
        out = tools.search(self.repo, "token", path_glob="*.js")
        self.assertIn("middleware.js", out)
        self.assertNotIn("auth.py", out)

    def test_outline_python(self):
        out = tools.outline(self.repo, "src/auth.py")
        self.assertIn("def verify_token", out)
        self.assertIn("async def login", out)

    def test_outline_js(self):
        out = tools.outline(self.repo, "src/middleware.js")
        self.assertIn("authMiddleware", out)
        self.assertIn("PUBLIC_PATHS", out)


# ---------- citation extraction & verification ----------

class TestCitations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = make_fixture_repo()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.repo, ignore_errors=True)

    def test_extract_simple(self):
        cites = _extract_citations("see `src/auth.py:5-6` for the verify function")
        self.assertEqual(len(cites), 1)
        _, path, start, end = cites[0]
        self.assertEqual(path, "src/auth.py")
        self.assertEqual(start, 5)
        self.assertEqual(end, 6)

    def test_extract_single_line(self):
        cites = _extract_citations("the bug is on src/auth.py:3.")
        self.assertEqual(len(cites), 1)
        _, path, start, end = cites[0]
        self.assertEqual(start, 3)
        self.assertEqual(end, 3)

    def test_extract_multiple(self):
        text = "look at `auth.py:5-6` and also `middleware.js:1`"
        cites = _extract_citations(text)
        self.assertEqual(len(cites), 2)

    def test_extract_dedupes(self):
        text = "see auth.py:5-6 ... again, auth.py:5-6"
        cites = _extract_citations(text)
        self.assertEqual(len(cites), 1)

    def test_extract_ignores_word_with_colon_number(self):
        # Should NOT match "TODO:42" or similar — we require a file extension
        text = "TODO:42 not a citation"
        cites = _extract_citations(text)
        self.assertEqual(len(cites), 0)

    def test_extract_handles_swapped_range(self):
        cites = _extract_citations("auth.py:6-5")
        _, _, start, end = cites[0]
        self.assertEqual(start, 5)
        self.assertEqual(end, 6)

    def test_verify_good_citation(self):
        report = verify_citations(self.repo, "see `src/auth.py:5-6` for verify_token")
        self.assertTrue(report.all_ok)
        self.assertEqual(len(report.checks), 1)

    def test_verify_missing_file(self):
        report = verify_citations(self.repo, "see `src/nope.py:5-6`")
        self.assertFalse(report.all_ok)
        self.assertIn("does not exist", report.checks[0].reason)

    def test_verify_past_eof(self):
        report = verify_citations(self.repo, "see `src/auth.py:9999`")
        self.assertFalse(report.all_ok)
        self.assertIn("past EOF", report.checks[0].reason)

    def test_verify_no_citations(self):
        report = verify_citations(self.repo, "I have no citations whatsoever.")
        self.assertTrue(report.all_ok)  # vacuously
        self.assertEqual(len(report.checks), 0)


# ---------- claims ledger ----------

class TestLedger(unittest.TestCase):
    def test_add_and_render(self):
        l = Ledger()
        l.add(1, [{"claim": "Auth uses HS256", "citation": "auth.py:5-6"}])
        l.add(2, [{"claim": "Tokens come from header", "citation": None}])
        text = l.render()
        self.assertIn("turn 1", text)
        self.assertIn("HS256", text)
        self.assertIn("auth.py:5-6", text)
        self.assertIn("turn 2", text)

    def test_empty_ledger_renders_empty(self):
        self.assertEqual(Ledger().render(), "")

    def test_skips_blank_claims(self):
        l = Ledger()
        l.add(1, [{"claim": "", "citation": "x.py:1"}])
        self.assertEqual(len(l.claims), 0)

    def test_handles_bad_citation_type(self):
        l = Ledger()
        l.add(1, [{"claim": "ok", "citation": ["not", "a", "string"]}])
        self.assertEqual(l.claims[0].citation, None)


# ---------- audit JSON parsing ----------

class TestAuditParse(unittest.TestCase):
    def _empty_report(self):
        return CitationReport()

    def test_parses_clean_json(self):
        text = '{"verdict": "solid", "summary": "looks good", "checked": [], "issues": []}'
        r = _parse_audit_json(text, self._empty_report())
        self.assertEqual(r.verdict, "solid")

    def test_parses_fenced_json(self):
        text = '```json\n{"verdict": "caveats", "summary": "ok"}\n```'
        r = _parse_audit_json(text, self._empty_report())
        self.assertEqual(r.verdict, "caveats")

    def test_parses_with_preamble(self):
        text = 'Here is my audit:\n{"verdict": "problems", "summary": "wrong"}'
        r = _parse_audit_json(text, self._empty_report())
        self.assertEqual(r.verdict, "problems")

    def test_handles_garbage(self):
        r = _parse_audit_json("not json at all", self._empty_report())
        self.assertEqual(r.verdict, "error")

    def test_downgrades_solid_when_citations_bad(self):
        """If LLM says 'solid' but programmatic check failed, we should downgrade."""
        from investigator.audit import CitationCheck
        bad_report = CitationReport(checks=[
            CitationCheck("x.py:1", "x.py", 1, 1, ok=False, reason="missing"),
        ])
        text = '{"verdict": "solid", "summary": "looks good"}'
        r = _parse_audit_json(text, bad_report)
        self.assertEqual(r.verdict, "problems")
        self.assertIn("Downgrading", r.summary)


if __name__ == "__main__":
    unittest.main()
