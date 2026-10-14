import base64
import sys
import unittest
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mail_inline_images import set_message_body

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
URI = 'data:image/png;base64,' + base64.b64encode(PNG).decode()


class InlineImageTests(unittest.TestCase):
    def test_related_images_survive_wire_encoding_with_attachments(self):
        msg = EmailMessage()
        html = '<div>Adam</div>\n<img alt="Brand > logo" width="64" src="' + URI + '"><img src="' + URI + '"/>'
        set_message_body(msg, 'Adam', html)
        msg.add_attachment(b'file', maintype='text', subtype='plain', filename='brief.txt')
        wire = message_from_bytes(msg.as_bytes(), policy=default)
        self.assertEqual(wire.get_body(preferencelist=('plain',)).get_content().strip(), 'Adam')
        images = [p for p in wire.walk() if p.get_content_maintype() == 'image']
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].get_payload(decode=True), PNG)
        self.assertEqual(images[0].get_content_disposition(), 'inline')
        body = wire.get_body(preferencelist=('html',)).get_content()
        self.assertEqual(body.count('cid:' + images[0]['Content-ID'][1:-1]), 2)
        self.assertIn('width="64"', body)
        self.assertNotIn('data:image', body)
        self.assertEqual(next(wire.iter_attachments()).get_filename(), 'brief.txt')

    def test_invalid_or_oversized_images_fail_before_send(self):
        for src in ['data:image/svg+xml;base64,YQ==', 'data:image/png;base64,a',
                    'data:image/png;base64,' + 'A' * (7 * 1024 * 1024)]:
            with self.subTest(src=src[:40]), self.assertRaises(ValueError):
                set_message_body(EmailMessage(), '', '<img src="' + src + '">')

    def test_plain_and_remote_images_unchanged(self):
        msg = EmailMessage()
        set_message_body(msg, 'Plain')
        self.assertEqual(msg.get_content_type(), 'text/plain')
        msg = EmailMessage()
        html = '<img src="https://example.invalid/logo.png"><p>data:image/png;base64,abc</p>'
        set_message_body(msg, 'Plain', html)
        self.assertEqual(msg.get_body(preferencelist=('html',)).get_content().strip(), html)

    def test_smtp_and_server_drafts_use_related_images(self):
        import mailapp
        account = {'email': 'adam@example.invalid', 'smtp_host': 'smtp.invalid', 'smtp_port': 587,
                   'smtp_starttls': True}
        with patch.object(mailapp, 'smtp_credentials', return_value=('user', 'secret')), patch.object(mailapp.smtplib, 'SMTP') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            mailapp.send_message(account, 'recipient@example.invalid', 'Test', 'Adam', '<img src="' + URI + '">', save_sent=False)
            msg = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            self.assertEqual(len([p for p in msg.walk() if p.get_content_maintype() == 'image']), 1)
        imap = MagicMock()
        with patch.object(mailapp, 'connect_imap', return_value=(imap, ['Drafts'], None)):
            mailapp.save_draft_message(account, '', '', 'Adam', '<img src="' + URI + '">')
        msg = message_from_bytes(imap.append.call_args.args[3], policy=default)
        self.assertEqual(len([p for p in msg.walk() if p.get_content_maintype() == 'image']), 1)


if __name__ == '__main__':
    unittest.main()
