#!/usr/bin/env python3
"""Kirim pesan teks ke Telegram dari workflow.
Usage: python3 scripts/send_message.py "teks pesan"
Env: BOT_TOKEN, CHAT_ID, TG_API_URL (opsional, default https://api.telegram.org)
"""
import os, sys, json, urllib.request

def main():
    token = os.environ.get("BOT_TOKEN", "")
    chat = os.environ.get("CHAT_ID", "")
    api = os.environ.get("TG_API_URL", "https://api.telegram.org").rstrip("/")
    if not token or not chat:
        print("⚠️ BOT_TOKEN/CHAT_ID kosong, skip kirim."); return
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    if not text:
        print("⚠️ Pesan kosong."); return
    fallback = "https://api.telegram.org"
    for base in [api, fallback]:
        url = f"{base}/bot{token}/sendMessage"
        data = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                print(f"✅ Pesan terkirim via {base[:30]}:", r.status)
                return
        except Exception as e:
            print(f"⚠️ Gagal via {base[:30]}:", e)

if __name__ == "__main__":
    main()
