"""Check release architecture and console behavior without real credentials/network."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import argparse
import struct

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
    (config_dir / 'config.json').write_text(json.dumps({'accounts': [
        {'id': 'fixture', 'label': 'Packaged QA', 'email': 'qa@example.invalid', 'password': ''}],
        'active_account': 'fixture'}), encoding='utf-8')
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
    print(json.dumps({'packaged_agent': True, 'architecture': args.arch, 'stdio_protocol': True, 'identity_scoped': True,
        'draft_store': True, 'pause': True, 'model_worker_imports': True,
        'live_mail_or_provider_requests': False, 'passed': True}))

