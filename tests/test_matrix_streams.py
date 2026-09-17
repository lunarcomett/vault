import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('matrix_media', Path(__file__).resolve().parents[1] / 'scripts/matrix_media.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class StreamTests(unittest.TestCase):
    def test_every_track_duration_checked(self):
        info = {'format':{'duration':'60'}, 'streams':[
            {'codec_type':'video','codec_name':'hevc','pix_fmt':'yuv420p10le','duration':'60'},
            {'codec_type':'audio','duration':'60'}, {'codec_type':'audio','duration':'10'}]}
        with patch.object(m, 'probe', return_value=info):
            with self.assertRaisesRegex(ValueError, 'duration'):
                m.validate_encoded('test.mp4',60,2)
    def test_unknown_nan_video_duration_rejected(self):
        for value in ['N/A', 'nan', None, '10']:
            info = {'format':{'duration':'60'}, 'streams':[
                {'codec_type':'video','codec_name':'hevc','pix_fmt':'yuv420p10le','duration':value},
                {'codec_type':'audio','duration':'60'}]}
            with patch.object(m, 'probe', return_value=info):
                with self.assertRaises(ValueError):
                    m.validate_encoded('test.mp4',60,1)
    def test_missing_track_rejected(self):
        info = {'format':{'duration':'60'}, 'streams':[
            {'codec_type':'video','codec_name':'hevc','pix_fmt':'yuv420p10le','duration':'60'},
            {'codec_type':'audio','duration':'60'}]}
        with patch.object(m, 'probe', return_value=info):
            with self.assertRaisesRegex(ValueError,'audio tracks'):
                m.validate_encoded('test.mp4',60,2)
    def test_encoder_maps_all_audio(self):
        script = (Path(__file__).resolve().parents[1] / 'scripts/run_hevc_encode.sh').read_text(encoding='utf-8')
        self.assertEqual(script.count('-map 0:v:0 -map 0:a?'), 2)

if __name__ == '__main__': unittest.main()
