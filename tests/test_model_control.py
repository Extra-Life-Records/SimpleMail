"""Owner setup, secret isolation and cross-process worker lifecycle gates."""
import base64
import os
import json
import time
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from model_control import ModelControl, protect_secret, run_managed
from model_worker import ModelWorker


def fixture_secret(value, decrypt=False):
    if decrypt:
        return base64.b64decode(value).decode()
    return base64.b64encode(value.encode()).decode()


class ModelControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.time = 1000
        self.path = Path(self.temp.name) / 'agent.sqlite3'
        self.control = ModelControl(self.path, fixture_secret, lambda: self.time)
        self.control.set_profile('one', True, 'Draft replies for review')

    def configured(self):
        return self.control.save('one', 'https://provider.example/v1/responses', 'model-one', 'responses', 'private-key')

    def test_state_never_returns_key_or_ciphertext(self):
        state = self.configured()
        self.assertTrue(state['has_key'])
        self.assertNotIn('private-key', str(state))
        self.assertNotIn(fixture_secret('private-key'), str(state))
        self.assertNotIn(b'private-key', self.path.read_bytes())
        self.assertEqual(self.control.connection('one')['key'], 'private-key')
        self.assertFalse(self.control.state('two')['configured'])

    def test_blank_key_keeps_secret_only_for_same_endpoint(self):
        self.configured()
        self.control.save('one', 'https://provider.example/v1/responses', 'model-two', 'responses')
        self.assertEqual(self.control.connection('one')['key'], 'private-key')
        with self.assertRaises(ValueError):
            self.control.save('one', 'https://different.example/v1/responses', 'model-two', 'responses')
        self.assertEqual(self.control.connection('one')['endpoint'], 'https://provider.example/v1/responses')

    def test_local_connection_can_remove_saved_key(self):
        self.configured()
        state = self.control.save('one', 'http://127.0.0.1:1234/v1/chat/completions', 'local', 'chat', clear_key=True)
        self.assertFalse(state['has_key'])
        self.assertEqual(self.control.connection('one')['key'], '')

    def test_remote_key_removal_is_rejected_without_overwriting_connection(self):
        self.configured()
        with self.assertRaises(ValueError):
            self.control.save('one', 'https://provider.example/v1/responses', 'model-one', 'responses', clear_key=True)
        self.assertEqual(self.control.connection('one')['key'], 'private-key')

    def test_invalid_endpoints_never_store_keys(self):
        for endpoint in ('http://remote.example/v1', 'https://user:pass@provider.example/v1',
                         'https://provider.example/v1?key=secret', 'file:///config'):
            with self.assertRaises(ValueError):
                self.control.save('one', endpoint, 'model', 'responses', 'private-key')
        self.assertFalse(self.control.state('one')['configured'])

    def test_concurrent_owner_starts_reserve_only_one_worker(self):
        self.configured()
        def claim(_):
            try:
                return ModelControl(self.path, fixture_secret, lambda: self.time).claim('one')
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            tokens = list(pool.map(claim, range(2)))
        self.assertEqual(sum(token is not None for token in tokens), 1)

    def test_pause_revokes_token_and_blocks_restart_until_stopped(self):
        self.configured()
        token = self.control.claim('one')
        self.control.stop('one')
        with self.assertRaises(ValueError):
            self.control.check('one', token)
        with self.assertRaises(ValueError):
            self.control.claim('one')
        self.control.heartbeat('one', token, 'stopped')
        self.assertNotEqual(self.control.claim('one'), token)

    def test_stale_token_cannot_act_or_update_replacement(self):
        self.configured()
        token = self.control.claim('one')
        self.time += 91
        self.assertEqual(self.control.state('one')['status'], 'interrupted')
        replacement = self.control.claim('one')
        with self.assertRaises(ValueError):
            self.control.check('one', token)
        self.control.heartbeat('one', token, 'failed')
        self.control.check('one', replacement)
        self.assertEqual(self.control.state('one')['status'], 'starting')

    def test_model_edit_is_blocked_while_running(self):
        self.configured()
        self.control.claim('one')
        with self.assertRaises(ValueError):
            self.control.save('one', 'http://localhost:1234/v1/responses', 'other', 'responses')

    def test_launch_failure_is_retryable_without_logging_key(self):
        self.configured()
        with self.assertRaisesRegex(ValueError, 'could not start'):
            self.control.start('one', launcher=Mock(side_effect=OSError('private-key')))
        self.assertEqual(self.control.state('one')['status'], 'failed')
        self.assertNotIn('private-key', str(self.control.state('one')))
        self.control.claim('one')

    def test_launcher_gets_account_token_but_no_provider_secret(self):
        self.configured()
        launch = Mock()
        self.control.start('one', launcher=launch)
        self.assertNotIn('private-key', str(launch.call_args))
        self.assertIn('managed', launch.call_args.args[0])
        self.assertIn('one', launch.call_args.args[0])

    def test_paused_job_cannot_start_worker(self):
        self.configured()
        self.control.set_profile('one', False, 'Draft replies')
        with self.assertRaises(ValueError):
            self.control.claim('one')

    def test_worker_guard_precedes_queue_or_provider_actions(self):
        mailbox, queue, model = Mock(), Mock(unsafe=True), Mock()
        guard = Mock(side_effect=ValueError('Paused'))
        worker = ModelWorker(mailbox, queue, model, owner_guard=guard)
        with self.assertRaises(ValueError):
            worker.run(once=True)
        model.complete.assert_not_called()
        queue.profile.assert_not_called()
        with self.assertRaises(ValueError):
            worker.check({}, {})
        queue.assert_claim.assert_not_called()

    def test_managed_process_records_clean_owner_stop(self):
        self.configured()
        token = self.control.claim('one')
        worker = Mock()
        def stop_during_pass(*args, **kwargs):
            self.control.stop('one')
            return {'status': 'idle'}
        worker.run.side_effect = stop_during_pass
        with patch('model_control.ModelControl', return_value=self.control), patch('model_worker.ModelWorker', return_value=worker):
            run_managed(Mock(), self.path, 'one', token)
        self.assertEqual(self.control.state('one')['status'], 'stopped')
        self.assertFalse(self.control.state('one')['active'])

    def test_real_source_worker_starts_and_pauses_with_isolated_local_mailbox(self):
        root = Path(self.temp.name) / 'SimpleMail'
        root.mkdir()
        (root / 'config.json').write_text(json.dumps({'accounts': [{'id': 'one',
            'label': 'Fixture', 'email': 'fixture@example.invalid', 'imap_host': '127.0.0.1',
            'imap_port': 1, 'smtp_host': '127.0.0.1', 'smtp_port': 1}]}), encoding='utf-8')
        control = ModelControl(root / 'agent' / 'mailbox.sqlite3')
        control.save('one', 'http://127.0.0.1:1/v1/responses', 'fixture', 'responses')
        control.set_profile('one', True, 'Prepare replies for review')
        with patch.dict(os.environ, {'APPDATA': self.temp.name}):
            control.start('one')
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and control.state('one')['status'] not in ('unavailable', 'failed', 'stopped'):
                time.sleep(.2)
            self.assertEqual(control.state('one')['status'], 'unavailable')
        finally:
            control.set_profile('one', False, 'Prepare replies for review')
            control.stop('one')
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and control.state('one')['status'] != 'stopped':
                time.sleep(.2)
        self.assertEqual(control.state('one')['status'], 'stopped')

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_real_windows_secret_roundtrip_and_corruption(self):
        key = 'fixture-key-unicode-\u00e9'
        encrypted = protect_secret(key)
        self.assertNotIn(key, encrypted)
        self.assertEqual(protect_secret(encrypted, True), key)
        with self.assertRaises(ValueError):
            protect_secret(base64.b64encode(b'corrupt').decode(), True)


if __name__ == '__main__':
    unittest.main()
