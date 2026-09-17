import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('matrix_media', Path(__file__).resolve().parents[1] / 'scripts/matrix_media.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg required')
class SilentRoundtripTests(unittest.TestCase):
    def test_silent_single_chunk_roundtrip(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            source = root / 'silent.mp4'
            m.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=160x90:rate=12',
                   '-t','2','-c:v','libx265','-pix_fmt','yuv420p10le','-preset','ultrafast',
                   '-x265-params','pools=1:log-level=error',source])
            inputs = root/'inputs'
            m.prepare(source, inputs)
            os.environ['HEVC_FILE'] = str(source)
            packed = root/'packed'
            m.pack(inputs, 1, packed)
            info = m.finalize(packed, root/'final.mp4')
            self.assertEqual(len(m.streams(info, 'audio')),0)
            self.assertEqual(int(m.streams(info,'video')[0]['nb_frames']),24)
            with self.assertRaisesRegex(ValueError,'audio tracks'):
                m.validate_encoded(source, 2, 1)
            with (packed/'chunk_001.mp4').open('ab') as f:
                f.write(b'bad')
            with self.assertRaisesRegex(ValueError,'checksum'):
                m.finalize(packed, root/'bad.mp4')

if __name__ == '__main__': unittest.main()
