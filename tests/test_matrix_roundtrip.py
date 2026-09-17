"""Real FFmpeg integration; generated fixture, no user media or network."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('matrix_media', ROOT / 'scripts/matrix_media.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg required')
class RoundtripTests(unittest.TestCase):
    def test_two_chunks_roundtrip_and_missing_chunk_rejected(self):
        with tempfile.TemporaryDirectory(prefix='matrix-test-') as temp:
            root = Path(temp)
            source = root / 'source.mp4'
            m.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                   'testsrc2=size=160x90:rate=12', '-f', 'lavfi', '-i',
                   'sine=frequency=440:sample_rate=48000', '-t', '12',
                   '-c:v', 'libx264', '-g', '24', '-keyint_min', '24', '-sc_threshold', '0',
                   '-c:a', 'aac', source])
            inputs = root / 'inputs'
            manifest = m.prepare(source, inputs, threshold=1)
            self.assertEqual(manifest['count'], 2)
            for chunk in manifest['chunks']:
                self.assertAlmostEqual(chunk['duration'], 6, delta=.5)
            self.assertTrue(source.exists())
            joined = root / 'joined'
            joined.mkdir()
            for chunk in manifest['chunks']:
                i = chunk['index']
                self.assertEqual(m.select(inputs, i)['sha256'], chunk['sha256'])
                encoded = root / f'encoded-{i}.mp4'
                m.run(['ffmpeg', '-v', 'error', '-y', '-i', inputs / chunk['file'],
                       '-c:v', 'libx265', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p10le',
                       '-x265-params', 'pools=1:frame-threads=1:log-level=error',
                       '-c:a', 'copy', '-tag:v', 'hvc1', encoded])
                os.environ['HEVC_FILE'] = str(encoded)
                packed = root / f'packed-{i}'
                m.pack(inputs, i, packed)
                for p in packed.iterdir():
                    shutil.copy2(p, joined / p.name)
            final = root / 'final.mp4'
            info = m.finalize(joined, final)
            self.assertAlmostEqual(m.duration(info), 12, delta=.5)
            self.assertEqual(m.streams(info, 'video')[0]['pix_fmt'], 'yuv420p10le')
            self.assertTrue(m.streams(info, 'audio'))
            m.run(['ffmpeg', '-v', 'error', '-xerror', '-i', final, '-f', 'null', '-'])
            self.assertEqual(int(m.streams(info, 'video')[0]['nb_frames']), 144)
            single = root / 'single'
            single_manifest = m.prepare(source, single)
            self.assertEqual(single_manifest['count'], 1)
            self.assertEqual(m.digest(source), m.digest(single / 'chunk_001.mp4'))
            (joined / 'chunk_002.mp4').unlink()
            with self.assertRaisesRegex(ValueError, 'Missing'):
                m.finalize(joined, root / 'invalid.mp4')
            # Tampered source must never enter the encoder.
            with (inputs / 'chunk_001.mp4').open('ab') as f:
                f.write(b'bad')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                m.select(inputs, 1)

if __name__ == '__main__':
    unittest.main()
