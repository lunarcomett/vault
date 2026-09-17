import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('matrix_media', Path(__file__).resolve().parents[1] / 'scripts/matrix_media.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class ProtectionTests(unittest.TestCase):
    def test_marker_preserves_body_and_idempotent(self):
        with tempfile.TemporaryDirectory() as t:
            event = Path(t)/'event.json'
            event.write_text(json.dumps({'inputs':{'release_tag':'tmp-source'}}))
            marker = '<!-- vault-matrix-source-protected -->'
            with patch.dict(os.environ, {'GITHUB_EVENT_PATH':str(event), 'GITHUB_EVENT_NAME':'workflow_dispatch', 'GITHUB_REPOSITORY':'owner/repo'}):
                with patch.object(m, 'run', return_value=subprocess.CompletedProcess([],0,json.dumps({'id':9,'body':'original'}))), patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,json.dumps({'body':'original\n'+marker}))) as update:
                    m.protect_source()
                    self.assertEqual(json.loads(update.call_args.kwargs['input'])['body'],'original\n'+marker)
                with patch.object(m, 'run', return_value=subprocess.CompletedProcess([],0,json.dumps({'id':9,'body':marker}))), patch.object(m.subprocess, 'run') as update:
                    m.protect_source()
                    update.assert_not_called()

if __name__ == '__main__': unittest.main()
