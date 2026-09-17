#!/usr/bin/env python3
"""Standalone HEVC delivery. No fallback, retries, or source deletion."""


from pathlib import Path
from contextlib import ExitStack
import html
import sys
import http.client
import ipaddress
import json
import os
import re
import uuid
from urllib.parse import urlsplit

CHUNK_SIZE = 1024 * 1024
MAX_RESPONSE = 1024 * 1024


def api_endpoint(url):
    """Validate base URL without DNS trust for plaintext remote hosts."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme not in ("http", "https") or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or any(ord(c) <= 32 for c in url)):
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.scheme == "http":
            if host == "localhost":
                host = "127.0.0.1"  # No DNS resolution can redirect plaintext token.
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError
        return parsed.scheme, host, port, parsed.path.rstrip("/")
    except ValueError:
        raise DeliveryError("Invalid API endpoint; remote hosts require HTTPS") from None


def upload_file(api_url, token, chat_id, path, media, caption, thumbnail=None):
    """Stream one multipart POST, bounded RAM; return JSON, never retry/redirect.

    Caller must validate_response before counting delivery. Timeout may mean
    server already accepted upload: inspect Telegram before manual rerun.
    """
    scheme, host, port, prefix = api_endpoint(api_url)
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or media not in ("video", "document"):
        raise DeliveryError("Invalid upload configuration")
    boundary = "matrix-" + uuid.uuid4().hex
    connection = None
    try:
        with ExitStack() as stack:
            fields = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
            if media == "video":
                fields["supports_streaming"] = "true"
            else:
                fields["disable_content_type_detection"] = "true"
            segments = [
                (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                 f'{value}\r\n').encode("utf-8") for key, value in fields.items()
            ]
            files = [(media, Path(path), "video/mp4" if media == "video" else "application/octet-stream")]
            if thumbnail and media == "video":
                files.append(("thumbnail", Path(thumbnail), "image/jpeg"))
            for field, file_path, content_type in files:
                handle = stack.enter_context(file_path.open("rb"))
                size = os.fstat(handle.fileno()).st_size
                if size <= 0:
                    raise DeliveryError("Empty upload file")
                # Quoted multipart parameters must not permit header injection.
                name = file_path.name.replace("%", "%25").replace('"', '%22').replace("\r", "%0D").replace("\n", "%0A")
                segments.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                                 f'filename="{name}"\r\nContent-Type: {content_type}\r\n\r\n').encode("utf-8"))
                segments.extend([(handle, size), b"\r\n"])
            segments.append(f"--{boundary}--\r\n".encode())
            length = sum(len(s) if isinstance(s, bytes) else s[1] for s in segments)
            connection_type = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            connection = connection_type(host, port, timeout=600)
            method = "sendVideo" if media == "video" else "sendDocument"
            connection.putrequest("POST", f"{prefix}/bot{token}/{method}")
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(length))
            connection.endheaders()
            for segment in segments:
                if isinstance(segment, bytes):
                    connection.send(segment)
                else:
                    handle, remaining = segment
                    while remaining:
                        chunk = handle.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            raise DeliveryError("Upload file changed while reading")
                        connection.send(chunk)
                        remaining -= len(chunk)
                    if handle.read(1):
                        raise DeliveryError("Upload file changed while reading")
            response = connection.getresponse()
            if response.status != 200:
                raise DeliveryError("Telegram HTTP failure")
            data = response.read(MAX_RESPONSE + 1)
            if len(data) > MAX_RESPONSE:
                raise DeliveryError("Telegram response too large")
            return json.loads(data)
    except DeliveryError:
        raise
    except Exception:
        # Never expose URL, token, server body, or exception text.
        raise DeliveryError("Upload or response failed; delivery unconfirmed") from None
    finally:
        if connection is not None:
            connection.close()


def select_files(filename, split="0"):
    """Split mode always wins over base presence. Never modify input files."""
    if not filename or split not in ("0", "1"):
        raise DeliveryError("Invalid HEVC selection")
    base = Path(filename)
    try:
        if split == "1":
            files = sorted(p for p in base.parent.iterdir()
                           if p.name.startswith(base.name + ".part-") and p.is_file())
            media = "document"
        else:
            files, media = [base], "video"
            if base.suffix.lower() != ".mp4":
                raise DeliveryError("Inline delivery requires MP4")
        if not files or any(not p.is_file() or p.stat().st_size == 0 for p in files):
            raise DeliveryError("HEVC files missing or empty")
    except OSError:
        raise DeliveryError("Cannot read HEVC files") from None
    return media, files


class DeliveryError(Exception):
    """Delivery cannot be confirmed; retain sources."""


def validate_response(response, media):
    """Return receipt only for a confirmed Telegram video/document message."""
    if media not in ("video", "document"):
        raise DeliveryError("Unsupported media type")
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise DeliveryError("Telegram did not confirm upload")
    result = response.get("result")
    if not isinstance(result, dict):
        raise DeliveryError("Missing Telegram message")
    message_id = result.get("message_id")
    if type(message_id) is not int or message_id <= 0:
        raise DeliveryError("Missing Telegram message ID")
    attachment = result.get(media)
    file_id = attachment.get("file_id") if isinstance(attachment, dict) else None
    if not isinstance(file_id, str) or not file_id.strip():
        raise DeliveryError("Missing Telegram media file ID")
    return {"message_id": message_id, "file_id": file_id}


def build_caption(env, path, media, index, total):
    """Escape each value before HTML interpolation; keep caption below limit."""
    def escaped(value, limit=60):
        # Telegram entity lengths use UTF-16 units; don't split surrogate pairs.
        text = str(value or "?").encode("utf-16-le")[:limit * 2].decode("utf-16-le", errors="ignore")
        return html.escape(text)

    lines = ["<b>HEVC</b>", f"File: <code>{escaped(path.name, 200)}</code>"]
    for label, key in (("Size", "HEVC_SIZE"), ("Durasi", "HEVC_DUR"),
                       ("Resolusi", "HEVC_RES"), ("Codec", "HEVC_VCODEC"),
                       ("Bitrate", "HEVC_VBITRATE")):
        lines.append(f"{label}: {escaped(env.get(key))}")
    if media == "document":
        lines.append(f"Part {index}/{total}. Bagian biner; gabungkan semua part sebelum diputar.")
    return "\n".join(lines)


def main(env=None, transport=None):
    """Consume notify.py's HEVC environment, fail closed, never delete sources.

    Required: BOT_TOKEN, CHAT_ID, HEVC_FILE. HEVC_SPLIT defaults to 0.
    TG_API_URL defaults to loopback local Bot API. GITHUB_OUTPUT is optional
    outside Actions; delivered=true is appended only after every media receipt.
    """
    env = os.environ if env is None else env
    transport = upload_file if transport is None else transport
    confirmed = 0
    try:
        output = env.get("GITHUB_OUTPUT")
        if output:
            with open(output, "a", encoding="utf-8") as handle:
                handle.write("delivered=false\n")
        if env.get("JOB_STATUS", "success") != "success":
            raise DeliveryError("Upstream job not successful")
        token, chat_id = env.get("BOT_TOKEN", ""), env.get("CHAT_ID", "")
        if not token.strip() or not chat_id.strip():
            raise DeliveryError("Missing delivery credentials")
        api_url = env.get("TG_API_URL") or "http://localhost:8081"
        api_endpoint(api_url)
        media, files = select_files(env.get("HEVC_FILE", ""), env.get("HEVC_SPLIT", "0"))
        thumbnail = None
        if media == "video" and env.get("HAS_HEVC_THUMB") == "1" and env.get("HEVC_THUMB_FILE"):
            candidate = Path(env["HEVC_THUMB_FILE"])
            if candidate.is_file() and candidate.stat().st_size > 0:
                thumbnail = candidate
        for index, path in enumerate(files, 1):
            caption = build_caption(env, path, media, index, len(files))
            response = transport(api_url, token, chat_id, path, media, caption, thumbnail)
            receipt = validate_response(response, media)
            confirmed += 1
            print(f"Confirmed media {index}/{len(files)}; message_id={receipt['message_id']}", flush=True)
        if output:
            with open(output, "a", encoding="utf-8") as handle:
                handle.write("delivered=true\n")
        return 0
    except Exception:
        # Static message only: even transport exceptions may contain bot token.
        print(f"Delivery unconfirmed; {confirmed} media receipts confirmed. Sources retained. "
              "No retry performed; inspect Telegram before rerun.", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
