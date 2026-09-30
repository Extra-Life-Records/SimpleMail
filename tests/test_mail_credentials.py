import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import mailapp
from agent_mcp import LiveConfig
from mail_credentials import unlock_account, seal_account, write_config

class CredentialTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.path=self.root/'config.json'
  for name,value in [('CONFIG_DIR',self.root),('CONFIG_FILE',self.path)]:
   p=patch.object(mailapp,name,value);p.start();self.addCleanup(p.stop)
  self.account=mailapp.normalize_account({'id':'one','email':'owner@example.invalid','password':'mail-secret','smtp_password':'smtp-secret'})
 def config(self):
  self.path.write_text(json.dumps({'accounts':[self.account],'active_account':'one'}),encoding='utf-8')
  return mailapp.Config()
 def test_plaintext_migration_desktop_and_live_agent(self):
  cfg=self.config();text=self.path.read_text();self.assertNotIn('mail-secret',text);self.assertNotIn('smtp-secret',text)
  self.assertEqual(cfg.account('one')['password'],'mail-secret')
  self.assertEqual(LiveConfig(self.path).account('one')['smtp_password'],'smtp-secret')
  self.assertEqual(mailapp.Config().account('one')['password'],'mail-secret')
 def test_no_passwords_or_ciphertext_in_public_settings(self):
  api=mailapp.Api(self.config());public=api.get_config();text=json.dumps(public)
  self.assertNotIn('mail-secret',text);self.assertNotIn('smtp-secret',text);self.assertNotIn('windows_dpapi',text)
  self.assertTrue(public['accounts'][0]['has_password']);self.assertEqual(public['accounts'][0]['password'],'')
 def test_blank_saved_fields_keep_secrets_and_explicit_smtp_clear_uses_mailbox(self):
  api=mailapp.Api(self.config());public=api.get_config();api.save_config(public)
  self.assertEqual(api.cfg.account('one')['password'],'mail-secret');self.assertEqual(api.cfg.account('one')['smtp_password'],'smtp-secret')
  public['accounts'][0]['clear_smtp_password']=True;api.save_config(public)
  self.assertEqual(mailapp.smtp_credentials(api.cfg.account('one'))[1],'mail-secret')
  self.assertEqual(mailapp.Config().account('one')['smtp_password'],'')
 def test_replace_secret_and_connection_change_require_new_password(self):
  api=mailapp.Api(self.config());public=api.get_config();public['accounts'][0]['imap_host']='changed.invalid'
  with self.assertRaises(ValueError):api.save_config(public)
  public['accounts'][0]['password']='replacement';api.save_config(public)
  self.assertEqual(mailapp.Config().account('one')['password'],'replacement')
  self.assertNotIn('replacement',self.path.read_text())
 def test_test_connection_uses_saved_credentials_without_frontend_secret(self):
  api=mailapp.Api(self.config());raw=api.get_config()['accounts'][0]
  with patch.object(mailapp,'check_connection',return_value=[(True,'Connected')]) as check:
   self.assertTrue(api.test_connection(raw)['ok']);self.assertEqual(check.call_args.args[0]['password'],'mail-secret')
 def test_failed_unlock_preserves_ciphertext_until_owner_replaces_it(self):
  original={'password':{'windows_dpapi':'unavailable-cipher'},'smtp_password':''}
  unlocked=unlock_account(original,lambda *args:(_ for _ in ()).throw(ValueError()))
  self.assertEqual(unlocked['password'],'');self.assertEqual(seal_account(unlocked)['password'],original['password'])
  unlocked['password']='new';sealed=seal_account(unlocked,lambda value:'protected')
  self.assertEqual(sealed['password'],{'windows_dpapi':'protected'});self.assertNotIn('_locked_credentials',sealed)
 def test_protection_or_replace_failure_never_truncates_original_config(self):
  self.path.write_text('original',encoding='utf-8')
  with self.assertRaises(ValueError):write_config(self.path,{'accounts':[self.account]},lambda value:(_ for _ in ()).throw(ValueError()))
  self.assertEqual(self.path.read_text(),'original')
  with patch('mail_credentials.os.replace',side_effect=OSError('Locked')):
   with self.assertRaises(OSError):write_config(self.path,{'accounts':[self.account]})
  self.assertEqual(self.path.read_text(),'original');self.assertEqual(list(self.root.glob('.config-*')),[])
 def test_legacy_single_account_migrates_without_plaintext_copy(self):
  self.path.write_text(json.dumps({'email':'legacy@example.invalid','password':'old-secret'}),encoding='utf-8')
  cfg=mailapp.Config();self.assertEqual(cfg.accounts()[0]['password'],'old-secret')
  self.assertNotIn('old-secret',self.path.read_text());self.assertNotIn('password',json.loads(self.path.read_text()))
 def test_failed_save_restores_backend_credentials(self):
  api=mailapp.Api(self.config());public=api.get_config();public['accounts'][0]['password']='replacement'
  with patch.object(api.cfg,'save',side_effect=OSError('Locked')):
   with self.assertRaises(OSError):api.save_config(public)
  self.assertEqual(api.cfg.account('one')['password'],'mail-secret')
  self.assertEqual(mailapp.Config().account('one')['password'],'mail-secret')
 def test_unavailable_password_survives_settings_save_and_can_be_replaced(self):
  raw={**self.account,'password':{'windows_dpapi':'invalid-cipher'},'smtp_password':''}
  self.path.write_text(json.dumps({'accounts':[raw]}),encoding='utf-8')
  api=mailapp.Api(mailapp.Config());public=api.get_config()
  self.assertTrue(public['accounts'][0]['credential_error']);self.assertTrue(public['accounts'][0]['has_password'])
  api.save_config(public);self.assertEqual(json.loads(self.path.read_text())['accounts'][0]['password'],raw['password'])
  public['accounts'][0]['password']='replacement';api.save_config(public)
  self.assertEqual(mailapp.Config().account('one')['password'],'replacement')

if __name__=='__main__':unittest.main()
