import shutil
import subprocess

import pytest

from cards import look


def test_app_js_parses(tmp_path):
    """A stray escape in the Python string once shipped a script the browser could not parse at all."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    for name, src in (("app.js", look.JS), ("theme.js", look.THEME_JS)):
        f = tmp_path / name
        f.write_text(src)
        r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{name}: {r.stderr[:400]}"


def test_every_module_imports():
    """The page modules are only imported by the server: a syntax slip there must fail the tests, not the deploy."""
    import importlib
    for m in ("web", "spendweb", "advisorweb", "goals", "points", "ask", "fees", "links", "awards"):
        importlib.import_module(f"cards.{m}")
