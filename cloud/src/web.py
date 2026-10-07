"""Serve only bundled public assets; no mail or authentication data is cached."""
import json
import os
from pathlib import Path


def serve(event, context):
    name = event['rawPath'].removeprefix('/ui/')
    types = {'index.html': 'text/html', 'app.js': 'text/javascript', 'style.css': 'text/css'}
    if name == 'config.json':
        body = json.dumps({'client_id': os.environ['CLIENT_ID'], 'auth_url': os.environ['AUTH_URL']})
        content_type = 'application/json'
    elif name in types:
        body = (Path(__file__).parent / 'static' / name).read_text(encoding='utf-8')
        content_type = types[name]
    else:
        return {'statusCode': 404, 'body': 'Not found'}
    return {'statusCode': 200, 'body': body, 'headers': {
        'Content-Type': content_type + '; charset=utf-8', 'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
        'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; "
                                  "connect-src 'self' https://*.amazoncognito.com; "
                                  "img-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"}}
