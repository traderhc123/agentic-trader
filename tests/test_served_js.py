"""The wizard/dashboard <script> blocks must be valid JS *after* Python
string interpretation.

Regression: `split('\\n')` written as split('\n') inside the non-raw
_WIZARD_HTML literal let Python turn the escape into a real newline, which
broke the served script mid-string — the wizard rendered with no tabs at
all. Checking the .py file text misses this class of bug entirely; only the
interpreted string catches it.
"""

import re
import shutil
import subprocess

import pytest

import webui


def _script_blocks(html):
    return re.findall(r"<script>(.*?)</script>", html, re.S)


@pytest.mark.parametrize("page", ["_WIZARD_HTML", "_DASH_HTML"])
def test_served_script_is_valid_js(tmp_path, page):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    blocks = _script_blocks(getattr(webui, page))
    assert blocks, f"no <script> block found in {page}"
    for i, js in enumerate(blocks):
        f = tmp_path / f"{page}-{i}.js"
        f.write_text(js)
        r = subprocess.run([node, "--check", str(f)],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{page} script {i} invalid:\n{r.stderr}"
