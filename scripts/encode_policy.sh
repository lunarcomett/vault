#!/usr/bin/env bash
# Encode size/bitrate policy — single source of truth for rusemeva-encode.
# Source this file:  source scripts/encode_policy.sh
# Constants target ~1.3 Mbps sweet spot @720p (1.0 too blurry; 2.0 too fat).

: "${MAX_TOTAL_BPS:=1450000}"
: "${TARGET_TOTAL_BPS:=1350000}"
: "${MIN_TOTAL_BPS:=1200000}"
: "${MAX_CRF:=28}"
: "${MIN_CRF:=22}"
: "${MAXRATE_K:=1450}"
: "${BUFSIZE_K:=2900}"
: "${ACCEPT_BPS:=1500000}"

# target_bytes DURATION_SEC ORIG_BYTES
# -> prints integer bytes = min(90% orig, MAX_TOTAL_BPS * dur / 8)
target_bytes() {
  local dur="${1:-0}" orig="${2:-0}"
  case "$dur" in ''|*[!0-9]*) dur=0 ;; esac
  case "$orig" in ''|*[!0-9]*) orig=0 ;; esac
  if [ "$dur" -le 0 ]; then
    echo "0"
    return 1
  fi
  local from_bps from_orig
  from_bps=$(awk -v b="$MAX_TOTAL_BPS" -v d="$dur" 'BEGIN{printf "%d", b*d/8}')
  from_orig=$(awk -v o="$orig" 'BEGIN{printf "%d", o*0.90}')
  if [ "$from_orig" -le 0 ] || [ "$from_bps" -lt "$from_orig" ]; then
    echo "$from_bps"
  else
    echo "$from_orig"
  fi
}

# require_positive_int VARNAME VALUE
require_positive_int() {
  local name="$1" val="$2"
  case "$val" in ''|*[!0-9]*) return 1 ;; esac
  [ "$val" -gt 0 ] 2>/dev/null
}

# probe_bitrate FILE -> stdout bps or empty
probe_bitrate() {
  local f="$1" b
  b=$(ffprobe -v error -show_entries format=bit_rate -of default=noprint_wrappers=1:nokey=1 "$f" 2>/dev/null | head -1)
  case "$b" in ''|*[!0-9]*) echo ""; return 1 ;; esac
  echo "$b"
}

# probe_duration_int FILE
probe_duration_int() {
  local f="$1" d
  d=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$f" 2>/dev/null | head -1)
  d=${d%.*}
  case "$d" in ''|*[!0-9]*) echo "0"; return 1 ;; esac
  echo "$d"
}

# audio_bps FILE (0 if unknown)
probe_audio_bps() {
  local f="$1" b
  b=$(ffprobe -v error -select_streams a:0 -show_entries stream=bit_rate -of default=noprint_wrappers=1:nokey=1 "$f" 2>/dev/null | head -1)
  case "$b" in ''|*[!0-9]*|N/A) echo "0"; return 0 ;; esac
  echo "$b"
}

# video_maxrate_k from audio: leave room; floor 1000k
video_maxrate_k() {
  local audio_bps="${1:-0}"
  case "$audio_bps" in ''|*[!0-9]*) audio_bps=0 ;; esac
  local room=$(( MAX_TOTAL_BPS - audio_bps ))
  [ "$room" -lt 1000000 ] && room=1000000
  awk -v r="$room" 'BEGIN{printf "%d", r/1000}'
}

# accept_hevc HEVC_BYTES TARGET_BYTES BITRATE_BPS
# prints: OK | NEED_SMALLER | NEED_BETTER | UNKNOWN
accept_hevc() {
  local bytes="${1:-0}" target="${2:-0}" bps="${3:-}"
  case "$bytes" in ''|*[!0-9]*) bytes=0 ;; esac
  case "$target" in ''|*[!0-9]*) target=0 ;; esac
  if [ -z "$bps" ]; then
    echo "UNKNOWN"
    return 2
  fi
  case "$bps" in *[!0-9]*) echo "UNKNOWN"; return 2 ;; esac
  if [ "$bps" -lt "$MIN_TOTAL_BPS" ]; then
    echo "NEED_BETTER"
    return 1
  fi
  if [ "$bps" -gt "$ACCEPT_BPS" ]; then
    echo "NEED_SMALLER"
    return 1
  fi
  if [ "$target" -gt 0 ] && [ "$bytes" -gt "$target" ]; then
    # size over but bitrate OK: soft OK if bps within cap (container overhead)
    if [ "$bps" -le "$MAX_TOTAL_BPS" ]; then
      echo "OK"
      return 0
    fi
    echo "NEED_SMALLER"
    return 1
  fi
  echo "OK"
  return 0
}
