import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('routing',Path(__file__).parents[1]/'cloudflare_routes.py')
routing=importlib.util.module_from_spec(spec);spec.loader.exec_module(routing)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.snapshot=Path(self.temp.name)/'before.json'
        self.hello={'id':'hello-existing','enabled':True,'matchers':[{'field':'to','type':'literal','value':'hello@playloudr.com'}], 'actions':[{'type':'forward','value':['playloudr@extraliferecords.com']}]}
        self.rules=[self.hello];self.writes=[];self.verified=True
        self.snapshot.write_text(json.dumps({'zone':'zone','rules':self.rules,'catchall':{},'mx':[]}))
    def api(self,method,path,data=None):
        if method=='POST':self.writes.append(data);self.rules.append({**data,'id':'created-'+str(len(self.writes))});return {'success':True,'result':data}
        if path=='/zones/zone':return {'result':{'name':'playloudr.com','account':{'id':'account'}}}
        if 'catch_all' in path:return {'result':{}}
        raise AssertionError(path)
    def pages(self,path):
        if path.endswith('/rules'):return list(self.rules)
        if 'dns_records' in path:return []
        return [{'email':n+'@inbox.playloudr.com','verified':'2026-10-07' if self.verified else None} for n in routing.NAMES]
    def run_command(self):
        with patch.object(routing,'api',side_effect=self.api),patch.object(routing,'pages',side_effect=self.pages),patch('sys.argv',['routes','enable','--zone','zone','--snapshot',str(self.snapshot)]),contextlib.redirect_stdout(io.StringIO()):routing.main()
    def test_adds_only_five_verified_exact_routes_and_preserves_hello(self):
        self.run_command();self.assertEqual(len(self.writes),5);self.assertEqual(self.rules[0],self.hello)
        self.assertEqual({r['matchers'][0]['value'] for r in self.writes},{n+'@playloudr.com' for n in routing.NAMES})
        self.run_command();self.assertEqual(len(self.writes),5)
    def test_unverified_destination_blocks_all_writes(self):
        self.verified=False
        with self.assertRaises(ValueError):self.run_command()
        self.assertEqual(self.writes,[])
    def test_changed_existing_route_blocks_all_writes(self):
        self.rules=[]
        with self.assertRaises(ValueError):self.run_command()
        self.assertEqual(self.writes,[])
    def test_conflicting_placeholder_is_not_overwritten(self):
        self.rules.append({'enabled':True,'matchers':[{'value':'one@playloudr.com'}],'actions':[{'type':'forward','value':['other@example.com']}]})
        with self.assertRaises(ValueError):self.run_command()
        self.assertEqual(self.writes,[])


if __name__=='__main__':unittest.main()
