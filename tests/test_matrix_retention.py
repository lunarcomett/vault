import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

class RetentionTests(unittest.TestCase):
    def test_sweeper_respects_protected_matrix_sources(self):
        workflow = (ROOT / '.github/workflows/release-sweeper.yml').read_text(encoding='utf-8')
        self.assertIn('vault-matrix-source-protected', workflow)
        self.assertIn('contains("vault-matrix-source-protected") | not', workflow)

if __name__ == '__main__': unittest.main()
