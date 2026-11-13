// pages_build_version for POST /repos/{o}/{r}/pages/deployments. GitHub requires it to be UNIQUE per deployment
// and returns it as the deployment id that status polls use. Re-using GITHUB_SHA for every in-loop deploy makes
// every deploy of a run share one id: the status poll then reads an older "succeed" while the new deployment
// never rolls out (the panel froze at 16:30 Dubai on 2026-11-13 while every deploy "succeeded" in ~5 s).
export function buildVersion(sha, name) {
  const base = String(sha || 'local').trim() || 'local';
  const tag = String(name || '').replace(/[^A-Za-z0-9_-]/g, '-');
  return tag ? `${base}-${tag}` : `${base}-${Date.now()}`;
}
