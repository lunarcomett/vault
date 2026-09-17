#!/usr/bin/env python3
"""Validated media handoffs for separate GitHub matrix runners. No network calls."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess


def chunk_count(duration, threshold=7200):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Source duration must be finite and positive')
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError('Threshold must be finite and positive')
    return 2 if duration > threshold else 1


def run(args):
    return subprocess.run([str(v) for v in args], check=True, capture_output=True, text=True)


def probe(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', path]).stdout)


def duration(info):
    value = float(info['format']['duration'])
    chunk_count(value)
    return value


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def emit(key, value, destination='GITHUB_OUTPUT'):
    value = str(value)
    if '\n' in value or '\r' in value:
        raise ValueError('Multiline workflow values rejected')
    if os.environ.get(destination):
        with open(os.environ[destination], 'a', encoding='utf-8') as stream:
            stream.write(f'{key}={value}\n')


def write_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2), encoding='utf-8')


def safe_name(value):
    if not value or value in ('.', '..') or any(c in value for c in '/\\\r\n\x00'):
        raise ValueError('Invalid basename')
    # Keep paths safe for shell commands in the legacy encoder.
    if not re.fullmatch(r'[\w .()\-]+', value, flags=re.UNICODE):
        raise ValueError('Filename contains unsupported characters')
    if value.startswith('-'):
        raise ValueError('Filename cannot start with option prefix')
    return value


def inputs():
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    data = event.get('client_payload', {}) if os.environ.get('GITHUB_EVENT_NAME') == 'repository_dispatch' else event.get('inputs', {})
    values = {key: str(data.get(key) or default) for key, default in {
        'filename':'', 'chat_id':'', 'release_tag':'', 'duration':'0',
        'human_duration':'', 'hevc_preset':'veryfast', 'hevc_crf':'24', 'job_id':''}.items()}
    safe_name(values['filename'])
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', values['release_tag']) or values['release_tag'].startswith('-'):
        raise ValueError('Invalid release tag')
    if not re.fullmatch(r'-?\d+', values['chat_id']):
        raise ValueError('Invalid chat ID')
    if values['hevc_preset'] not in ['ultrafast','superfast','veryfast','faster','fast','medium','slow','slower','veryslow','placebo']:
        raise ValueError('Invalid preset')
    if not values['hevc_crf'].isdigit() or not 0 <= int(values['hevc_crf']) <= 51:
        raise ValueError('Invalid CRF')
    if not values['duration'].isdigit():
        raise ValueError('Invalid requested duration')
    for key, value in values.items():
        emit(key, value)


def ffmpeg():
    return os.environ.get('FFMPEG_STATIC') or 'ffmpeg'


def streams(info, kind):
    return [s for s in info['streams'] if s['codec_type'] == kind]


def prepare(source, directory, threshold=7200):
    source, directory = Path(source), Path(directory)
    info = probe(source)
    total = duration(info)
    if len(streams(info, 'video')) != 1:
        raise ValueError('Exactly one video stream required')
    count = chunk_count(total, threshold)
    directory.mkdir(parents=True, exist_ok=True)
    if list(directory.glob('chunk_*')):
        raise ValueError('Refusing stale chunk directory')
    if count == 1:
        shutil.copy2(source, directory / 'chunk_001.mp4')
    else:
        # One split point only: segment_time can accidentally produce a tiny third part.
        run([ffmpeg(), '-v', 'error', '-y', '-i', source, '-map', '0:v:0', '-map', '0:a?',
             '-c', 'copy', '-avoid_negative_ts', 'disabled', '-f', 'segment', '-segment_times', f'{total / 2:.6f}',
             '-segment_start_number', '1', '-reset_timestamps', '1', directory / 'chunk_%03d.mp4'])
    paths = sorted(directory.glob('chunk_*.mp4'))
    if len(paths) != count:
        raise ValueError(f'Expected {count} chunks, got {len(paths)}; no usable midpoint keyframe')
    chunks = []
    for i, path in enumerate(paths, 1):
        part_info = probe(path)
        part_duration = duration(part_info)
        if bool(streams(info, 'audio')) != bool(streams(part_info, 'audio')):
            raise ValueError('Audio missing from source chunk')
        if count == 2 and abs(part_duration - total / 2) > max(5, total * .02):
            raise ValueError('Keyframe spacing prevents balanced split')
        chunks.append({'index':i, 'file':path.name, 'duration':part_duration, 'sha256':digest(path)})
    if abs(sum(c['duration'] for c in chunks) - total) > max(2, count * .5):
        raise ValueError('Split duration mismatch')
    manifest = {'source_name':source.name, 'source_duration':total, 'count':count,
                'audio_count':len(streams(info, 'audio')), 'chunks':chunks}
    write_json(directory / 'manifest.json', manifest)
    emit('matrix', json.dumps({'index':list(range(1, count + 1))}, separators=(',', ':')))
    emit('count', count)
    emit('source_duration', total)
    print(f'Prepared {count} chunks; source {total:.3f}s')
    return manifest


def load_manifest(directory):
    manifest = json.loads((Path(directory) / 'manifest.json').read_text(encoding='utf-8'))
    n = manifest['count']
    if n not in (1, 2) or [c['index'] for c in manifest['chunks']] != list(range(1, n+1)):
        raise ValueError('Invalid chunk manifest')
    for c in manifest['chunks']:
        if c['file'] != f"chunk_{c['index']:03d}.mp4":
            raise ValueError('Invalid chunk filename')
    return manifest


def select(directory, index):
    directory = Path(directory)
    manifest = load_manifest(directory)
    if not 1 <= index <= manifest['count']:
        raise ValueError('Chunk index out of range')
    chunk = manifest['chunks'][index-1]
    path = directory / chunk['file']
    if digest(path) != chunk['sha256']:
        raise ValueError('Source chunk checksum mismatch')
    emit('ORIG_FILE', path.resolve())
    emit('duration', math.ceil(chunk['duration']))
    return chunk


def video_signature(info):
    fields = ['codec_name', 'profile', 'pix_fmt', 'width', 'height', 'time_base']
    video = streams(info, 'video')[0]
    return {key:video.get(key) for key in fields}


def audio_signature(info):
    return [{key:s.get(key) for key in ['codec_name','sample_rate','channels','channel_layout','time_base']}
            for s in streams(info, 'audio')]


def stream_duration(info, kind):
    values = [float(s['duration']) for s in streams(info, kind) if s.get('duration') not in (None, 'N/A')]
    return max(values, default=0.0)


def validate_encoded(path, expected_duration, audio_count):
    info = probe(path)
    video = streams(info, 'video')
    if len(video) != 1 or video[0]['codec_name'] != 'hevc' or video[0]['pix_fmt'] != 'yuv420p10le':
        raise ValueError('Output must contain one HEVC 10-bit video')
    if len(streams(info, 'audio')) != audio_count:
        raise ValueError(f'Encoded audio tracks {len(streams(info, "audio"))} != source {audio_count}')
    # Container duration can hide a truncated stream; require each stream to match.
    for kind in ('video', 'audio'):
        for stream in streams(info, kind):
            try:
                stream_dur = float(stream['duration'])
            except (KeyError, TypeError, ValueError):
                raise ValueError(f'Encoded {kind} stream duration unknown') from None
            if not math.isfinite(stream_dur) or stream_dur <= 0 or abs(stream_dur - expected_duration) > 2:
                raise ValueError(f'Encoded {kind} stream duration mismatch: {stream_dur:.3f}s vs {expected_duration:.3f}s')
    if abs(duration(info) - expected_duration) > 2:
        raise ValueError('Encoded duration mismatch')
    return info


def pack(directory, index, output_dir):
    manifest = load_manifest(directory)
    chunk = select(directory, index)
    source = Path(os.environ['HEVC_FILE'])
    info = validate_encoded(source, chunk['duration'], manifest['audio_count'])
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / chunk['file']
    shutil.copy2(source, target)
    metadata = {'index':index, 'sha256':digest(target), 'duration':duration(info),
                'video':video_signature(info), 'audio':audio_signature(info)}
    write_json(target.with_suffix('.json'), metadata)
    write_json(output_dir / 'manifest.json', manifest)
    print(f'Packed encoded chunk {index}; {duration(info):.3f}s')


def finalize(directory, output):
    directory, output = Path(directory), Path(output)
    manifest = load_manifest(directory)
    expected = {c['file'] for c in manifest['chunks']}
    if {p.name for p in directory.glob('chunk_*.mp4')} != expected:
        raise ValueError('Missing or unexpected encoded chunk')
    infos = []
    for chunk in manifest['chunks']:
        path = directory / chunk['file']
        metadata = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        if metadata['index'] != chunk['index'] or digest(path) != metadata['sha256']:
            raise ValueError('Encoded chunk checksum mismatch')
        infos.append(validate_encoded(path, chunk['duration'], manifest['audio_count']))
    for info in infos[1:]:
        if video_signature(info) != video_signature(infos[0]) or audio_signature(info) != audio_signature(infos[0]):
            raise ValueError('Incompatible encoded streams; refusing concat')
    # Video durasi menentukan utuh/tidaknya; audio container bisa lebih panjang.
    for chunk, info in zip(manifest['chunks'], infos):
        video_dur = stream_duration(info, 'video')
        if video_dur and abs(video_dur - chunk['duration']) > 2:
            raise ValueError('Encoded chunk video truncated')
    output.parent.mkdir(parents=True, exist_ok=True)
    if manifest['count'] == 1:
        shutil.copy2(directory / manifest['chunks'][0]['file'], output)
    else:
        listing = directory / 'concat.txt'
        # Relative fixed names: no user-controlled path in concat syntax.
        listing.write_text(''.join(f"file '{c['file']}'\n" for c in manifest['chunks']), encoding='utf-8')
        run([ffmpeg(), '-v', 'warning', '-y', '-f', 'concat', '-safe', '1', '-i', listing,
             '-map', '0:v:0', '-map', '0:a?', '-c', 'copy', '-movflags', '+faststart', output])
    info = validate_encoded(output, manifest['source_duration'], manifest['audio_count'])
    # Decode around each join to detect corrupt packets; full decode exercised in CI smoke.
    for boundary in [sum(duration(x) for x in infos[:i]) for i in range(1, len(infos))]:
        run([ffmpeg(), '-v', 'error', '-xerror', '-ss', str(max(0,boundary-2)), '-i', output,
             '-t', '4', '-map', '0:v:0', '-map', '0:a?', '-f', 'null', '-'])
    total = duration(info)
    video = streams(info, 'video')[0]
    thumb = output.with_suffix('.jpg')
    run([ffmpeg(), '-v', 'error', '-y', '-ss', str(total/2), '-i', output,
         '-frames:v', '1', '-vf', 'scale=640:-2', '-q:v', '2', thumb])
    seconds = round(total)
    env = {'HEVC_FILE':output.resolve(), 'HEVC_SIZE':f'{output.stat().st_size/1024**2:.1f} MiB',
           'ACT_DURATION_SEC':seconds, 'HEVC_DUR':f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}',
           'HEVC_RES':f"{video['width']}x{video['height']}", 'HEVC_VCODEC':'hevc',
           'HEVC_VBITRATE':f"{int(info['format'].get('bit_rate',0))/1e6:.2f} Mbps",
           'HEVC_THUMB_FILE':thumb.resolve(), 'HAS_HEVC_THUMB':'1', 'HEVC_SPLIT':'0'}
    for key, value in env.items():
        emit(key, value, 'GITHUB_ENV')
    print(f'Validated final MP4: {total:.3f}s; {output.stat().st_size} bytes; {manifest["count"]} chunks')
    return info


def protect_source():
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    data = event.get('client_payload', {}) if os.environ.get('GITHUB_EVENT_NAME') == 'repository_dispatch' else event.get('inputs', {})
    tag = str(data['release_tag'])
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', tag) or tag.startswith('-'):
        raise ValueError('Invalid release tag')
    repo = os.environ['GITHUB_REPOSITORY']
    release = json.loads(run(['gh', 'api', f'repos/{repo}/releases/tags/{tag}']).stdout)
    marker = '<!-- vault-matrix-source-protected -->'
    body = release.get('body') or ''
    if marker not in body:
        result = subprocess.run(['gh', 'api', '-X', 'PATCH', f"repos/{repo}/releases/{int(release['id'])}", '--input', '-'],
                                input=json.dumps({'body':body + '\n' + marker}), capture_output=True, text=True, check=True)
        if marker not in json.loads(result.stdout).get('body',''):
            raise ValueError('Release protection not confirmed')
    print('Source protected from age-only sweeper until confirmed delivery')


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('inputs')
    sub.add_parser('protect-source')
    p = sub.add_parser('prepare')
    p.add_argument('--input', required=True)
    p.add_argument('--output-dir', required=True)
    p.add_argument('--threshold-seconds', type=float, default=7200)
    for name in ['select', 'pack']:
        p = sub.add_parser(name)
        p.add_argument('--directory', required=True)
        p.add_argument('--index', type=int, required=True)
        if name == 'pack':
            p.add_argument('--output-dir', required=True)
    p = sub.add_parser('finalize')
    p.add_argument('--directory', required=True)
    p.add_argument('--output', required=True)
    a = parser.parse_args()
    if a.command == 'inputs': inputs()
    elif a.command == 'protect-source': protect_source()
    elif a.command == 'prepare': prepare(a.input, a.output_dir, a.threshold_seconds)
    elif a.command == 'select': select(a.directory, a.index)
    elif a.command == 'pack': pack(a.directory, a.index, a.output_dir)
    elif a.command == 'finalize': finalize(a.directory, a.output)


if __name__ == '__main__':
    main()
