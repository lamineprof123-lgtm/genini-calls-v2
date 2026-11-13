"""In-loop Pages deploy must send a unique pages_build_version per deploy (not the bare GITHUB_SHA)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _versions(sha, names):
    js = ("import { buildVersion } from %s; console.log(JSON.stringify(%s.map(n => buildVersion(%s, n))));"
          % (json.dumps(str(TOOLS / "pages-version.mjs")), json.dumps(names), json.dumps(sha)))
    out = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_build_version_unique_per_deploy_same_sha():
    sha = "104c39118ef7e03d97096e637fc7d27696ef6fff"
    v = _versions(sha, ["pages-1-8-1794573037", "pages-1-9-1794573144", "pages-1-9-1794573144"])
    assert v[0] != v[1] and v[1] == v[2]
    assert all(x != sha and len(x) == 40 and all(c in "0123456789abcdef" for c in x) for x in v)
    assert _versions("de3fd0f63780fb2e1e21b2287eb67842c6eb46fb", ["pages-1-9-1794573144"])[0] != v[1]


def test_deployer_uses_build_version_not_bare_sha():
    src = (TOOLS / "deploy-pages.mjs").read_text()
    assert "pages_build_version: buildVersion(process.env.GITHUB_SHA, name)" in src
    assert "pages_build_version: process.env.GITHUB_SHA" not in src
