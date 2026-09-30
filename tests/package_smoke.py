"""Check release architecture and console behavior without real credentials/network."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import argparse
import struct
import time
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

parser = argparse.ArgumentParser()
parser.add_argument('--arch', choices=('x64', 'arm64'), required=True)
parser.add_argument('--directory', type=Path, required=True)
args = parser.parse_args()
for name in ('SimpleMail.exe', 'SimpleMailAgent.exe'):
    binary = (args.directory / name).read_bytes()
    assert binary[:2] == b'MZ', name
    pe = struct.unpack_from('<I', binary, 0x3c)[0]
    assert binary[pe:pe+4] == b'PE\0\0', name
    machine = struct.unpack_from('<H', binary, pe+4)[0]
    assert machine == {'x64': 0x8664, 'arm64': 0xaa64}[args.arch], (name, hex(machine))
exe = (args.directory / 'SimpleMailAgent.exe').resolve()
with tempfile.TemporaryDirectory(prefix='simplemail-installed-agent-') as root:
    env = {**os.environ, 'APPDATA': root}
    config_dir = Path(root) / 'SimpleMail'
    config_dir.mkdir()
    from mail_credentials import write_config
    write_config(config_dir / 'config.json', {'accounts': [
        {'id': 'fixture', 'label': 'Packaged QA', 'email': 'qa@example.invalid', 'password': 'fixture-password',
         'imap_host': '127.0.0.1', 'imap_port': 1, 'smtp_host': '127.0.0.1', 'smtp_port': 1}],
        'active_account': 'fixture'})
    def command(*args, input=None):
        result = subprocess.run([str(exe), *args], input=input, capture_output=True,
                                text=True, encoding='utf-8', env=env, timeout=45)
        assert result.returncode == 0, result.stderr
        return result.stdout
    accounts = json.loads(command('accounts'))
    assert accounts[0]['id'] == 'fixture'
    assigned = json.loads(command('assign', 'fixture', '--job', 'Prepare replies for owner review'))
    assert assigned['enabled'] and assigned['mode'] == 'draft_for_review'
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'mailbox_identity'}},
        {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'mailbox_drafts'}},
    ]
    output = command('serve', '--account', 'fixture', input=''.join(json.dumps(item)+'\n' for item in requests))
    replies = [json.loads(line) for line in output.splitlines()]
    assert len(replies) == 4
    assert replies[2]['result']['structuredContent']['address'] == 'qa@example.invalid'
    names = {item['name'] for item in replies[1]['result']['tools']}
    assert 'mailbox_work_next' in names and 'mailbox_send' not in names
    assert replies[3]['result']['structuredContent']['items'] == []
    paused = json.loads(command('pause', 'fixture'))
    assert not paused['enabled']
    result = json.loads(command('run', '--account', 'fixture', '--endpoint',
        'http://127.0.0.1:1/v1/responses', '--model', 'fixture-model', '--key-env', 'SIMPLEMAIL_PACKAGED_QA_KEY', '--once'))
    assert result['status'] == 'paused'
    from model_control import ModelControl
    control = ModelControl(config_dir / 'agent' / 'mailbox.sqlite3')
    control.save('fixture', 'http://127.0.0.1:1/v1/responses', 'fixture-model', 'responses')
    control.set_profile('fixture', True, 'Prepare replies for owner review')
    token = control.claim('fixture')
    process = subprocess.Popen([str(exe), 'managed', '--account', 'fixture', '--token', token],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and control.state('fixture')['status'] not in ('unavailable', 'failed', 'stopped'):
            time.sleep(.2)
        assert control.state('fixture')['status'] == 'unavailable', control.state('fixture')
    finally:
        control.set_profile('fixture', False, 'Prepare replies for owner review')
        control.stop('fixture')
        process.wait(timeout=15)
    assert process.returncode == 0 and control.state('fixture')['status'] == 'stopped'
    print(json.dumps({'packaged_agent': True, 'architecture': args.arch, 'stdio_protocol': True, 'identity_scoped': True,
        'draft_store': True, 'pause': True, 'model_worker_imports': True,
        'managed_worker_start_pause': True, 'live_mail_or_provider_requests': False, 'passed': True}))

