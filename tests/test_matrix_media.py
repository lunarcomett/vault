import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

class PlanningTests(unittest.TestCase):
    def test_two_hour_boundary(self):
        path = ROOT / 'scripts/matrix_media.py'
        self.assertTrue(path.exists(), 'matrix helper missing')
        spec = importlib.util.spec_from_file_location('matrix_media', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for duration, count in [(3600, 1), (7200, 1), (7200.01, 2), (10800, 2), (14400, 2)]:
            self.assertEqual(module.chunk_count(duration), count)
        for duration in [0, -1, float('nan'), float('inf')]:
            with self.assertRaises(ValueError):
                module.chunk_count(duration)

class InputTests(unittest.TestCase):
    def test_safe_names(self):
        spec = importlib.util.spec_from_file_location('matrix_media', ROOT / 'scripts/matrix_media.py')
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertEqual(m.safe_name('Video pilihan 1.mp4'), 'Video pilihan 1.mp4')
        for name in ['../a.mp4', 'a/b.mp4', 'a\\\\b.mp4', '-file.mp4', 'a\n.mp4', 'a$(id).mp4']:
            with self.assertRaises(ValueError):
                m.safe_name(name)

if __name__ == '__main__':
    unittest.main()
