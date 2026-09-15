#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Heartbeat anti auto-disable.

GitHub men-disable schedule/cron workflow jika tidak ada commit biasa
(bukan commit dari GITHUB_TOKEN / bukan [skip ci]) selama 60 hari.
Workflow daily-snapshot butuh dijangkarkan oleh commit tiap <=60 hari.

Push file heartbeat.txt ke branch `heartbeat` (bukan main) -> repo dianggap
aktif, tapi branch utama tetap bersih. Dipanggil workflow heartbeat.yml tiap
25 hari dengan GH_TOKEN dari secret GITHUB_PAT."""
import base64, datetime, json, os, sys, urllib.request, urllib.error

API = "https://api.github.com"


def gh(method, path, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "heartbeat-action"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"GitHub {method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')[:200]}\n")
        raise


def main():
    repo = os.environ.get("GITHUB_REPOSITORY") or sys.exit("GITHUB_REPOSITORY missing")
    token = os.environ.get("GH_TOKEN") or sys.exit("GH_TOKEN missing")

    # pastikan branch heartbeat ada (buat dari default branch jika belum)
    try:
        gh("GET", f"/repos/{repo}/branches/heartbeat", token)
    except urllib.error.HTTPError:
        default = gh("GET", f"/repos/{repo}", token)["default_branch"]
        sha = gh("GET", f"/repos/{repo}/git/ref/heads/{default}", token)["object"]["sha"]
        try:
            gh("POST", f"/repos/{repo}/git/refs", token,
               {"ref": "refs/heads/heartbeat", "sha": sha})
        except urllib.error.HTTPError:
            pass  # race: sudah dibuat runner lain

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"message": f"chore: keep repo active {now}",
            "content": base64.b64encode(f"heartbeat {now}\n".encode()).decode(),
            "branch": "heartbeat"}
    try:
        cur = gh("GET", f"/repos/{repo}/contents/heartbeat.txt?ref=heartbeat", token)
        if cur.get("sha"):
            body["sha"] = cur["sha"]
    except urllib.error.HTTPError:
        pass  # file belum ada = create baru
    out = gh("PUT", f"/repos/{repo}/contents/heartbeat.txt", token, body)
    print(f"heartbeat push OK -> branch heartbeat @{out['commit']['sha'][:8]}")


if __name__ == "__main__":
    main()
