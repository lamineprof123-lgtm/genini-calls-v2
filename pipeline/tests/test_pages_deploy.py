"""In-loop Pages deploy: pages_build_version must be a real commit that changes every deploy (the data-branch HEAD),
never the bare GITHUB_SHA reused for every deploy of a run."""
import asyncio
import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from signals import gha_loop
from signals.config import load_config


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(load_config())
    c["general"]["state_dir"] = str(tmp_path / "state")
    c["general"]["logs_dir"] = str(tmp_path / "logs")
    c["site"]["dir"] = str(tmp_path)
    return c

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def _version(env):
    js = ("import { buildVersion } from %s; console.log(JSON.stringify(buildVersion(%s)));"
          % (json.dumps(str(TOOLS / "pages-version.mjs")), json.dumps(env)))
    out = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_build_version_prefers_data_commit():
    sha, data = "104c39118ef7e03d97096e637fc7d27696ef6fff", "6d5f2f71" + "a" * 32
    assert _version({"GITHUB_SHA": sha, "PAGES_BUILD_VERSION": data}) == data
    assert _version({"GITHUB_SHA": sha, "PAGES_BUILD_VERSION": "not-a-sha"}) == sha
    assert _version({"GITHUB_SHA": sha}) == sha


def test_deployer_does_not_send_bare_github_sha():
    src = (TOOLS / "deploy-pages.mjs").read_text()
    assert "pages_build_version: buildVersion()" in src
    assert "pages_build_version: process.env.GITHUB_SHA" not in src


def _repo(path: Path) -> Path:
    path.mkdir()
    for a in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *a], cwd=path, check=True)
    return path


def _commit(path: Path, n: int) -> str:
    (path / "f.txt").write_text(str(n))
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"c{n}"], cwd=path, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True).stdout.strip()


def test_loop_uses_data_head_and_skips_already_deployed_commit(cfg, tmp_path, monkeypatch):
    data = _repo(tmp_path / "data")
    lp = gha_loop.GhaLoop(cfg, data, "t", "holder", dry_run=True, deploy=True)
    sent = []

    async def fake_exec(*args, env=None, **kw):
        sent.append(env["PAGES_BUILD_VERSION"])

        class P:
            async def communicate(self):
                return (json.dumps({"ok": True, "ms": 1}).encode(), None)
        return P()

    monkeypatch.setattr(gha_loop.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(lp, "stage_site", lambda: Path(tempfile_dir(tmp_path)))
    c1 = _commit(data, 1)
    assert asyncio.run(lp.deploy())["ok"] and sent == [c1]
    assert asyncio.run(lp.deploy()) is None and sent == [c1]          # same commit: not redeployed
    c2 = _commit(data, 2)
    assert asyncio.run(lp.deploy())["ok"] and sent == [c1, c2] and c1 != c2


def tempfile_dir(tmp_path: Path) -> str:
    import tempfile
    return tempfile.mkdtemp(dir=tmp_path)
