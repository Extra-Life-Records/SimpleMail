"""Snapshot-first exact rules. Never modifies MX, catch-all or existing routes.

Set CLOUDFLARE_API_TOKEN in the process environment using your credential manager.
The token needs Email Routing read/edit for this account and playloudr.com zone.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

NAMES=('one','two','three','four','five')


def api(method,path,data=None):
    req=Request('https://api.cloudflare.com/client/v4'+path,method=method,
                data=json.dumps(data).encode() if data is not None else None,
                headers={'Authorization':'Bearer '+os.environ['CLOUDFLARE_API_TOKEN'],'Content-Type':'application/json'})
    with urlopen(req,timeout=30) as response:body=json.load(response)
    if not body.get('success'):raise RuntimeError('Cloudflare rejected request; no further changes attempted')
    return body


def pages(path):
    result=[];page=1
    while True:
        body=api('GET',path+('?' if '?' not in path else '&')+f'page={page}&per_page=50')
        result.extend(body['result'])
        if page>=body.get('result_info',{}).get('total_pages',1):return result
        page+=1


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['snapshot','destinations','enable']);p.add_argument('--zone',required=True);p.add_argument('--snapshot',type=Path,required=True)
    args=p.parse_args();zone=api('GET','/zones/'+args.zone)['result']
    if zone['name']!='playloudr.com':raise ValueError('Refusing to modify another domain')
    root='/zones/'+args.zone+'/email/routing';account=zone['account']['id']
    rules=pages(root+'/rules');catchall=api('GET',root+'/rules/catch_all')['result']
    dns=pages('/zones/'+args.zone+'/dns_records?type=MX')
    if args.command=='snapshot':
        # Exclusive creation avoids overwriting the pre-rollout evidence.
        with args.snapshot.open('x',encoding='utf-8') as f:json.dump({'captured':dt.datetime.now(dt.timezone.utc).isoformat(),'zone':args.zone,'rules':rules,'catchall':catchall,'mx':dns},f,indent=2)
        print('Routing and MX snapshot saved.');return
    snapshot=json.loads(args.snapshot.read_text(encoding='utf-8'))
    if snapshot['zone']!=args.zone:raise ValueError('Snapshot belongs to another zone')
    if snapshot['catchall']!=catchall or snapshot['mx']!=dns:raise ValueError('MX or catch-all changed since snapshot; review before continuing')
    destinations=pages('/accounts/'+account+'/email/routing/addresses')
    by_email={d['email']:d for d in destinations}
    if args.command=='destinations':
        for name in NAMES:
            address=name+'@inbox.playloudr.com'
            if address not in by_email:api('POST','/accounts/'+account+'/email/routing/addresses',{'email':address})
        print('Destination verification requested. Verify all five through the private inbox before enabling routes.');return
    for name in NAMES:
        address=name+'@inbox.playloudr.com'
        if not by_email.get(address,{}).get('verified'):raise ValueError('Destination not verified: '+address)
    for original in snapshot['rules']:
        if original not in rules:raise ValueError('An existing routing rule changed; review before continuing')
    for name in NAMES:
        public=name+'@playloudr.com';destination=name+'@inbox.playloudr.com'
        matches=[r for r in rules if any(m.get('value','').lower()==public for m in r.get('matchers',[]))]
        if matches:
            if len(matches)!=1 or not matches[0].get('enabled') or matches[0]['actions']!=[{'type':'forward','value':[destination]}]:raise ValueError('Conflicting existing rule for '+public)
            continue
        api('POST',root+'/rules',{'name':'Reserved mailbox '+name,'enabled':True,
            'matchers':[{'type':'literal','field':'to','value':public}],
            'actions':[{'type':'forward','value':[destination]}]})
    final=pages(root+'/rules')
    for name in NAMES:
        if not any(r.get('enabled') and any(m.get('value')==name+'@playloudr.com' for m in r.get('matchers',[])) for r in final):raise RuntimeError('Final verification failed')
    print('Five exact forwarding rules verified. End-to-end delivery tests still required.')


if __name__=='__main__':main()
