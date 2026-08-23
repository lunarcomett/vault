#!/usr/bin/env bash
set -euo pipefail
# #7 mulai timer kalibrasi
ENC_START=$SECONDS
FILE="${ORIG_FILE}"
HEVC_FILE="${FILE%.mp4}-h265-10bit.mp4"
FFMPEG_STATIC="${FFMPEG_STATIC}"
HEVC_PRESET="${HEVC_PRESET}"
HEVC_CRF="${HEVC_CRF}"
CHOSEN="${CHOSEN_PRESET}"
if [ "$CHOSEN" != "$HEVC_PRESET" ]; then
  MSG="🔻 <b>Auto-downgrade!</b> Preset <code>$CHOSEN</code> estimasi &gt;5j50m, otomatis turun ke <code>$HEVC_PRESET</code> (CRF $HEVC_CRF) biar muat 6 jam."
else
  MSG="🎚 Encode pakai preset <code>$HEVC_PRESET</code> (CRF $HEVC_CRF)."
fi
CHAT_ID="$CHAT_ID" FILENAME="$FILE" TG_API_URL="$TG_API_URL" BOT_TOKEN="$BOT_TOKEN" python3 scripts/send_message.py "$MSG" || true
echo "🎞 Encoding HEVC 10-bit (preset=${HEVC_PRESET}, CRF ${HEVC_CRF}) dari original..."
echo "📦 Original: $(ls -lh "$FILE" | awk '{print $5}')"

source scripts/encode_policy.sh
REQ_DUR="${REQUESTED_DURATION}"
SRC_DUR_INT="${DURATION_SEC}"
case "$SRC_DUR_INT" in ''|*[!0-9]*) SRC_DUR_INT=0 ;; esac
REAL_SRC_INT=$(probe_duration_int "$FILE" || true)
if [ "${REAL_SRC_INT:-0}" -gt 0 ] 2>/dev/null; then
  SRC_DUR_INT=$REAL_SRC_INT
elif [ "${SRC_DUR_INT:-0}" -le 0 ] 2>/dev/null; then
  case "$REQ_DUR" in ''|*[!0-9]*) SRC_DUR_INT=60 ;; *) SRC_DUR_INT=$REQ_DUR ;; esac
  [ "$SRC_DUR_INT" -le 0 ] 2>/dev/null && SRC_DUR_INT=60
fi
AUDIO_BPS=$(probe_audio_bps "$FILE" || echo 0)
MAXRATE_K=$(video_maxrate_k "$AUDIO_BPS")
BUFSIZE_K=$(( MAXRATE_K * 2 ))
echo "dur=${SRC_DUR_INT}s audio=${AUDIO_BPS} maxrate=${MAXRATE_K}k"

# === SCENE-AWARE CRF ===
# Probe 5 cuplikan @8s: encode mini ultrafast di CRF basis, ukur bytes/s.
# Konten ramai/gelap (bytes/s tinggi) → CRF turun (lebih bagus).
# Konten sepi/talking-head (bytes/s rendah) → CRF naik (lebih hemat).
# Clamp final 22..28 biar tetap di sweet-spot ~1.2–1.4 Mbps @720p.
BASE_CRF=$HEVC_CRF
SCENE_DELTA=0
if [ "$SRC_DUR_INT" -ge 60 ] 2>/dev/null; then
  echo "🧠 Scene-aware probe (5x8s)..."
  SUM_BPS=0
  N_OK=0
  for frac in 10 30 50 70 90; do
    SS=$(( SRC_DUR_INT * frac / 100 ))
    # jangan mepet EOF
    MAX_SS=$(( SRC_DUR_INT - 10 ))
    [ "$SS" -gt "$MAX_SS" ] && SS=$MAX_SS
    [ "$SS" -lt 0 ] && SS=0
    SAMPLE="/tmp/rusemeva_scene_${frac}.mp4"
    "$FFMPEG_STATIC" -hide_banner -y -ss "$SS" -t 8 -i "$FILE" \
      -c:v libx265 -profile:v main10 -pix_fmt yuv420p10le \
      -crf ${BASE_CRF} -preset ultrafast -an "$SAMPLE" >/tmp/rusemeva_scene_probe.log 2>&1 || true
    if [ -s "$SAMPLE" ]; then
      BY=$(stat -c%s "$SAMPLE" 2>/dev/null || wc -c < "$SAMPLE")
      # bytes per second of sample
      BPS=$(( BY / 8 ))
      SUM_BPS=$(( SUM_BPS + BPS ))
      N_OK=$(( N_OK + 1 ))
      echo "   • t=${SS}s sample=${BY}B (~${BPS} B/s)"
    fi
    rm -f "$SAMPLE" 2>/dev/null || true
  done
  if [ "$N_OK" -gt 0 ]; then
    AVG_BPS=$(( SUM_BPS / N_OK ))
    echo "🧠 Scene avg complexity: ${AVG_BPS} B/s (n=$N_OK) base_crf=$BASE_CRF"
    # Kalibrasi kasar 720p@CRF24 ultrafast sample:
    # sepi < 40k B/s, normal 40–90k, ramai > 90k
    if [ "$AVG_BPS" -gt 90000 ]; then
      SCENE_DELTA=-2
      SCENE_LABEL="ramai/kompleks"
    elif [ "$AVG_BPS" -gt 60000 ]; then
      SCENE_DELTA=-1
      SCENE_LABEL="agak ramai"
    elif [ "$AVG_BPS" -lt 35000 ]; then
      SCENE_DELTA=1
      SCENE_LABEL="sepi/talking-head"
    else
      SCENE_DELTA=0
      SCENE_LABEL="normal"
    fi
    HEVC_CRF=$(( BASE_CRF + SCENE_DELTA ))
    [ "$HEVC_CRF" -lt 22 ] && HEVC_CRF=22
    [ "$HEVC_CRF" -gt 28 ] && HEVC_CRF=28
    echo "🧠 Scene-aware: ${SCENE_LABEL} → CRF ${BASE_CRF} + (${SCENE_DELTA}) = ${HEVC_CRF}"
    if [ "$HEVC_CRF" != "$BASE_CRF" ]; then
      CHAT_ID="$CHAT_ID" TG_API_URL="$TG_API_URL" BOT_TOKEN="$BOT_TOKEN" python3 scripts/send_message.py \
        "🧠 <b>Scene-aware:</b> konten <code>${SCENE_LABEL}</code> → CRF <code>${BASE_CRF}</code> → <code>${HEVC_CRF}</code> (jaga ~1.3 Mbps, anti-buram di bagian penting)." || true
    fi
  else
    echo "⚠️ Scene probe gagal — pakai CRF basis $BASE_CRF"
  fi
else
  echo "ℹ️ Video pendek (<60s) — skip scene-aware"
fi

# x265 adaptive quant: alokasi bit lebih pintar per-block (dalam-frame scene-aware)

# === CHAPTER MARKERS (scene detection) ===
# Deteksi scene change → cluster jadi chapter list.
# Decode-only (no encode), ~2-5 min untuk video 2 jam.
CHAPTERS_FILE="/tmp/rusemeva_chapters.json"
CHAPTERS_TEXT=""
if [ "${SRC_DUR_INT:-0}" -ge 120 ] 2>/dev/null; then
  echo "📌 Chapter detection (scene changes)..."
  SCENE_TIMES_FILE="/tmp/rusemeva_scene_times.txt"
  # ffmpeg select='gt(scene,0.3)' = scene change >30% difference
  # showinfo prints pts_time to stderr. Decode-only, no encode.
  timeout 300 "$FFMPEG_STATIC" -hide_banner -loglevel info \
    -i "$FILE" \
    -vf "select='gt(scene,0.3)',showinfo" \
    -f null - 2>&1 \
    | grep -oP 'pts_time:\K[0-9.]+' > "$SCENE_TIMES_FILE" 2>/dev/null || true

  SCENE_COUNT=$(wc -l < "$SCENE_TIMES_FILE" 2>/dev/null || echo 0)
  echo "📌 Detected $SCENE_COUNT raw scene changes"

  if [ "$SCENE_COUNT" -gt 0 ] 2>/dev/null; then
    # Cluster: keep scenes ≥30s apart, limit to 15 most significant
    # Auto-label based on position in video
    python3 - "$SCENE_TIMES_FILE" "$SRC_DUR_INT" "$CHAPTERS_FILE" <<'PYEOF'
import json, sys

times_file = sys.argv[1]
dur = int(sys.argv[2]) if len(sys.argv) > 2 else 300
out_file = sys.argv[3] if len(sys.argv) > 3 else "/tmp/rusemeva_chapters.json"

raw = []
with open(times_file) as f:
    for line in f:
        line = line.strip()
        if line:
            try:
                raw.append(float(line))
            except ValueError:
                pass

if not raw:
    sys.exit(0)

# Always include 0:00
raw = sorted(set([0.0] + raw))

# Cluster: keep timestamps ≥30s apart
MIN_GAP = 30
clustered = [raw[0]]
for t in raw[1:]:
    if t - clustered[-1] >= MIN_GAP:
        clustered.append(t)

# Cap at 15 chapters
if len(clustered) > 15:
    # Keep evenly spaced + first/last
    step = len(clustered) / 15
    picked = [clustered[0]]
    for i in range(1, 14):
        picked.append(clustered[int(i * step)])
    picked.append(clustered[-1])
    clustered = sorted(set(picked))

# Auto-label based on position
def label(t, dur, idx, total):
    pct = (t / dur * 100) if dur > 0 else 0
    if idx == 0:
        return "Opening"
    if idx == total - 1:
        return "Closing"
    if pct < 10:
        return "Intro"
    if pct > 90:
        return "Penutup"
    if 40 <= pct <= 60:
        return "Pembahasan utama"
    return f"Segmen {idx}"

chapters = []
for i, t in enumerate(clustered):
    m, s = divmod(int(t), 60)
    h, m = divmod(m, 60)
    ts = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    chapters.append({"time": ts, "seconds": int(t), "label": label(t, dur, i, len(clustered))})

with open(out_file, "w") as f:
    json.dump(chapters, f)

print(f"📌 {len(chapters)} chapters generated")
PYEOF

    if [ -s "$CHAPTERS_FILE" ]; then
      CHAPTERS_COUNT=$(python3 -c "import json; print(len(json.load(open('$CHAPTERS_FILE'))))" 2>/dev/null || echo 0)
      echo "📌 $CHAPTERS_COUNT chapters:"
      python3 -c "
import json
ch = json.load(open('$CHAPTERS_FILE'))
for c in ch:
    print(f\"  {c['time']} — {c['label']}\")
" 2>/dev/null || true
      # Export formatted text for notify.py
      CHAPTERS_TEXT=$(python3 -c "
import json
ch = json.load(open('$CHAPTERS_FILE'))
lines = []
for c in ch:
    lines.append(f\"{c['time']} — {c['label']}\")
print(chr(10).join(lines))
" 2>/dev/null || echo "")
    fi
  else
    echo "📌 No scene changes detected — skip chapters"
  fi
  rm -f "$SCENE_TIMES_FILE" 2>/dev/null || true
else
  echo "ℹ️ Video pendek (<120s) — skip chapter detection"
fi
echo "CHAPTERS_FILE=$CHAPTERS_FILE" >> $GITHUB_ENV
echo "CHAPTERS_TEXT<<RUSEMEVA_EOF" >> $GITHUB_ENV
echo "$CHAPTERS_TEXT" >> $GITHUB_ENV
echo "RUSEMEVA_EOF" >> $GITHUB_ENV

# === LIVE-FRIENDLY MODE (siaran TV) ===
# Aktif jika: profil /setting live  ATAU auto-detect chrome statis (logo/ticker).
# Efek: AQ lebih agresif di detail, denoise ringan, jaga tengah, hemat area statis.
ENCODE_PROFILE="${ENCODE_PROFILE}"
LIVE_MODE=0
LIVE_REASON=""
if [ "$ENCODE_PROFILE" = "live" ]; then
  LIVE_MODE=1
  LIVE_REASON="profil /setting live"
else
  # Auto-detect: bandingkan stabilitas sudut vs tengah di 5 timestamp
  # Kalau sudut jauh lebih stabil → kemungkinan logo/ticker TV.
  if [ "${SRC_DUR_INT:-0}" -ge 120 ] 2>/dev/null; then
    echo "📺 Live-detect: cek chrome statis (logo/ticker)..."
    CORNER_DIFF=0
    CENTER_DIFF=0
    N_PAIR=0
    PREV_C=""
    PREV_M=""
    for frac in 15 35 55 75 90; do
      SS=$(( SRC_DUR_INT * frac / 100 ))
      MAX_SS=$(( SRC_DUR_INT - 2 ))
      [ "$SS" -gt "$MAX_SS" ] && SS=$MAX_SS
      FC="/tmp/rusemeva_live_c_${frac}.png"
      FM="/tmp/rusemeva_live_m_${frac}.png"
      # sudut kiri-atas 12%
      "$FFMPEG_STATIC" -hide_banner -y -ss "$SS" -i "$FILE" -vframes 1 \
        -vf "crop=iw*0.12:ih*0.12:0:0,scale=64:64,format=gray" "$FC" >/dev/null 2>&1 || true
      # tengah 30%
      "$FFMPEG_STATIC" -hide_banner -y -ss "$SS" -i "$FILE" -vframes 1 \
        -vf "crop=iw*0.30:ih*0.30:(iw-ow)/2:(ih-oh)/2,scale=64:64,format=gray" "$FM" >/dev/null 2>&1 || true
      if [ -n "$PREV_C" ] && [ -s "$FC" ] && [ -s "$PREV_C" ]; then
        # mean abs diff via ffmpeg psnr (lower=more similar/static)
        DC=$("$FFMPEG_STATIC" -hide_banner -i "$PREV_C" -i "$FC" -filter_complex "psnr" -f null - 2>&1 | sed -n 's/.*mse_avg:\([0-9.]*\).*/\1/p' | tail -1)
        DM=$("$FFMPEG_STATIC" -hide_banner -i "$PREV_M" -i "$FM" -filter_complex "psnr" -f null - 2>&1 | sed -n 's/.*mse_avg:\([0-9.]*\).*/\1/p' | tail -1)
        DC=${DC%.*}; DM=${DM%.*}
        [ -z "$DC" ] && DC=0
        [ -z "$DM" ] && DM=0
        CORNER_DIFF=$(( CORNER_DIFF + DC ))
        CENTER_DIFF=$(( CENTER_DIFF + DM ))
        N_PAIR=$(( N_PAIR + 1 ))
        echo "   • t=${SS}s corner_mse~$DC center_mse~$DM"
      fi
      PREV_C=$FC; PREV_M=$FM
    done
    rm -f /tmp/rusemeva_live_c_*.png /tmp/rusemeva_live_m_*.png 2>/dev/null || true
    if [ "$N_PAIR" -gt 0 ]; then
      AVG_C=$(( CORNER_DIFF / N_PAIR ))
      AVG_M=$(( CENTER_DIFF / N_PAIR ))
      echo "📺 Live-detect avg: corner_mse=$AVG_C center_mse=$AVG_M"
      # sudut statis (mse kecil) + tengah lebih dinamis → siaran TV
      if [ "$AVG_C" -le 25 ] && [ "$AVG_M" -ge $(( AVG_C * 3 + 5 )) ]; then
        LIVE_MODE=1
        LIVE_REASON="auto-detect chrome TV (logo/ticker)"
      fi
    fi
  fi
fi

VF_LIVE=""
if [ "$LIVE_MODE" = "1" ]; then
  echo "📺 LIVE MODE ON ($LIVE_REASON)"
  # Denoise sangat ringan (siaran sering noisy), jaga detail wajah
  VF_LIVE="hqdn3d=0.8:0.6:2:2"
  # AQ lebih kuat + lookahead lebih panjang + mild deblock (ticker/logo lebih rapi)
  X265_PARAMS="aq-mode=3:aq-strength=1.25:qcomp=0.72:rd=3:psy-rd=1.8:psy-rdoq=1.0:rc-lookahead=60:scenecut=40:deblock=-1,-1:sao=1:strong-intra-smoothing=1:bframes=6"
  CHAT_ID="$CHAT_ID" TG_API_URL="$TG_API_URL" BOT_TOKEN="$BOT_TOKEN" python3 scripts/send_message.py \
    "📺 <b>Live-friendly ON</b> — ${LIVE_REASON}.\\nJaga tengah frame, hemat logo/ticker, denoise ringan. Target tetap ~1.3 Mbps." || true
else
  echo "📺 Live mode OFF (konten general)"
  X265_PARAMS="aq-mode=3:aq-strength=1.0:rd=3:psy-rd=1.5:psy-rdoq=1.0:rc-lookahead=40:scenecut=40"
fi



CHAT_ID="$CHAT_ID" FILENAME="$FILE" TG_API_URL="$TG_API_URL" BOT_TOKEN="$BOT_TOKEN" PROGRESS_MSG_FILE=/tmp/rusemeva_progress_msg_id PROGRESS_STATE_FILE=/tmp/rusemeva_progress_state SRC_DUR_SEC="$SRC_DUR_INT" python3 scripts/progress.py start || true
LAST_PCT=-1
HEVC_LOG=/tmp/rusemeva_hevc_encode.log
# FIX PROGRESS 0%: -progress pipe:1 tulis ke stdout.
# Pakai '3>&1 1>>...' supaya progress ke fd 3 (yg di-pipe ke while),
# stderr+stdout utama ke log. Tanpa ini, progress diambil log, while kosong.
# Live denoise filter (kosong kalau bukan live)
LIVE_VF_ARGS=()
if [ -n "${VF_LIVE:-}" ]; then
  LIVE_VF_ARGS=(-vf "$VF_LIVE")
fi
"$FFMPEG_STATIC" -hide_banner -y -i "$FILE" \
  "${LIVE_VF_ARGS[@]}" \
  -c:v libx265 -profile:v main10 -pix_fmt yuv420p10le \
  -crf ${HEVC_CRF} -preset ${HEVC_PRESET} -maxrate ${MAXRATE_K:-1450}k -bufsize ${BUFSIZE_K:-2900}k \
  -x265-params "${X265_PARAMS}" -tag:v hvc1 \
  -c:a copy -progress pipe:3 "$HEVC_FILE" \
  3> >(while IFS='=' read -r k v; do
    if [ "$k" = "out_time_ms" ]; then
      ms=${v%.*}
      [ "$ms" -gt 0 ] 2>/dev/null || continue
      cur=$(( ms / 1000000 ))
      pct=$(( (cur * 100) / SRC_DUR_INT )); [ "$pct" -gt 100 ] && pct=100
      if [ "$pct" != "$LAST_PCT" ]; then
        LAST_PCT=$pct
        echo "🔄 HEVC encode ${pct}%"
        CHAT_ID="$CHAT_ID" FILENAME="$FILE" TG_API_URL="$TG_API_URL" BOT_TOKEN="$BOT_TOKEN" PROGRESS_MSG_FILE=/tmp/rusemeva_progress_msg_id PROGRESS_STATE_FILE=/tmp/rusemeva_progress_state SRC_DUR_SEC="$SRC_DUR_INT" python3 scripts/progress.py progress "$pct" || true
      fi
    fi
  done) \
  > "$HEVC_LOG" 2>&1
echo "🔄 HEVC encode 100%"
CHAT_ID="$CHAT_ID" FILENAME="$FILE" TG_API_URL="$TG_API_URL" BOT_TOKEN="$BOT_TOKEN" PROGRESS_MSG_FILE=/tmp/rusemeva_progress_msg_id python3 scripts/progress.py done || true

if [ -s "$HEVC_FILE" ]; then
  echo "✅ HEVC selesai: $(ls -lh "$HEVC_FILE" | awk '{print $5}')"

  # === AUTO BITRATE/SIZE GUARD (strict via encode_policy.sh) ===
  # Accept only if bitrate known AND in band (~1.2-1.5 Mbps) AND size vs target.
  # NEED_BETTER (blur) re-enters same loop with CRF-2; no separate pass with || true.
  ORIG_BYTES=$(stat -c%s "$FILE" 2>/dev/null || wc -c < "$FILE")
  TARGET_BYTES=$(target_bytes "$SRC_DUR_INT" "$ORIG_BYTES" || echo 0)
  if [ "${TARGET_BYTES:-0}" -le 0 ]; then
    echo "target_bytes invalid dur=$SRC_DUR_INT orig=$ORIG_BYTES"
    exit 1
  fi
  echo "Size target: $TARGET_BYTES bytes | orig=$ORIG_BYTES dur=${SRC_DUR_INT}s maxrate=${MAXRATE_K:-1450}k"
  CUR_CRF="$HEVC_CRF"
  try=0
  while true; do
    HEVC_BYTES=$(stat -c%s "$HEVC_FILE" 2>/dev/null || wc -c < "$HEVC_FILE")
    HBR_CHK=$(probe_bitrate "$HEVC_FILE" || true)
    if [ -z "$HBR_CHK" ]; then
      sleep 1
      HBR_CHK=$(probe_bitrate "$HEVC_FILE" || true)
    fi
    DECISION=$(accept_hevc "$HEVC_BYTES" "$TARGET_BYTES" "${HBR_CHK:-}" || true)
    echo "size hevc=${HEVC_BYTES} target<=${TARGET_BYTES} bps=${HBR_CHK:-?} decision=${DECISION} crf=${CUR_CRF}"
    if [ "$DECISION" = "OK" ]; then
      echo "Size/bitrate OK crf=$CUR_CRF bps=$HBR_CHK"
      break
    fi
    if [ "$DECISION" = "UNKNOWN" ]; then
      CHAT_ID="$CHAT_ID" TG_API_URL="$TG_API_URL" BOT_TOKEN="$BOT_TOKEN" python3 scripts/send_message.py \
        "Auto-size: gagal baca bitrate HEVC — encode dihentikan." || true
      exit 1
    fi
    if [ "$DECISION" = "NEED_BETTER" ]; then
      NEXT_CRF=$((CUR_CRF - 2))
      [ "$NEXT_CRF" -lt "${MIN_CRF:-22}" ] && NEXT_CRF=${MIN_CRF:-22}
      if [ "$NEXT_CRF" -ge "$CUR_CRF" ]; then
        echo "Already min CRF ${MIN_CRF:-22} — keep (may be soft)"
        break
      fi
      PHASE_NOTE="anti-blur"
    else
      NEXT_CRF=$((CUR_CRF + 2))
      if [ "$NEXT_CRF" -gt "${MAX_CRF:-28}" ]; then
        CHAT_ID="$CHAT_ID" TG_API_URL="$TG_API_URL" BOT_TOKEN="$BOT_TOKEN" python3 scripts/send_message.py \
          "Auto-size: still large at CRF $CUR_CRF (cap ${MAX_CRF:-28}) bps=${HBR_CHK:-?}." || true
        break
      fi
      PHASE_NOTE="auto-size"
      try=$((try + 1))
    fi
    CUR_CRF=$NEXT_CRF
    HEVC_CRF=$CUR_CRF
    CHAT_ID="$CHAT_ID" TG_API_URL="$TG_API_URL" BOT_TOKEN="$BOT_TOKEN" python3 scripts/send_message.py \
      "Re-encode CRF → <code>$CUR_CRF</code> ($PHASE_NOTE)." || true
    CHAT_ID="$CHAT_ID" FILENAME="$FILE" TG_API_URL="$TG_API_URL" BOT_TOKEN="$BOT_TOKEN" \
      PROGRESS_MSG_FILE=/tmp/rusemeva_progress_msg_id PROGRESS_STATE_FILE=/tmp/rusemeva_progress_state \
      SRC_DUR_SEC="$SRC_DUR_INT" PHASE_LABEL="Re-encode CRF $CUR_CRF ($PHASE_NOTE)" \
      python3 scripts/progress.py start || true
    LAST_PCT=-1
    "$FFMPEG_STATIC" -hide_banner -y -i "$FILE" \
      ${LIVE_VF_ARGS[@]+"${LIVE_VF_ARGS[@]}"} \
      -c:v libx265 -profile:v main10 -pix_fmt yuv420p10le \
      -crf ${CUR_CRF} -preset ${HEVC_PRESET} -maxrate ${MAXRATE_K:-1450}k -bufsize ${BUFSIZE_K:-2900}k \
      -x265-params "${X265_PARAMS:-aq-mode=3:aq-strength=1.0}" -tag:v hvc1 \
      -c:a copy -progress pipe:3 "$HEVC_FILE" \
      3> >(while IFS='=' read -r k v; do
        if [ "$k" = "out_time_ms" ]; then
          ms=${v%.*}
          [ "$ms" -gt 0 ] 2>/dev/null || continue
          cur=$(( ms / 1000000 ))
          pct=$(( (cur * 100) / SRC_DUR_INT )); [ "$pct" -gt 100 ] && pct=100
          if [ "$pct" != "$LAST_PCT" ]; then
            LAST_PCT=$pct
            CHAT_ID="$CHAT_ID" FILENAME="$FILE" TG_API_URL="$TG_API_URL" BOT_TOKEN="$BOT_TOKEN" \
              PROGRESS_MSG_FILE=/tmp/rusemeva_progress_msg_id PROGRESS_STATE_FILE=/tmp/rusemeva_progress_state \
              SRC_DUR_SEC="$SRC_DUR_INT" PHASE_LABEL="Re-encode CRF $CUR_CRF ($PHASE_NOTE)" \
              python3 scripts/progress.py progress "$pct" || true
          fi
        fi
      done) \
      > "$HEVC_LOG" 2>&1
    RE_RC=$?
    CHAT_ID="$CHAT_ID" FILENAME="$FILE" TG_API_URL="$TG_API_URL" BOT_TOKEN="$BOT_TOKEN" \
      PROGRESS_MSG_FILE=/tmp/rusemeva_progress_msg_id PHASE_LABEL="Re-encode CRF $CUR_CRF ($PHASE_NOTE)" \
      python3 scripts/progress.py done || true
    if [ ! -s "$HEVC_FILE" ] || [ "${RE_RC:-0}" -ne 0 ]; then
      echo "Re-encode failed crf=$CUR_CRF rc=$RE_RC"
      tail -n 30 "$HEVC_LOG" 2>/dev/null || true
      exit 1
    fi
  done
  echo "HEVC_CRF_FINAL=$CUR_CRF" >> $GITHUB_ENV
  echo "ACT_DURATION_SEC=$SRC_DUR_INT" >> $GITHUB_ENV
  echo "REQ_DURATION_SEC=${REQ_DUR:-}" >> $GITHUB_ENV

  echo "HEVC_FILE=$HEVC_FILE" >> $GITHUB_ENV
  echo "HEVC_SIZE=$(ls -lh "$HEVC_FILE" | awk '{print $5}')" >> $GITHUB_ENV
  HDUR=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$HEVC_FILE" 2>/dev/null | head -1)
  HDUR_INT=${HDUR%.*}
  [ -z "$HDUR_INT" ] && HDUR_INT=0
  echo "HEVC_DUR=$(printf '%02d:%02d:%02d' $((HDUR_INT/3600)) $(((HDUR_INT%3600)/60)) $((HDUR_INT%60)))" >> $GITHUB_ENV
  HW=$(ffprobe -v error -select_streams v:0 -show_entries stream=width -of default=noprint_wrappers=1:nokey=1 "$HEVC_FILE" 2>/dev/null|head -1)
  HH=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of default=noprint_wrappers=1:nokey=1 "$HEVC_FILE" 2>/dev/null|head -1)
  [ -n "$HW" ] && [ -n "$HH" ] && echo "HEVC_RES=${HW}x${HH}" >> $GITHUB_ENV
  HCV=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of default=noprint_wrappers=1:nokey=1 "$HEVC_FILE" 2>/dev/null|head -1)
  [ -n "$HCV" ] && echo "HEVC_VCODEC=$HCV" >> $GITHUB_ENV
  HBR=$(ffprobe -v error -show_entries format=bit_rate -of default=noprint_wrappers=1:nokey=1 "$HEVC_FILE" 2>/dev/null|head -1)
  case "$HBR" in ''|*[!0-9]*) ;; *) echo "HEVC_VBITRATE=$(awk "BEGIN{printf \"%.1f Mbps\", ${HBR}/1000000}")" >> $GITHUB_ENV ;; esac
  # === #4 VERIFIKASI AUDIO: pastikan HEVC punya stream audio ===
  # x265 cuma encode video; -c:a copy harusnya salin audio. Cek biar gak ke-drop.
  AUD=$(ffprobe -v error -select_streams a -show_entries stream=index -of default=noprint_wrappers=1:nokey=1 "$HEVC_FILE" 2>/dev/null | head -1)
  if [ -z "$AUD" ]; then
    echo "🔇 HEVC TIDAK punya audio stream — re-mux dari original..."
    TMP_REMUX="${HEVC_FILE%.mp4}.remux.mp4"
    "$FFMPEG_STATIC" -hide_banner -y -i "$HEVC_FILE" -i "$FILE" \
      -map 0:v:0 -map 1:a? -c copy "$TMP_REMUX" 2>/dev/null || true
    if [ -s "$TMP_REMUX" ]; then
      AUD2=$(ffprobe -v error -select_streams a -show_entries stream=index -of default=noprint_wrappers=1:nokey=1 "$TMP_REMUX" 2>/dev/null | head -1)
      if [ -n "$AUD2" ]; then
        mv -f "$TMP_REMUX" "$HEVC_FILE"
        echo "✅ Audio berhasil di-re-mux ke HEVC."
      else
        rm -f "$TMP_REMUX"
        echo "⚠️ Original juga gak punya audio — HEVC tanpa suara (wajar kalau sumber silent)."
      fi
    else
      rm -f "$TMP_REMUX"
      echo "⚠️ Re-mux gagal — HEVC tanpa suara."
    fi
  else
    echo "🔊 HEVC punya audio stream (#$AUD) — OK."
  fi
  HEVC_THUMB="${HEVC_FILE%.mp4}.jpg"
  HSEEK=1; [ "$HDUR_INT" -gt 6 ] && HSEEK=$((HDUR_INT/2))
  "$FFMPEG_STATIC" -hide_banner -loglevel error -y -ss "$HSEEK" -i "$HEVC_FILE" -frames:v 1 -q:v 2 -vf "scale=640:-2" "$HEVC_THUMB" 2>/dev/null || true
  [ ! -s "$HEVC_THUMB" ] && "$FFMPEG_STATIC" -hide_banner -loglevel error -y -ss 1 -i "$HEVC_FILE" -frames:v 1 -q:v 2 -vf "scale=640:-2" "$HEVC_THUMB" 2>/dev/null || true
  if [ -s "$HEVC_THUMB" ]; then echo "HEVC_THUMB_FILE=$HEVC_THUMB" >> $GITHUB_ENV; echo "HAS_HEVC_THUMB=1" >> $GITHUB_ENV; else echo "HAS_HEVC_THUMB=0" >> $GITHUB_ENV; fi
  # === #7 KALIBRASI: hitung realtime_x aktual & kirim ke worker (KV) ===
  ENC_ELAPSED=$(( SECONDS - ENC_START ))
  echo "ENC_ELAPSED=$ENC_ELAPSED" >> $GITHUB_ENV
  echo "SCENE_LABEL=${SCENE_LABEL:-normal}" >> $GITHUB_ENV
  echo "LIVE_MODE=${LIVE_MODE:-0}" >> $GITHUB_ENV
  if [ "$HDUR_INT" -gt 0 ] && [ "$ENC_ELAPSED" -gt 0 ]; then
    ACT_RT=$(awk "BEGIN{printf \"%.4f\", $HDUR_INT / $ENC_ELAPSED}")
    echo "🎯 realtime_x aktual: ${ACT_RT}x (video ${HDUR_INT}s / encode ${ENC_ELAPSED}s)"
    # Kirim ke worker biar disimpan + di-rata-rata ke KV
    curl -fsS --retry 2 --retry-delay 3 -X POST "${WORKER_URL:-https://rusemeva.rusemeva-vault.workers.dev}/rtcal" \
      -H "Content-Type: application/json" \
      -d "{\"preset\":\"${HEVC_PRESET}\",\"rt\":${ACT_RT},\"secret\":\"${PROGRESS_SECRET}\"}" 2>/dev/null \
      || echo "⚠️ Gagal kirim kalibrasi ke worker (non-fatal)."
    true  # pastikan block ini selalu return 0
  fi
else
  echo "⚠️ Encode HEVC gagal."
  # === #5 ERROR CLASSIFICATION: deteksi penyebab gagal biar notif jujur ===
  # Cek log encode untuk kata kunci umum
  REASON="unknown"
  if grep -qiE "No space left on device|disk full|ENOSPC" "$HEVC_LOG" 2>/dev/null; then
    REASON="disk_full"
  elif grep -qiE "Codec .* not found|Unknown encoder|libx265|Unable to find a suitable output|Invalid data found|moov atom not found" "$HEVC_LOG" 2>/dev/null; then
    REASON="codec_or_corrupt"
  elif grep -qiE "Timeout|timed out|killed|Signal 9|SIGKILL" "$HEVC_LOG" 2>/dev/null; then
    REASON="timeout_or_killed"
  elif grep -qiE "Conversion failed|Error .* frames|Denominator" "$HEVC_LOG" 2>/dev/null; then
    REASON="ffmpeg_error"
  fi
  echo "FAIL_REASON=$REASON" >> $GITHUB_ENV
  echo "🔍 FAIL_REASON=$REASON"
fi

