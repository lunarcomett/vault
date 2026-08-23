import json, os, urllib.request, mimetypes, uuid

chat_id = os.environ["CHAT_ID"]
bot_token = os.environ["BOT_TOKEN"]
filename = os.environ["FILENAME"]
human_dur = os.environ.get("HUMAN_DUR", "?")
tag = os.environ.get("TAG", "")
repo = os.environ.get("GITHUB_REPOSITORY", "")
server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
run_id = os.environ.get("GITHUB_RUN_ID", "")
job_status = os.environ.get("JOB_STATUS", "")
phase = os.environ.get("PHASE", "both")

# ORV ID (dari worker) — biar user gampang rujuk /cancel
orv_id = os.environ.get("ORV_ID", "") or ""

def id_line():
    return f"🆔 ID: <code>{orv_id}</code>\n" if orv_id else ""

# Metadata original (dari step ffprobe; default "?" kalau kosong)
real_duration = os.environ.get("REAL_DURATION", "") or "?"
resolution = os.environ.get("RESOLUTION", "") or "?"
vcodec = os.environ.get("VCODEC", "") or "?"
vbitrate = os.environ.get("VBITRATE", "") or "?"
file_size = os.environ.get("FILE_SIZE", "") or "?"
orig_bytes = int(os.environ.get("ORIG_BYTES", "0") or "0")
thumb_file = os.environ.get("THUMB_FILE", "")
has_thumb = os.environ.get("HAS_THUMB", "0") == "1"

# Preset HEVC + durasi original (buat estimasi ETA encode terpisah)
hevc_preset = (os.environ.get("HEVC_PRESET", "") or "veryfast").strip() or "veryfast"
hevc_crf = (os.environ.get("HEVC_CRF", "") or "24").strip() or "24"

# Faktor realtime (720p60, kalibrasi bench): ultrafast~3.5x, veryfast~2.5x, slow~1x, slower~0.5x
RT_FACTOR = {
    "ultrafast": 3.5, "superfast": 3.0, "veryfast": 2.5, "faster": 2.0,
    "fast": 1.5, "medium": 1.2, "slow": 1.0, "slower": 0.5, "veryslow": 0.3,
}

def estimate_encode(dur_str):
    """Hitung estimasi encode dari durasi + preset. Return (eta_min, rt)."""
    # parse durasi "4 jam" / "2h19m" / "01:23:45"
    import re as _re
    secs = 0
    mm = _re.search(r"(\d+)\s*jam", dur_str)
    if mm: secs += int(mm.group(1)) * 3600
    mm = _re.search(r"(\d+)\s*menit", dur_str)
    if mm: secs += int(mm.group(1)) * 60
    mm = _re.search(r"(\d+)\s*detik", dur_str)
    if mm: secs += int(mm.group(1))
    mm = _re.search(r"(\d+):(\d+):(\d+)", dur_str)
    if mm: secs = int(mm.group(1))*3600 + int(mm.group(2))*60 + int(mm.group(3))
    rt = RT_FACTOR.get(hevc_preset, 2.5)
    enc_secs = int(secs / rt) if rt > 0 else secs
    # overhead: download original + upload HEVC (kasar 15 menit buat file besar)
    enc_secs += 15 * 60
    return enc_secs, rt

def fmt_dur(s):
    h = s // 3600; m = (s % 3600) // 60
    if h: return f"{h}j{m}m"
    return f"{m}m"

def eta_clock(mins_from_now):
    from datetime import datetime, timedelta
    # WIB = UTC+7
    t = datetime.utcnow() + timedelta(minutes=mins_from_now + 7 * 60)
    return t.strftime("%H:%M") + " WIB"

hevc_file = os.environ.get("HEVC_FILE", "")
hevc_size = os.environ.get("HEVC_SIZE", "")
hevc_res = os.environ.get("HEVC_RES", "") or resolution
hevc_codec = os.environ.get("HEVC_VCODEC", "") or "hevc"
hevc_br = os.environ.get("HEVC_VBITRATE", "") or "?"
hevc_dur = os.environ.get("HEVC_DUR", "") or real_duration
# HEVC thumbnail (diperlukan agar sendVideo tidak crash & muncul preview)
hevc_thumb_file = os.environ.get("HEVC_THUMB_FILE", "")
has_hevc_thumb = os.environ.get("HAS_HEVC_THUMB", "0") == "1"

# === CHAPTER MARKERS (dari run_hevc_encode.sh) ===
chapters_text = os.environ.get("CHAPTERS_TEXT", "").strip()

def chapters_block():
    """Format chapter list untuk caption Telegram."""
    if not chapters_text:
        return ""
    lines = [l.strip() for l in chapters_text.split("\n") if l.strip()]
    if not lines:
        return ""
    block = "\n\n📌 <b>Chapters:</b>\n"
    for l in lines:
        block += f"  {l}\n"
    return block

run_url = f"{server_url}/{repo}/actions/runs/{run_id}"
release_url = f"https://github.com/{repo}/releases/tag/{tag}" if tag else ""

# Default ke local Bot API Server (allow 2GB). Token disisipkan di path /bot<TOKEN>.
TG_URL = os.environ.get('TG_API_URL', 'https://api.telegram.org').rstrip('/')
API = f"{TG_URL}/bot{bot_token}"
FALLBACK_API = f"https://api.telegram.org/bot{bot_token}"
print(f"ℹ️  Notify start | phase={phase} | API={API[:40]}... | chat={chat_id} | job={job_status} | file={filename} | hevc={'yes' if hevc_file else 'no'}", flush=True)


def send_message(text):
    """Kirim pesan teks biasa (fallback)."""
    data = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    for api_url in [API, FALLBACK_API]:
        try:
            req = urllib.request.Request(
                f"{api_url}/sendMessage",
                data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30)
            return True
        except Exception as e:
            print(f"⚠️ sendMessage gagal ({api_url[:30]}): {e}")
    return False


def send_video(video_path, thumb_path, caption):
    """Kirim video langsung ke Telegram (playable inline) via multipart/form-data.
    Lewat local Bot API Server (TG_API_URL), max upload dinaikkan ke 2 GB."""
    if not video_path or not os.path.isfile(video_path) or os.path.getsize(video_path) == 0:
        print("⚠️ File video tidak valid, lewati sendVideo.")
        return False

    size = os.path.getsize(video_path)
    MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB (local Bot API Server)
    if size > MAX_BYTES:
        print(f"⚠️ Video {size/1024/1024/1024:.2f} GB > 2 GB, tidak dikirim langsung.")
        return False

    boundary = f"----rusemeva{uuid.uuid4().hex}"

    parts = []
    # chat_id
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="chat_id"\r\n\r\n')
    parts.append(f"{chat_id}\r\n".encode())
    # caption
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="caption"\r\n\r\n')
    parts.append(f"{caption}\r\n".encode())
    # parse_mode
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="parse_mode"\r\n\r\n')
    parts.append(b"HTML\r\n")
    # thumb (optional)
    if thumb_path and os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0:
        with open(thumb_path, "rb") as tf:
            tbytes = tf.read()
        tname = os.path.basename(thumb_path)
        tctype = mimetypes.guess_type(tname)[0] or "image/jpeg"
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="thumb"; filename="{tname}"\r\n'.encode())
        parts.append(f"Content-Type: {tctype}\r\n\r\n".encode())
        parts.append(tbytes)
        parts.append(b"\r\n")
    # video (file)
    with open(video_path, "rb") as f:
        vbytes = f.read()
    vname = os.path.basename(video_path)
    vctype = mimetypes.guess_type(vname)[0] or "video/mp4"
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="video"; filename="{vname}"\r\n'.encode())
    parts.append(f"Content-Type: {vctype}\r\n\r\n".encode())
    parts.append(vbytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    # Try primary API, then fallback
    for api_url in [API, FALLBACK_API]:
        try:
            req = urllib.request.Request(
                f"{api_url}/sendVideo",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            urllib.request.urlopen(req, timeout=600)
            return True
        except Exception as e:
            print(f"⚠️ sendVideo gagal ({api_url[:30]}): {e}")
    return False


def send_photo_fallback(photo_path, caption):
    """Kirim foto (thumbnail) dengan caption via multipart/form-data (fallback)."""
    if not photo_path or not os.path.isfile(photo_path) or os.path.getsize(photo_path) == 0:
        print("⚠️ Thumbnail tidak valid, lewati sendPhoto.")
        return False
    try:
        with open(photo_path, "rb") as f:
            photo_bytes = f.read()
    except Exception as e:
        print(f"⚠️ Gagal baca thumbnail: {e}")
        return False

    boundary = f"----rusemeva{uuid.uuid4().hex}"
    fname = os.path.basename(photo_path)
    ctype = mimetypes.guess_type(fname)[0] or "image/jpeg"

    parts = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="chat_id"\r\n\r\n')
    parts.append(f"{chat_id}\r\n".encode())
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="caption"\r\n\r\n')
    parts.append(f"{caption}\r\n".encode())
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="parse_mode"\r\n\r\n')
    parts.append(b"HTML\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="photo"; filename="{fname}"\r\n'.encode())
    parts.append(f"Content-Type: {ctype}\r\n\r\n".encode())
    parts.append(photo_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        f"{API}/sendPhoto",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        resp = urllib.request.urlopen(req, timeout=600)
        try:
            js = json.loads(resp.read().decode())
            if js.get("ok") and js.get("result", {}).get("video", {}).get("file_id"):
                return js["result"]["video"]["file_id"]
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"⚠️ sendPhoto gagal: {e}")
        # Fallback ke public API
        try:
            req2 = urllib.request.Request(
                f"{FALLBACK_API}/sendPhoto",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            urllib.request.urlopen(req2, timeout=600)
            return True
        except Exception as e2:
            print(f"⚠️ sendPhoto fallback juga gagal: {e2}")
            return False


def send_video_with_fallback(video_path, caption, thumb_path):
    """Kirim video (max 2GB) via local Bot API Server; fallback foto+link lalu teks.
    Return file_id (str) kalau sukses lewat sendVideo, True kalau sukses fallback, False kalau gagal."""
    fid = send_video(video_path, thumb_path, caption)
    if isinstance(fid, str):
        return fid
    if fid is True:
        return True
    fallback_caption = caption + (f"\n\n📂 Release: {release_url}\n🔗 Run: {run_url}" if release_url else "")
    if thumb_path and os.path.isfile(thumb_path) and os.path.getsize(thumb_path) > 0:
        if send_photo_fallback(thumb_path, fallback_caption):
            return True
    send_message(fallback_caption)
    return False


if job_status == "success":
    thumb_path = thumb_file if (has_thumb and os.path.isfile(thumb_file)) else ""

    # --- Phase original: kirim video ORIGINAL (asli, tidak diubah) ---
    # Kalau original > 2GB (limit Bot API 2GB), dilewati — HEVC di-encode terpisah & dikirim JIKA sukses.
    if phase in ("original", "both"):
        req_h = os.environ.get("HUMAN_DUR") or os.environ.get("REQUESTED_HUMAN") or human_dur
        stop_r = os.environ.get("STOP_REASON") or ""
        stop_line = f"\n📌 Stop: <code>{stop_r}</code>" if stop_r else ""
        caption_orig = (
            f"✅ <b>Rekaman selesai!</b>\n\n"
            f"{id_line()}"
            f"📦 File: <code>{filename}</code>\n"
            f"📏 Size: {file_size}\n"
            f"⏱ Diminta: {req_h}\n"
            f"⏱ Didapat: {real_duration}{stop_line}\n"
            f"🖥 Resolusi: {resolution}\n"
            f"🎞 Codec: {vcodec}\n"
            f"📶 Bitrate: {vbitrate}"
        )
        if orig_bytes > 2 * 1024 * 1024 * 1024:
            print(f"⚠️ ORIGINAL {orig_bytes/1024/1024/1024:.2f} GB > 2GB — dilewati (limit Bot API).", flush=True)
            send_message(
                f"⚠️ <b>Original {orig_bytes/1024/1024/1024:.2f} GB &gt; 2GB limit</b> — tidak dikirim ke Telegram (batas Bot API 2GB).\n"
                f"📦 File: <code>{filename}</code>\n"
                f"🎞 Video ini di-encode ke HEVC 10-bit di workflow terpisah (<code>rusemeva-encode</code>).\n"
                f"📤 Hasil HEVC akan dikirim <b>JIKA encode berhasil</b>; bila gagal akan ada notifikasi error."
            )
        elif os.path.isfile(filename) and os.path.getsize(filename) > 0:
            print("📤 Mengirim video ORIGINAL...", flush=True)
            res = send_video_with_fallback(filename, caption_orig, thumb_path)
            # Capture file_id (kalau sukses lewat sendVideo) untuk trigger encode terpisah
            if isinstance(res, str):
                with open("/tmp/rusemeva_orig_file_id.txt", "w") as f:
                    f.write(res)
                print(f"✅ ORIGINAL terkirim, file_id={res[:20]}... (disimpan untuk encode)", flush=True)
            elif res is True:
                print("✅ ORIGINAL terkirim (via fallback, tanpa file_id).", flush=True)

            # Info: HEVC di-encode di workflow TERPISAH (rusemeva-encode, full 6 jam)
            enc_secs, rt = estimate_encode(human_dur)
            warn = ""
            if enc_secs > 6 * 3600:
                warn = "\n⚠️ <b>Estimasi &gt; 6 jam</b> — encode bisa ke-potong limit GitHub (auto-downgrade ke preset lebih cepat otomatis dilakukan di encode.yml)."
            send_message(
                f"⏳ <b>HEVC 10-bit sedang di-encode terpisah</b> (workflow <code>rusemeva-encode</code>, hingga 6 jam).\n"
                f"🎚 Preset: <code>{hevc_preset}</code> (CRF {hevc_crf})\n"
                f"🎞 Estimasi encode HEVC: <b>{fmt_dur(enc_secs)}</b> (~{rt}x realtime, job terpisah — bukan nunggu rekam realtime)\n"
                f"🕐 Prediksi selesai ~<b>{eta_clock(enc_secs // 60)}</b>\n"
                f"📤 Hasil akan dikirim <b>JIKA berhasil</b>; bila gagal akan ada laporan error.{warn}"
            )

    # --- Phase hevc: kirim video HEVC 10-bit (metadata asli hasil encode) ---
    if phase in ("hevc", "both"):
        if hevc_file and os.path.isfile(hevc_file) and os.path.getsize(hevc_file) > 0:
            print("📤 Mengirim video HEVC 10-bit...", flush=True)
            hevc_thumb_path = hevc_thumb_file if (has_hevc_thumb and os.path.isfile(hevc_thumb_file)) else ""
            caption_hevc = (
                f"🎞 <b>HEVC 10-bit</b>\n\n"
                f"{id_line()}"
                f"📦 File: <code>{hevc_file}</code>\n"
                f"📏 Size: {hevc_size}\n"
                f"⏱ Durasi: {hevc_dur}\n"
                f"🖥 Resolusi: {hevc_res}\n"
                f"🎞 Codec: {hevc_codec}\n"
                f"📶 Bitrate: {hevc_br}"
                f"{chapters_block()}"
            )
            send_video_with_fallback(hevc_file, caption_hevc, hevc_thumb_path)
        elif os.environ.get("HEVC_SPLIT", "0") == "1" and hevc_file:
            # #1 SPLIT: kirim tiap part (file asli sudah di-split, hapus di workflow)
            base = hevc_file
            parts = sorted([f for f in os.listdir(os.path.dirname(base) or ".") if f.startswith(os.path.basename(base) + ".part")])
            total = len(parts)
            print(f"📦 Mengirim {total} part HEVC (split)...", flush=True)
            for i, pf in enumerate(parts, 1):
                ppath = os.path.join(os.path.dirname(base) or ".", pf)
                psize = os.path.getsize(ppath)
                pcap = (
                    f"🎞 <b>HEVC 10-bit — Part {i}/{total}</b>\n\n"
                    f"{id_line()}"
                    f"📦 File: <code>{pf}</code>\n"
                    f"📏 Size: {psize/1024/1024/1024:.2f} GB\n"
                    f"⏱ Durasi: {hevc_dur}\n"
                    f"🖥 Resolusi: {hevc_res}\n"
                    f"ℹ️ Gabungkan semua part dengan: cat {os.path.basename(base)}.part* > {os.path.basename(base)}"
                )
                send_video_with_fallback(ppath, pcap, "")
            # Bersihkan part
            for pf in parts:
                try: os.remove(os.path.join(os.path.dirname(base) or ".", pf))
                except Exception: pass
        else:
            # Encode gagal / file tidak ada -> jujur lapor, jangan diam
            print("⚠️ HEVC file tidak ada -> encode gagal, lapor ke user.", flush=True)
            # === #5 ERROR CLASSIFICATION: pesan jujur + saran berdasarkan FAIL_REASON ===
            reason = os.environ.get("FAIL_REASON", "unknown") or "unknown"
            reason_map = {
                "disk_full": (
                    "💽 <b>Penyebab: Disk runner GitHub penuh (No space left on device).</b>\n"
                    "ℹ️ GitHub runner cuma punya ~14GB; video besar + original numpuk.\n"
                    "💡 Saran: coba lagi nanti (runner di-reset), atau rekam durasi lebih pendek."
                ),
                "codec_or_corrupt": (
                    "🎞 <b>Penyebab: Codec/stream bermasalah (file sumber korup atau encoder libx265 hilang).</b>\n"
                    "ℹ️ Biasanya m3u8 terputus saat rekam, menghasilkan file tidak valid.\n"
                    "💡 Saran: cek koneksi stream, atau coba preset berbeda. Sistem auto-retry 1 level lebih cepat."
                ),
                "timeout_or_killed": (
                    "⏱ <b>Penyebab: Encode ke-potong (timeout 6 jam / SIGKILL).</b>\n"
                    "ℹ️ Video terlalu panjang untuk preset ini di runner 2-CPU.\n"
                    "💡 Saran: sistem otomatis turun preset (auto-downgrade) atau auto-retry lebih cepat. Coba lagi."
                ),
                "ffmpeg_error": (
                    "⚙️ <b>Penyebab: ffmpeg error (conversion failed / frame error).</b>\n"
                    "ℹ️ Lihat log encode untuk detail.\n"
                    "💡 Saran: ulangi rekam, atau laporkan ke admin kalau berulang."
                ),
                "unknown": (
                    "❓ <b>Penyebab: tidak terklasifikasi.</b>\n"
                    "ℹ️ Cek log encode untuk detail lengkap.\n"
                    "💡 Sistem otomatis mencoba ulang 1 level lebih cepat (lihat notifikasi berikutnya)."
                ),
            }
            reason_text = reason_map.get(reason, reason_map["unknown"])
            send_message(
                f"❌ <b>Encode HEVC gagal.</b>\n\n"
                f"{id_line()}"
                f"📦 Source: <code>{filename}</code>\n"
                f"{reason_text}\n"
                f"🔗 Log: {run_url}"
            )
elif job_status == "cancelled":
    # Pesan jujur tergantung phase:
    # - phase=hevc (encode) -> yang dibatalkan ENCODE-nya, rekaman SUDAH SELESAI (masih utuh di release).
    # - phase record/other -> rekaman dibatalkan.
    if phase == "hevc":
        msg = (
            f"⏹ <b>Encode dibatalkan.</b>\n\n"
            f"{id_line()}"
            f"📦 Source: <code>{filename}</code>\n"
            f"ℹ️ Rekaman ASLI sudah selesai direkam & dikirim ke Telegram (cek file di chat Anda).\n"
            f"ℹ️ Di GitHub TIDAK ada file video — release vault hanya berisi manifest .txt, dan source temp di release sudah dibersihkan otomatis.\n"
        )
        msg += f"🔗 Run encode: {run_url}"
    else:
        msg = (
            f"🚫 <b>Rekaman dibatalkan.</b>\n\n"
            f"{id_line()}"
            f"📦 File: <code>{filename}</code>\n"
            f"🔗 Run: {run_url}"
        )
    send_message(msg)
else:
    # --- Ambil tail log GH run biar owner tahu KENAPA gagal ---
    run_log = ""
    try:
        import subprocess
        out = subprocess.run(
            ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/logs",
             "--header", "Accept: application/vnd.github+json",
             "-o", "/tmp/rusemeva_run.log.zip"],
            capture_output=True, text=True, timeout=60)
        if out.returncode == 0:
            import zipfile
            with zipfile.ZipFile("/tmp/rusemeva_run.log.zip") as z:
                # gabungkan semua log step, ambil 1500 char terakhir
                full = ""
                for n in z.namelist():
                    full += z.read(n).decode("utf-8", "replace") + "\n"
                run_log = full[-1500:]
    except Exception as e:
        run_log = f"(gagal ambil log: {e})"
    # fallback tail log dari artifact jika zip gagal
    if not run_log:
        try:
            import subprocess
            out = subprocess.run(
                ["gh", "run", "view", run_id, "--log", "--repo", repo],
                capture_output=True, text=True, timeout=60)
            run_log = (out.stdout or out.stderr)[-1500:]
        except Exception:
            run_log = ""
    log_block = f"\n\n📜 <b>Log tail:</b>\n<pre>{run_log.strip()[-1200:]}</pre>" if run_log.strip() else ""
    send_message(
        f"❌ <b>Rekaman gagal.</b>\n\n"
        f"{id_line()}"
        f"📦 File: <code>{filename}</code>\n"
        f"🔗 Cek log: {run_url}{log_block}"
    )
