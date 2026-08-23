#!/usr/bin/env python3
"""Decide post-record route: send direct vs encode HEVC.

Policy (satu sumber kebenaran, sama dengan rusemeva lama):
- Encode jika: total bitrate > ENCODE_BPS (1.5 Mbps) ATAU size > 2GB.
- Tidak encode jika: bitrate <= ambang DAN size <= 2GB (kirim original utuh).

Usage: python3 scripts/decide_route.py FILE DURATION_SEC
Output (stdout): KEY=VALUE lines untuk GITHUB_ENV:
  ROUTE=direct|encode
  ORIG_BPS=<int>
  REASON=<teks singkat>
Exit code selalu 0 (keputusan, bukan error).
"""
import os
import subprocess
import sys


def probe(f):
    br = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=bit_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", f],
        capture_output=True, text=True, timeout=30)
    dur = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", f],
        capture_output=True, text=True, timeout=30)
    bps = (br.stdout or "").strip()
    d = (dur.stdout or "").strip()
    try:
        bps = int(float(bps))
    except Exception:
        bps = 0
    try:
        d = int(float(d.split(".")[0]))
    except Exception:
        d = 0
    return bps, d


def main():
    f = sys.argv[1]
    dur_req = sys.argv[2] if len(sys.argv) > 2 else "0"
    try:
        dur_req = int(dur_req)
    except Exception:
        dur_req = 0
    if not os.path.isfile(f) or os.path.getsize(f) == 0:
        print("ROUTE=error")
        print("REASON=file hilang/kosong")
        sys.exit(0)

    size = os.path.getsize(f)
    bps, dur_real = probe(f)
    # fallback: kalau format bitrate tidak ada (beberapa mp4), hitung dari size/durasi
    if bps <= 0 and dur_real > 0:
        bps = int(size * 8 / dur_real)

    ENCODE_BPS = int(os.environ.get("ENCODE_BPS", "1500000"))
    MAX_SEND = int(os.environ.get("MAX_SEND_BYTES", str(1950 * 1024 * 1024)))

    reasons = []
    need_encode = False
    if size > MAX_SEND:
        need_encode = True
        reasons.append(f"size {size/1024/1024:.0f}MB > {MAX_SEND/1024/1024:.0f}MB")
    if bps > ENCODE_BPS:
        need_encode = True
        reasons.append(f"bitrate {bps/1000000:.2f}Mbps > {ENCODE_BPS/1000000:.2f}Mbps")

    route = "encode" if need_encode else "direct"
    if not reasons:
        reasons.append(f"bitrate {bps/1000000:.2f}Mbps & size {size/1024/1024:.0f}MB di bawah ambang")

    print(f"ROUTE={route}")
    print(f"ORIG_BPS={bps}")
    print(f"DUR_REAL={dur_real}")
    print(f"SIZE_BYTES={size}")
    print(f"REASON={'; '.join(reasons)}")


if __name__ == "__main__":
    main()
