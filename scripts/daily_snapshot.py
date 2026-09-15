#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Daily live-stream snapshot recovery (GitHub Actions runner).

Ambil file playlist snapshot (baris `file 'https://.../stream-720-<ts>.ts'`),
download semua segmen yang masih hidup di CDN, concat -c copy -> MP4,
generate thumbnail, kirim sendVideo ke Telegram.

Env:
  PLAYLIST_URL  (wajib) URL raw .txt
  BOT_TOKEN     wajib
  CHAT_ID       wajib
  TG_API_URL    opsional (bot api server lokal utk >50MB, fallback api.telegram.org)
"""
import os, sys, json, time, shutil, mimetypes, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

WIB = timezone(timedelta(hours=7))
PLAYLIST_URL = os.environ.get("PLAYLIST_URL", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("CHAT_ID", "").strip()
API_URLS = [u for u in [os.environ.get("TG_API_URL", "").rstrip("/"), "https://api.telegram.org"] if u]
OUT_DIR = "/tmp/snap"
SEG_DIR = os.path.join(OUT_DIR, "segs")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
H2ID = {"Indonesia", "id"}


def log(m):
    print(f"[{datetime.now(WIB).strftime('%H:%M:%S')}] {m}", flush=True)


def http_get(url, timeout=60, binary=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = r.read()
    return d if binary else d.decode("utf-8", "replace")


def send_msg(text):
    for base in API_URLS:
        try:
            data = json.dumps({"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
            req = urllib.request.Request(f"{base}/bot{BOT_TOKEN}/sendMessage", data=data,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30).read()
            return True
        except Exception as e:
            log(f"msg gagal via {base[:34]}: {e}")
    return False


def parse_urls(text):
    urls = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#"):
            continue
        if "file '" in ln:
            u = ln.split("file '", 1)[1].rsplit("'", 1)[0]
        else:
            u = ln
        u = u.strip()
        if u.startswith("http") and ".ts" in u.split("?")[0]:
            urls.append(u)
    return urls


def seg_name(url):
    return url.split("?")[0].rsplit("/", 1)[-1]


def dl(url):
    dest = os.path.join(SEG_DIR, seg_name(url))
    for _ in range(2):
        try:
            data = http_get(url, timeout=90, binary=True)
            if data and len(data) > 1000:
                with open(dest, "wb") as f:
                    f.write(data)
                return True
        except Exception:
            time.sleep(1)
    return False


def main():
    if not PLAYLIST_URL or not BOT_TOKEN or not CHAT_ID:
        log("PLAYLIST_URL/BOT_TOKEN/CHAT_ID kosong"); sys.exit(1)
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    os.makedirs(SEG_DIR, exist_ok=True)

    send_msg("⏳ <b>Daily snapshot</b> dimulai (GitHub Runner) — ambil playlist...")
    try:
        text = http_get(PLAYLIST_URL, timeout=30)
    except Exception as e:
        send_msg(f"❌ Snapshot gagal: playlist tidak bisa diambil\n<code>{e}</code>"); sys.exit(1)

    urls = parse_urls(text)
    if not urls:
        send_msg("❌ Snapshot: playlist kosong / tidak ada .ts"); sys.exit(1)
    ep = [int("".join(c for c in seg_name(u) if c.isdigit())[-10:]) for u in urls]
    ep = [e for e in ep if e > 1000000000]
    t0, t1 = (min(ep), max(ep)) if ep else (0, 0)
    log(f"playlist: {len(urls)} segmen, {datetime.fromtimestamp(t0,WIB):%d/%m %H:%M}→{datetime.fromtimestamp(t1,WIB):%H:%M} WIB")

    ok = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        for r in ex.map(dl, urls):
            ok += 1 if r else 0
    missing = len(urls) - ok
    pct = ok * 100 // max(len(urls), 1)
    log(f"download ok={ok} missing={missing} ({pct}%)")
    if ok < 30:
        send_msg(f"❌ Snapshot dibatalkan: hanya {ok} segmen hidup ({pct}%) — CDN sudah prune total.")
        sys.exit(2)

    segs = sorted(f for f in os.listdir(SEG_DIR) if f.endswith(".ts") and os.path.getsize(os.path.join(SEG_DIR, f)) > 1000)
    listfile = os.path.join(OUT_DIR, "concat.txt")
    with open(listfile, "w") as f:
        for s in segs:
            f.write(f"file '{os.path.join(SEG_DIR, s)}'\n")

    d0 = datetime.fromtimestamp(t0, WIB)
    fname = f"Rekaman_{d0.strftime('%A, %d %B %Y')}_{d0.strftime('%H-%M')}.mp4"
    out = os.path.join(OUT_DIR, fname)
    log(f"concat {len(segs)} segmen -> {fname}")
    r = os.system(f"ffmpeg -y -hide_banner -loglevel error -f concat -safe 0 -i '{listfile}' -c copy '{out}'")
    if r != 0 or not os.path.isfile(out) or os.path.getsize(out) < 102400:
        send_msg("❌ Snapshot: ffmpeg concat gagal"); sys.exit(3)

    size = os.path.getsize(out)
    try:
        dur = float(os.popen(f"ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 '{out}'").read().strip())
    except Exception:
        dur = len(segs) * 2.0

    h, rem = divmod(int(dur), 3600); m, s = divmod(rem, 60)
    hdur = f"{h}j{m:02d}m" if h else f"{m}m{s:02d}s"

    # Thumbnail di tengah
    thumb = os.path.join(OUT_DIR, "thumb.jpg")
    os.system(f"ffmpeg -y -hide_banner -loglevel error -ss {int(dur/2)} -i '{out}' -frames:v 1 -q:v 3 '{thumb}'")
    has_thumb = os.path.isfile(thumb) and os.path.getsize(thumb) > 1000

    caption = (f"📼 <b>{fname}</b>\n"
               f"🕐 Periode: {datetime.fromtimestamp(t0,WIB):%d/%m/%Y %H:%M}–{datetime.fromtimestamp(t1,WIB):%H:%M} WIB\n"
               f"⏱ {hdur} · 📦 {size/1024/1024:.0f} MB · 🧩 {ok}/{len(urls)} segmen ({pct}%)")

    if size > 1950 * 1024 * 1024:
        send_msg(f"⚠️ Snapshot: hasil {size/1024/1024:.0f} MB melebihi limit 2GB Bot API, tidak dikirim utuh.")
        sys.exit(4)

    sent = False
    for base in API_URLS:
        try:
            bd = "s" + uuid_boundary()
            parts = []
            def fd(name, val):
                return (f"--{bd}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{val}\r\n").encode()
            parts.append(fd("chat_id", CHAT_ID))
            parts.append(fd("caption", caption))
            parts.append(fd("parse_mode", "HTML"))
            parts.append(fd("supports_streaming", "true"))
            if has_thumb:
                parts.append((f"--{bd}\r\nContent-Disposition: form-data; name=\"thumbnail\"; filename=\"t.jpg\"\r\n"
                              f"Content-Type: image/jpeg\r\n\r\n").encode() + open(thumb, "rb").read() + b"\r\n")
            parts.append((f"--{bd}\r\nContent-Disposition: form-data; name=\"video\"; filename=\"{fname}\"\r\n"
                          f"Content-Type: video/mp4\r\n\r\n").encode() + open(out, "rb").read() + b"\r\n")
            parts.append(f"--{bd}--\r\n".encode())
            body = b"".join(parts)
            req = urllib.request.Request(f"{base}/bot{BOT_TOKEN}/sendVideo", data=body,
                                         headers={"Content-Type": f"multipart/form-data; boundary={bd}"})
            resp = json.loads(urllib.request.urlopen(req, timeout=3600).read())
            if resp.get("ok"):
                sent = True
                log(f"terkirim via {base[:34]}")
                break
        except Exception as e:
            log(f"sendVideo gagal via {base[:34]}: {e}")
    if not sent:
        send_msg(f"⚠️ Snapshot: MP4 dibuat ({size/1024/1024:.0f} MB) tapi upload GAGAL semua endpoint.")
        sys.exit(5)
    send_msg(f"✅ <b>Daily snapshot selesai</b>\n📦 {size/1024/1024:.0f} MB · {hdur} · {ok} segmen")
    shutil.rmtree(OUT_DIR, ignore_errors=True)


def uuid_boundary():
    import random, string
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=28))


if __name__ == "__main__":
    main()
