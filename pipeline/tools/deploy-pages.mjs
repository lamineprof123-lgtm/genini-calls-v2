// Deploy a static folder to GitHub Pages from INSIDE a running job (no extra workflow steps), so the
// long-running signal loop can publish after every cycle. Same API calls as actions/upload-pages-artifact
// + actions/deploy-pages: upload artifact.tar as a run artifact, then POST /pages/deployments with an OIDC token.
// Needs: ACTIONS_RUNTIME_TOKEN + ACTIONS_RESULTS_URL (exported by an earlier actions/github-script step),
// ACTIONS_ID_TOKEN_REQUEST_* (job has id-token: write), GITHUB_TOKEN with pages: write, GITHUB_REPOSITORY.
// Usage: node deploy-pages.mjs <site_dir> <artifact_name>   -> prints one JSON line {ok, ...}
import { DefaultArtifactClient } from '@actions/artifact';
import * as core from '@actions/core';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { buildVersion } from './pages-version.mjs';

const [, , siteDir, name] = process.argv;
const repo = process.env.GITHUB_REPOSITORY;
const token = process.env.GITHUB_TOKEN || process.env.GH_TOKEN;
const t0 = Date.now();
const out = { ok: false, name };
const done = (extra) => { Object.assign(out, extra, { ms: Date.now() - t0 }); console.log(JSON.stringify(out)); process.exit(out.ok ? 0 : 1); };

const api = async (p, opt = {}) => {
  const r = await fetch(`https://api.github.com${p}`, {
    ...opt,
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json' },
  });
  const text = await r.text();
  let body; try { body = JSON.parse(text); } catch { body = text; }
  return { status: r.status, body };
};

try {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'pages-'));
  const tar = path.join(tmp, 'artifact.tar');
  execFileSync('tar', ['--dereference', '--hard-dereference', '--exclude=.git', '-cf', tar, '-C', siteDir, '.']);
  const client = new DefaultArtifactClient();
  const up = await client.uploadArtifact(name, [tar], tmp, { retentionDays: 1, compressionLevel: 6 });
  out.artifact_id = up.id; out.upload_ms = Date.now() - t0;
  fs.rmSync(tmp, { recursive: true, force: true });
  const oidc = await core.getIDToken();
  const cr = await api(`/repos/${repo}/pages/deployments`, {
    method: 'POST',
    body: JSON.stringify({ artifact_id: up.id, pages_build_version: buildVersion(), oidc_token: oidc }),
  });
  if (cr.status >= 300) done({ stage: 'create', http: cr.status, error: JSON.stringify(cr.body).slice(0, 300) });
  const dep = cr.body;
  out.create_http = cr.status; out.create_body = JSON.stringify(dep).slice(0, 200);
  const id = dep.id || String(dep.status_url || '').split('/').pop();
  out.deployment_id = id;
  out.build_version = buildVersion();
  const failed = new Set(['deployment_failed', 'deployment_content_failed', 'deployment_cancelled', 'deployment_lost',
                          'deployment_perms_error']);
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, 3000));
    const st = await api(`/repos/${repo}/pages/deployments/${id}`);
    const s = st.body && st.body.status;
    out.last_status = s; out.last_http = st.status;
    if (s === 'succeed') done({ ok: true, status: s });
    if (failed.has(s)) done({ stage: 'status', status: s, http: st.status });
  }
  done({ stage: 'status', status: 'timeout' });
} catch (e) {
  done({ stage: out.artifact_id ? 'deploy' : 'upload', error: String(e && e.message || e).slice(0, 300) });
}
