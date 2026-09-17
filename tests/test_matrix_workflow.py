"""Offline workflow contracts. Run: python -m unittest discover -s tests -p test_matrix_workflow.py -v

Requires PyYAML; no GitHub, Telegram, media, or network calls.
"""
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    workflow = yaml.safe_load((ROOT / '.github' / 'workflows' / name).read_text(encoding='utf-8'))
    # PyYAML 1.1 parses a bare `on:` key as boolean True; normalize for assertions.
    return {'on': workflow.pop(True, None), **workflow}


def step(job, *, id=None, uses=None, contains=None):
    matches = [s for s in job['steps'] if
               (id is None or s.get('id') == id) and
               (uses is None or s.get('uses', '').startswith(uses)) and
               (contains is None or contains in s.get('run', ''))]
    if len(matches) != 1:
        raise AssertionError(f'Expected one step: {id=}, {uses=}, {contains=}; got {len(matches)}')
    return matches[0]


class MatrixWorkflowTests(unittest.TestCase):
    def test_production_handoff_chunk_budget_and_confirmed_delivery(self):
        workflow = load('transcode.yml')
        jobs = workflow['jobs']
        self.assertEqual(set(jobs), {'prepare', 'encode', 'finalize', 'failure-notify'})
        prepare, encode, finalize = (jobs[n] for n in ('prepare', 'encode', 'finalize'))
        for job in (prepare, encode, finalize):
            self.assertEqual(job['runs-on'], 'ubuntu-latest')
            step(job, uses='actions/checkout@v4')
        self.assertEqual(encode['needs'], 'prepare')
        self.assertEqual(set(finalize['needs']), {'prepare', 'encode'})
        self.assertEqual(encode['strategy']['max-parallel'], 2)
        self.assertIs(encode['strategy']['fail-fast'], False)
        self.assertEqual(encode['strategy']['matrix'], '${{ fromJSON(needs.prepare.outputs.matrix) }}')
        step(prepare, id='inputs', contains='python3 scripts/matrix_media.py inputs')
        for key in ('filename', 'chat_id', 'release_tag', 'duration', 'human_duration',
                    'hevc_preset', 'hevc_crf', 'job_id'):
            self.assertEqual(prepare['outputs'][key], '${{ steps.inputs.outputs.' + key + ' }}')
        for key in ('matrix', 'count', 'source_duration'):
            self.assertEqual(prepare['outputs'][key], '${{ steps.split.outputs.' + key + ' }}')
        split = step(prepare, id='split', contains='scripts/matrix_media.py prepare')
        self.assertIn('--output-dir /tmp/matrix-input', split['run'])
        self.assertNotIn('--threshold-seconds', split['run'])  # production uses helper's 7200s boundary
        download = step(prepare, id='dl')
        self.assertEqual(download['env']['TAG'], '${{ steps.inputs.outputs.release_tag }}')
        self.assertEqual(download['env']['RAW'], '${{ steps.inputs.outputs.filename }}')
        uploaded = step(prepare, uses='actions/upload-artifact@v4')['with']
        self.assertEqual(uploaded['name'], 'matrix-input')
        self.assertEqual(uploaded['path'], '/tmp/matrix-input')
        self.assertEqual(step(encode, uses='actions/download-artifact@v4')['with'],
                         {'name': 'matrix-input', 'path': '/tmp/matrix-input'})
        select = step(encode, id='select', contains='scripts/matrix_media.py select')
        self.assertIn('--index "$CHUNK_INDEX"', select['run'])
        self.assertEqual(select['env']['CHUNK_INDEX'], '${{ matrix.index }}')
        autodg = step(encode, id='autodg')
        self.assertEqual(autodg['env']['DUR'], '${{ steps.select.outputs.duration }}')
        self.assertEqual(autodg['env']['ORIG'], '${{ steps.select.outputs.ORIG_FILE }}')
        self.assertIn('timeout 90', autodg['run'])
        self.assertIn('LIMIT_MIN=320', autodg['run'])
        hevc = step(encode, id='hevc')
        self.assertEqual(hevc['run'], 'bash scripts/run_hevc_encode.sh')
        self.assertEqual(hevc['env']['ENCODE_PROFILE'], 'live')
        for key in ('DURATION_SEC', 'REQUESTED_DURATION'):
            self.assertEqual(hevc['env'][key], '${{ steps.select.outputs.duration }}')
        self.assertEqual(hevc['env']['ORIG_FILE'], '${{ steps.select.outputs.ORIG_FILE }}')
        pack = step(encode, contains='scripts/matrix_media.py pack')
        self.assertIn('--output-dir "/tmp/matrix-output-$CHUNK_INDEX"', pack['run'])
        self.assertEqual(pack['env']['HEVC_FILE'], '${{ env.HEVC_FILE }}')
        encoded = step(encode, uses='actions/upload-artifact@v4')['with']
        self.assertEqual(encoded['name'], 'encoded-${{ matrix.index }}')
        self.assertEqual(encoded['path'], '/tmp/matrix-output-${{ matrix.index }}')
        for upload in (uploaded, encoded):
            self.assertEqual(upload['retention-days'], 3)
            self.assertEqual(upload['compression-level'], 0)
            self.assertEqual(upload['if-no-files-found'], 'error')
        joined = step(finalize, uses='actions/download-artifact@v4')['with']
        self.assertEqual(joined, {'pattern': 'encoded-*', 'merge-multiple': True,
                                  'path': '/tmp/matrix-encoded'})
        join = step(finalize, contains='scripts/matrix_media.py finalize')
        self.assertIn('--directory /tmp/matrix-encoded', join['run'])
        self.assertEqual(join['env']['RAW'], '${{ needs.prepare.outputs.filename }}')
        delivery = step(finalize, id='delivery')
        self.assertEqual(delivery['run'], 'python3 scripts/matrix_delivery.py')
        self.assertFalse(delivery.get('continue-on-error', False))
        self.assertNotIn('always()', delivery.get('if', ''))
        cleanup = step(finalize, contains='gh release delete')
        self.assertIn("steps.delivery.outputs.delivered == 'true'", cleanup['if'])
        self.assertEqual(cleanup['env']['TAG'], '${{ needs.prepare.outputs.release_tag }}')
        self.assertGreater(finalize['steps'].index(cleanup), finalize['steps'].index(delivery))
        self.assertNotIn('rm -f "$HF"', step(finalize, contains='split -b 1990M')['run'])
        failure = jobs['failure-notify']
        self.assertEqual(set(failure['needs']), {'prepare', 'encode', 'finalize'})
        self.assertIn('always()', failure['if'])
        for name in ('prepare', 'encode', 'finalize'):
            self.assertIn(f"needs.{name}.result == 'failure'", failure['if'])
        step(failure, contains='scripts/send_message.py')
        for job in jobs.values():
            for s in job['steps']:
                # Event data must travel through env, never shell source substitution.
                self.assertNotRegex(s.get('run', ''), r'\$\{\{.*?(?:inputs\.|client_payload|outputs\.)')
                self.assertNotIn('gh api repos/${{ github.repository }}/actions/runs/', s.get('run', ''))


class SmokeWorkflowTests(unittest.TestCase):
    def test_smoke_two_runner_split_encode_concat_is_offline(self):
        workflow = load('matrix-smoke.yml')
        jobs = workflow['jobs']
        self.assertEqual(set(jobs), {'prepare', 'encode', 'finalize'})
        prepare, encode, finalize = (jobs[n] for n in ('prepare', 'encode', 'finalize'))
        self.assertEqual(set(workflow['on']), {'workflow_dispatch'})
        self.assertEqual(prepare['outputs']['matrix'], '${{ steps.split.outputs.matrix }}')
        for job in (prepare, encode, finalize):
            self.assertEqual(job['runs-on'], 'ubuntu-latest')
            step(job, uses='actions/checkout@v4')
        self.assertEqual(encode['needs'], 'prepare')
        self.assertEqual(set(finalize['needs']), {'prepare', 'encode'})
        self.assertEqual(encode['strategy']['max-parallel'], 2)
        self.assertIs(encode['strategy']['fail-fast'], False)
        self.assertEqual(encode['strategy']['matrix'], '${{ fromJSON(needs.prepare.outputs.matrix) }}')
        gen = step(prepare, contains='testsrc2=size=160x90:rate=12:duration=12')
        self.assertIn('-g 24', gen['run'])
        self.assertIn('-f lavfi -i sine=', gen['run'])
        split = step(prepare, id='split', contains='scripts/matrix_media.py prepare')
        self.assertIn('--threshold-seconds 1', split['run'])
        count = step(prepare, id='assert-two')
        self.assertEqual(count['env']['COUNT'], '${{ steps.split.outputs.count }}')
        self.assertIn('test "$COUNT" = "2"', count['run'])
        for job in (prepare, encode, finalize):
            self.assertLessEqual(job['timeout-minutes'], 15)
        self.assertNotIn('secrets.', (ROOT / '.github/workflows/matrix-smoke.yml').read_text())
        self.assertEqual(step(encode, uses='actions/download-artifact@v4')['with'],
                         {'name': 'matrix-input', 'path': '/tmp/matrix-input'})
        select = step(encode, id='select', contains='scripts/matrix_media.py select')
        self.assertIn('--index "$CHUNK_INDEX"', select['run'])
        encode_step = step(encode, contains='-c:v libx265 -profile:v main10')
        self.assertIn('-preset ultrafast', encode_step['run'])
        pack = step(encode, contains='scripts/matrix_media.py pack')
        self.assertIn('--output-dir "/tmp/matrix-output-$CHUNK_INDEX"', pack['run'])
        self.assertEqual(step(encode, uses='actions/upload-artifact@v4')['with']['name'],
                         'encoded-${{ matrix.index }}')
        joined = step(finalize, uses='actions/download-artifact@v4')['with']
        self.assertEqual(joined, {'pattern': 'encoded-*', 'merge-multiple': True,
                                  'path': '/tmp/matrix-encoded'})
        step(finalize, contains='scripts/matrix_media.py finalize')
        decode = step(finalize, contains='-f null -')
        self.assertIn('-xerror', decode['run'])
        for job in jobs.values():
            for s in job['steps']:
                run = s.get('run', '')
                self.assertNotIn('send_message.py', run)
                self.assertNotIn('notify.py', run)
                self.assertNotIn('matrix_delivery.py', run)
                self.assertNotIn('api.telegram.org', run)


if __name__ == '__main__':
    unittest.main()

