// pages_build_version for POST /repos/{o}/{r}/pages/deployments. It must be a commit SHA that exists in the repo
// (anything else -> HTTP 404) and it becomes the deployment id the status poll reads. Re-using GITHUB_SHA for every
// in-loop deploy made all deploys of a run share one version: GitHub stopped rolling out repeat deploys of an
// already-deployed version while the status poll kept reading the old "succeed" (panel froze at 16:30 Dubai on
// 2026-11-13, every deploy "succeeded" in ~5 s). The loop therefore passes PAGES_BUILD_VERSION = the current
// pipeline-data commit (a new real commit every cycle); GITHUB_SHA is only the fallback.
export function buildVersion(env = process.env) {
  const v = String(env.PAGES_BUILD_VERSION || '').trim().toLowerCase();
  if (/^[0-9a-f]{40}$/.test(v)) return v;
  return String(env.GITHUB_SHA || '').trim();
}
