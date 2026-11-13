// pages_build_version for POST /repos/{o}/{r}/pages/deployments. GitHub wants it UNIQUE per deployment and returns
// it as the deployment id the status poll uses. Re-using GITHUB_SHA for every in-loop deploy made every deploy of a
// run share one id: GitHub stopped rolling out repeat deploys of an already-deployed version, while the status poll
// kept reading the old "succeed" (panel froze at 16:30 Dubai on 2026-11-13; every deploy "succeeded" in ~5 s).
// A "<sha>-<name>" string was not usable either, so derive a commit-SHA-shaped 40-hex id per deploy.
import { createHash } from 'node:crypto';

export function buildVersion(sha, name) {
  const base = String(sha || 'local').trim() || 'local';
  const tag = String(name || '') || String(Date.now());
  return createHash('sha1').update(`${base}\n${tag}`).digest('hex');
}
