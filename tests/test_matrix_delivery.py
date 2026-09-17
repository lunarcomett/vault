"""Offline tests: never contact Telegram or invoke subprocesses."""
import importlib.util
from pathlib import Path
import unittest
import tempfile
from unittest.mock import Mock, patch
import io
import contextlib


def load_delivery():
    spec = importlib.util.spec_from_file_location("matrix_delivery", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "matrix_delivery.py"


class DeliveryTests(unittest.TestCase):
    def test_response_requires_real_media_receipt(self):
        self.assertTrue(MODULE.exists(), "standalone delivery module missing")
        spec = importlib.util.spec_from_file_location("matrix_delivery", MODULE)
        delivery = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(delivery)
        for media in ("video", "document"):
            good = {"ok": True, "result": {"message_id": 42, media: {"file_id": "file-42"}}}
            self.assertEqual(delivery.validate_response(good, media),
                             {"message_id": 42, "file_id": "file-42"})
            bad = [None, {}, {"ok": False}, {"ok": 1, "result": good["result"]},
                   {"ok": True, "result": None},
                   {"ok": True, "result": {"message_id": 42, "photo": [{"file_id": "photo"}]}},
                   {"ok": True, "result": {"message_id": 42, "text": "done"}},
                   {"ok": True, "result": {media: {"file_id": "file"}}}]
            for mid in (None, 0, -1, True, "42"):
                bad.append({"ok": True, "result": {"message_id": mid, media: {"file_id": "file"}}})
            for fid in (None, "", " ", 123):
                bad.append({"ok": True, "result": {"message_id": 42, media: {"file_id": fid}}})
            for response in bad:
                with self.subTest(media=media, response=response):
                    with self.assertRaises(delivery.DeliveryError):
                        delivery.validate_response(response, media)


class SelectionTests(unittest.TestCase):
    def test_split_selects_parts_even_with_base_and_keeps_everything(self):
        delivery = load_delivery()
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "movie[1].mp4"
            base.write_bytes(b"base")
            parts = [Path(str(base) + f".part-{n:03}") for n in range(2)]
            for part in reversed(parts):
                part.write_bytes(b"part")
            Path(str(base) + ".part-other.tmp").mkdir()
            self.assertTrue(hasattr(delivery, "select_files"), "selection missing")
            self.assertEqual(delivery.select_files(str(base), "1"), ("document", parts))
            self.assertEqual(delivery.select_files(str(base), "0"), ("video", [base]))
            self.assertTrue(all(p.exists() for p in [base] + parts))

    def test_missing_empty_and_invalid_selection_fail_closed(self):
        delivery = load_delivery()
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "movie.mp4"
            for split in ("0", "1", "unexpected"):
                with self.assertRaises(delivery.DeliveryError):
                    delivery.select_files(str(base), split)
            base.touch()
            Path(str(base) + ".part-000").touch()
            for split in ("0", "1"):
                with self.assertRaises(delivery.DeliveryError):
                    delivery.select_files(str(base), split)


class TransportTests(unittest.TestCase):
    def test_streaming_multipart_length_fields_and_timeout(self):
        delivery = load_delivery()
        self.assertTrue(hasattr(delivery, "upload_file"), "streaming transport missing")
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / 'movie.mp4'
            video.write_bytes(b'x' * (2 * 1024 * 1024 + 7))
            thumb = Path(folder) / 'thumb.jpg'
            thumb.write_bytes(b'jpeg')
            response = Mock(status=200)
            response.read.return_value = b'{"ok":true,"result":{"message_id":42,"video":{"file_id":"f"}}}'
            connection = Mock()
            connection.getresponse.return_value = response
            with patch.object(delivery.http.client, 'HTTPConnection', return_value=connection) as factory:
                result = delivery.upload_file('http://localhost:8081', '123:dummy', '7',
                                              video, 'video', '<b>HEVC</b>', thumb)
            self.assertTrue(result['ok'])
            factory.assert_called_once_with('127.0.0.1', 8081, timeout=600)
            connection.putrequest.assert_called_once_with('POST', '/bot123:dummy/sendVideo')
            chunks = [call.args[0] for call in connection.send.call_args_list]
            self.assertLessEqual(max(map(len, chunks)), 1024 * 1024)
            body = b''.join(chunks)
            headers = dict(call.args for call in connection.putheader.call_args_list)
            self.assertEqual(int(headers['Content-Length']), len(body))
            self.assertIn(b'name="video"; filename="movie.mp4"', body)
            self.assertIn(b'name="thumbnail"', body)
            self.assertIn(b'name="supports_streaming"\r\n\r\ntrue', body)
            self.assertIn(b'x' * (2 * 1024 * 1024 + 7), body)
            connection.close.assert_called_once()

    def test_http_remote_rejected_before_connection(self):
        delivery = load_delivery()
        for url in ('http://example.com', 'http://192.168.1.2', 'ftp://localhost',
                    'https://user:pass@example.com', 'https://example.com?q=x',
                    'http://localhost.evil', 'https://example.com/#fragment'):
            with self.subTest(url=url):
                with self.assertRaises(delivery.DeliveryError):
                    delivery.upload_file(url, '123:dummy', '7', Path('missing'), 'video', '')

    def test_https_document_and_bad_http_json_fail_without_retry(self):
        delivery = load_delivery()
        with tempfile.TemporaryDirectory() as folder:
            part = Path(folder) / 'movie.mp4.part-000'
            part.write_bytes(b'part')
            for status, body, fails in ((200, b'{"ok":true}', False),
                                        (500, b'secret upstream error', True),
                                        (302, b'', True), (200, b'not json', True)):
                connection = Mock()
                connection.getresponse.return_value = Mock(status=status)
                connection.getresponse.return_value.read.return_value = body
                with patch.object(delivery.http.client, 'HTTPSConnection', return_value=connection) as factory:
                    if fails:
                        with self.assertRaises(delivery.DeliveryError):
                            delivery.upload_file('https://example.com/api', '123:dummy', '7', part, 'document', '')
                    else:
                        delivery.upload_file('https://example.com/api', '123:dummy', '7', part, 'document', '')
                    factory.assert_called_once()
                connection.putrequest.assert_called_once_with('POST', '/api/bot123:dummy/sendDocument')
                wire = b''.join(c.args[0] for c in connection.send.call_args_list)
                self.assertIn(b'name="document"', wire)
                self.assertIn(b'application/octet-stream', wire)
                self.assertNotIn(b'supports_streaming', wire)
                connection.close.assert_called_once()


class MainTests(unittest.TestCase):
    def setUp(self):
        self.delivery = load_delivery()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'movie&name.mp4'
        self.base.write_bytes(b'video')
        self.output = Path(self.temp.name) / 'outputs'
        self.env = {'BOT_TOKEN': '123:dummy', 'CHAT_ID': '7', 'HEVC_FILE': str(self.base),
                    'HEVC_SPLIT': '0', 'GITHUB_OUTPUT': str(self.output), 'JOB_STATUS': 'success',
                    'HEVC_DUR': '01:00:00', 'HEVC_RES': '1280x720', 'HEVC_SIZE': '10 MB',
                    'HEVC_VCODEC': 'hevc', 'HEVC_VBITRATE': '1 Mbps'}

    def run_main(self, transport):
        self.assertTrue(hasattr(self.delivery, 'main'), 'entry point missing')
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = self.delivery.main(self.env, transport=transport)
        return code, out.getvalue() + err.getvalue()

    def test_single_receipt_sets_output_with_escaped_caption_and_metadata(self):
        transport = Mock(return_value={'ok': True, 'result': {'message_id': 8, 'video': {'file_id': 'f'}}})
        code, _ = self.run_main(transport)
        self.assertEqual(code, 0)
        self.assertEqual(self.output.read_text().splitlines()[-1], 'delivered=true')
        args = transport.call_args.args
        self.assertEqual(args[0], 'http://localhost:8081')
        self.assertEqual(args[3:5], (self.base, 'video'))
        caption = args[5]
        for text in ('movie&amp;name.mp4', '01:00:00', '1280x720', '10 MB', 'hevc', '1 Mbps'):
            self.assertIn(text, caption)
        self.assertNotIn(self.temp.name, caption)
        transport.assert_called_once()
        self.assertTrue(self.base.exists())

    def test_split_all_receipts_required_and_files_retained(self):
        self.env['HEVC_SPLIT'] = '1'
        parts = [Path(str(self.base) + f'.part-{n:03}') for n in range(3)]
        for part in parts:
            part.write_bytes(b'part')
        for fail_at in (None, 0, 1, 2):
            if self.output.exists():
                self.output.unlink()
            calls = []
            def transport(*args):
                calls.append(args)
                self.assertNotIn('delivered=true', self.output.read_text())
                if len(calls) - 1 == fail_at:
                    return {'ok': False, 'description': 'secret 123:dummy'}
                return {'ok': True, 'result': {'message_id': len(calls), 'document': {'file_id': 'f'}}}
            code, logs = self.run_main(transport)
            self.assertEqual(code, 0 if fail_at is None else 1)
            self.assertEqual(len(calls), 3 if fail_at is None else fail_at + 1)
            self.assertEqual([c[3] for c in calls], parts[:len(calls)])
            self.assertTrue(all(c[4] == 'document' for c in calls))
            self.assertEqual('delivered=true' in self.output.read_text(), fail_at is None)
            self.assertTrue(all(p.exists() for p in [self.base] + parts))
            self.assertNotIn('123:dummy', logs)

    def test_bad_receipts_and_transport_exception_never_deliver_or_leak(self):
        for response in ({'ok': False}, {'ok': True, 'result': {'message_id': 4, 'photo': []}},
                         {'ok': True, 'result': {'message_id': 4, 'text': 'done'}},
                         {'ok': True, 'result': {'video': {'file_id': 'f'}}},
                         RuntimeError('http://localhost:8081/bot123:dummy/sendVideo')):
            transport = Mock(side_effect=response) if isinstance(response, Exception) else Mock(return_value=response)
            code, logs = self.run_main(transport)
            self.assertEqual(code, 1)
            self.assertNotIn('delivered=true', self.output.read_text())
            self.assertNotIn('123:dummy', logs)
            self.assertNotIn('http://', logs)
            self.assertTrue(self.base.exists())
            transport.assert_called_once()

    def test_caption_fits_telegram_limit_with_astral_unicode(self):
        env = {key: '\U0001f600' * 1000 for key in self.env}
        caption = self.delivery.build_caption(env, Path('\U0001f600' * 200 + '.mp4'), 'document', 1, 2)
        import re
        import html
        plain = html.unescape(re.sub(r'<[^>]*>', '', caption))
        self.assertLessEqual(len(plain.encode('utf-16-le')) // 2, 1024)

    def test_optional_thumbnail_and_output_error(self):
        self.env['HAS_HEVC_THUMB'] = '1'
        thumb = Path(self.temp.name) / 'thumb.jpg'
        self.env['HEVC_THUMB_FILE'] = str(thumb)
        good = {'ok': True, 'result': {'message_id': 8, 'video': {'file_id': 'f'}}}
        for exists in (False, True):
            if exists:
                thumb.write_bytes(b'jpeg')
            transport = Mock(return_value=good)
            code, _ = self.run_main(transport)
            self.assertEqual(code, 0)
            self.assertEqual(transport.call_args.args[6], thumb if exists else None)
        self.env['GITHUB_OUTPUT'] = self.temp.name  # Directory is not writable as output file.
        transport = Mock()
        code, _ = self.run_main(transport)
        self.assertEqual(code, 1)
        transport.assert_not_called()

    def test_failed_job_and_missing_credentials_never_upload(self):
        for key, value in (('JOB_STATUS', 'failure'), ('BOT_TOKEN', ''), ('CHAT_ID', ''), ('HEVC_FILE', '')):
            with patch.dict(self.env, {key: value}):
                transport = Mock()
                code, _ = self.run_main(transport)
                self.assertEqual(code, 1)
                transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
