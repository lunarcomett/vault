# --- heartbeat_commit.py (reusable snippet) ---
#!/usr/bin/env python3
"""Push commit dummy ke branch `heartbeat` di repo ini supaya repo dianggap aktif.
GitHub men-disable schedule/cron workflow yang tidak ada commit (dari user/bukan [skip ci])
selama 60 hari. Commit workflow GAKH dihitung. Ini dijalankan tiap 25 hari oleh
workflow Heartbeat."""
import base64, json, os, sys, urllib.request

API = "https://api.github.com"
REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GH_TOKEN"]


def api(path, data=None, method=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(data).encode() if data is not None else None,
        method=method or ("GET" if data is None else "PUT"),
        headers={"Authorization": "Bearer " + TOKEN,
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "heartbeat-action"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


# 1. pastikan branch heartbeat ada
try:
    ref = api(f"/repos/{REPO}/git/ref/heads/heartbeat")
    sha_head = ref["object"]["sha"]
except Exception:
    base = api(f"/repos/{REPO}/git/ref/heads/main")["object"]["sha"]
    api(f"/repos/{REPO}/git/refs", {"ref": "refs/heads/heartbeat", "sha": base}, "POST")
    sha_head = base

# 2. blob berisi timestamp
blob = api(f"/repos/{REPO}/git/blobs", {
    "content": base64.b64encode(
        f"heartbeat {__import__('datetime').datetime.utcnow().isoformat()}Z\n".encode()
    ).decode(),
    "encoding": "base64",
})

# 3. tree di atas HEAD heartbeat (file tunggal heartbeat.txt)
tree = api(f"/repos/{REPO}/git/trees", {
    "base_tree": sha_head,
    "tree": [{"path": "heartbeat.txt", "mode": "100644", "type": "blob", "sha": blob["sha"]}],
})

# 4. commit + update ref
commit = api(f"/repos/{REPO}/git/commits", {
    "message": f"chore: keep repo active {__import__('datetime').datetime.utcnow():%Y-%m-%d}",
    "tree": tree["sha"],
    "parents": [sha_head],
})
api(f"/repos/{REPO}/git/refs/heads/heartbeat",
    {"sha": commit["sha"], "force": True}, "PATCH")
print("heartbeat push OK:", commit["sha"][:8])
