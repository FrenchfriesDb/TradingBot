"""
Hosts per-trade chart PNGs on a dedicated branch of the project's own public GitHub
repo, referenced via raw.githubusercontent.com — used when Google Drive isn't viable
(service accounts have no storage quota and can't accept ownership transfers, see
chart_renderer.py). Talks to the GitHub Contents API directly over HTTPS; never
touches the local git working tree, so it can't collide with uncommitted work or the
branch you're actually developing on.

One-time setup: create a fine-grained GitHub Personal Access Token (Settings ->
Developer settings -> Personal access tokens -> Fine-grained tokens) scoped to just
this repo with Contents: Read and write permission, then set GITHUB_TOKEN in .env.
Every function here fails soft: on any error it prints a warning and returns None,
so a GitHub outage can never block or crash a trading loop.
"""

import base64

import requests

from config import GITHUB_TOKEN, GITHUB_REPO, GITHUB_CHART_BRANCH

_API_ROOT = "https://api.github.com"
_branch_ready = False  # cached: True once ensure_chart_branch() has succeeded this run


def _headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def ensure_chart_branch():
    """Creates the dedicated chart-storage branch if it doesn't exist yet. Idempotent
    — safe to call every time a bot starts. Returns True if the branch is ready to
    receive uploads, False on any failure (missing token, network, permissions)."""
    global _branch_ready
    if _branch_ready:
        return True
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("[GITHUB] GITHUB_TOKEN or GITHUB_REPO not set — chart hosting disabled.")
        return False
    try:
        check = requests.get(
            f"{_API_ROOT}/repos/{GITHUB_REPO}/branches/{GITHUB_CHART_BRANCH}",
            headers=_headers(), timeout=10,
        )
        if check.status_code == 200:
            _branch_ready = True
            return True
        if check.status_code != 404:
            print(f"[GITHUB] Branch check failed: {check.status_code} {check.text}")
            return False

        default = requests.get(f"{_API_ROOT}/repos/{GITHUB_REPO}", headers=_headers(), timeout=10)
        default.raise_for_status()
        default_branch = default.json()["default_branch"]
        head = requests.get(
            f"{_API_ROOT}/repos/{GITHUB_REPO}/git/ref/heads/{default_branch}",
            headers=_headers(), timeout=10,
        )
        head.raise_for_status()
        base_sha = head.json()["object"]["sha"]

        create = requests.post(
            f"{_API_ROOT}/repos/{GITHUB_REPO}/git/refs", headers=_headers(), timeout=10,
            json={"ref": f"refs/heads/{GITHUB_CHART_BRANCH}", "sha": base_sha},
        )
        create.raise_for_status()
        print(f"[GITHUB] Created chart-storage branch '{GITHUB_CHART_BRANCH}'")
        _branch_ready = True
        return True
    except Exception as e:
        print(f"[GITHUB] ensure_chart_branch failed: {e}")
        return False


def upload_chart_to_github(local_path, filename):
    """Uploads a local PNG to the chart-storage branch via a single Contents API
    call (no local git operations). Returns a raw.githubusercontent.com URL usable
    directly in a Sheets =IMAGE() formula, or None on any failure — never raises."""
    if not ensure_chart_branch():
        return None
    try:
        with open(local_path, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("ascii")
        path = f"charts/{filename}"
        resp = requests.put(
            f"{_API_ROOT}/repos/{GITHUB_REPO}/contents/{path}", headers=_headers(), timeout=15,
            json={
                "message": f"Add trade chart {filename}",
                "content": content_b64,
                "branch": GITHUB_CHART_BRANCH,
            },
        )
        resp.raise_for_status()
        return f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_CHART_BRANCH}/{path}"
    except Exception as e:
        print(f"[GITHUB] Chart upload failed: {e}")
        return None
