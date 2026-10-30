import base64
import io
import socket
import unittest
from unittest.mock import Mock, patch

import mail_images
from mailapp import Api


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7xkAAAAASUVORK5CYII=")


def address(ip):
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))


def response(payload=PNG, mime="image/png", status=200, headers=None):
    result = Mock(status=status)
    values = {"Content-Type": mime, "Content-Length": str(len(payload)), **(headers or {})}
    result.getheader.side_effect = lambda name, default=None: values.get(name, default)
    result.read1.side_effect = io.BytesIO(payload).read1
    return result


class ImageTests(unittest.TestCase):
    @patch("mail_images.socket.getaddrinfo", return_value=[address("93.184.216.34")])
    @patch("mail_images._HTTPSConnection")
    def test_same_origin_image_is_embedded_without_cookies_or_referrer(self, connection, dns):
        conn = connection.return_value
        conn.getresponse.return_value = response(headers={"Cross-Origin-Resource-Policy": "same-origin"})
        url = "https://example.com/logo.png?image=1&v=2"
        result = mail_images.load_remote_images('<img src="https://example.com/logo.png?image=1&amp;v=2">')
        self.assertEqual(result, {"images": {url: "data:image/png;base64," + base64.b64encode(PNG).decode()}, "failed": 0})
        connection.assert_called_once_with("example.com", "93.184.216.34", unittest.mock.ANY)
        headers = conn.request.call_args.kwargs["headers"]
        self.assertFalse({"Cookie", "Authorization", "Referer", "Origin"} & headers.keys())
        self.assertEqual(conn.request.call_args.args[:2], ("GET", "/logo.png?image=1&v=2"))
        conn.close.assert_called_once()

    @patch("mail_images.socket.getaddrinfo")
    @patch("mail_images._HTTPSConnection")
    def test_private_addresses_and_mixed_dns_answers_are_blocked(self, connection, dns):
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "::ffff:127.0.0.1", "224.0.0.1"):
            with self.subTest(ip=ip):
                dns.return_value = [address("93.184.216.34"), address(ip)]
                self.assertEqual(mail_images.load_remote_images('<img src="https://example.com/pic">')["failed"], 1)
        connection.assert_not_called()

    @patch("mail_images.socket.getaddrinfo", side_effect=[[address("93.184.216.34")], [address("127.0.0.1")]])
    @patch("mail_images._HTTPSConnection")
    def test_redirect_to_private_network_is_blocked(self, connection, dns):
        connection.return_value.getresponse.return_value = response(status=302, headers={"Location": "https://internal.example/pic"})
        result = mail_images.load_remote_images('<img src="https://example.com/pic">')
        self.assertEqual(result, {"images": {}, "failed": 1})
        self.assertEqual(connection.call_count, 1)

    @patch("mail_images.socket.getaddrinfo", return_value=[address("93.184.216.34")])
    @patch("mail_images._HTTPSConnection")
    def test_redirects_are_bounded_and_each_host_is_checked(self, connection, dns):
        connection.return_value.getresponse.return_value = response(status=302, headers={"Location": "https://redirect.example/pic"})
        self.assertEqual(mail_images.load_remote_images('<img src="https://example.com/pic">')["failed"], 1)
        self.assertEqual(connection.call_count, 4)
        self.assertEqual(dns.call_count, 4)

    @patch("mail_images.socket.getaddrinfo", return_value=[address("93.184.216.34")])
    @patch("mail_images._HTTPSConnection")
    def test_non_images_large_images_and_failed_downloads_are_not_embedded(self, connection, dns):
        for fixture in (response(b"<svg><script/></svg>", "image/svg+xml"),
                        response(b"<html>Oops</html>", "image/png"), response(status=403),
                        response(headers={"Content-Length": str(mail_images.MAX_IMAGE_BYTES + 1)}),
                        response(PNG + b"x" * mail_images.MAX_IMAGE_BYTES, headers={"Content-Length": ""})):
            with self.subTest(fixture=fixture):
                connection.return_value.getresponse.return_value = fixture
                self.assertEqual(mail_images.load_remote_images('<img src="https://example.com/pic">'), {"images": {}, "failed": 1})

    @patch("mail_images._fetch", return_value=(PNG, "image/png"))
    def test_only_image_sources_are_fetched_once_with_limits(self, fetch):
        html = '<img src="//example.com/pic"><img src="https://example.com/pic"><img src>' + '<a href="https://example.com/link">Link</a><div style="background:url(https://example.com/css)"></div>'
        result = mail_images.load_remote_images(html)
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["failed"], 0)
        fetch.assert_called_once_with("https://example.com/pic", unittest.mock.ANY, mail_images.MAX_IMAGE_BYTES)
        fetch.reset_mock()
        with patch.object(mail_images, "MAX_IMAGES", 2), patch.object(mail_images, "MAX_TOTAL_BYTES", len(PNG)):
            result = mail_images.load_remote_images(''.join('<img src="https://example.com/%s">' % n for n in range(4)))
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["failed"], 3)
        self.assertEqual(fetch.call_count, 1)

    @patch("mail_images.socket.getaddrinfo")
    def test_unsafe_urls_are_rejected_before_dns(self, dns):
        for url in ("file:///secret", "ftp://example.com/pic", "https://user:pass@example.com/pic", "https://example.com:444/pic", "https://example.com/pic\r\nX-Evil: yes"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                mail_images._destination(url)
        dns.assert_not_called()

    @patch("mail_images._fetch")
    def test_expired_deadline_does_not_start_a_download(self, fetch):
        with patch.object(mail_images, "MAX_SECONDS", -1):
            self.assertEqual(mail_images.load_remote_images('<img src="https://example.com/pic">')["failed"], 1)
        fetch.assert_not_called()

    def test_api_resolves_images_from_the_selected_message(self):
        api = Api(Mock())
        with patch.object(api, "get_message", return_value={"html": "<p>Selected message</p>"}) as read, patch("mail_images.load_remote_images", return_value={"images": {}, "failed": 0}) as load:
            api.load_message_images("cloud:one", "Inbox", "123", "validity")
        read.assert_called_once_with("cloud:one", "Inbox", "123", "validity")
        load.assert_called_once_with("<p>Selected message</p>")



class Cfg:
    def __init__(self, accounts):
        import mailapp
        self.data = {"accounts": [mailapp.normalize_account(a) for a in accounts], "active_account": ""}
        self.saves = 0

    def accounts(self):
        return self.data["accounts"]

    def account(self, account_id):
        return next(a for a in self.accounts() if a["id"] == account_id)

    def save(self):
        self.saves += 1


LIVE = {"id": "adam", "email": "adam@live.co.uk", "auth": "microsoft", "password": "token",
        "imap_host": "outlook.office365.com", "smtp_host": "smtp-mail.outlook.com", "trusted": ["distrokid.com"]}


class TrustedImageTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Cfg([{**LIVE, "trusted": list(LIVE["trusted"])}])
        self.api = Api(self.cfg)
        known = patch("mailapp.known_senders", return_value={"sarah@sky.com"})
        self.known = known.start()
        self.addCleanup(known.stop)

    def check(self, sender, folder="inbox"):
        return self.api.message_images_trusted("adam", sender, folder)

    def test_trusted_senders_and_people_written_to_show_pictures(self):
        self.assertTrue(self.check('"Zayn (DistroKid)" <support@distrokid.com>')["auto"])   # a trusted site
        self.assertTrue(self.check("Sarah <Sarah@Sky.com>")["auto"])                         # written to
        self.assertTrue(self.check("Adam <adam@live.co.uk>")["auto"])                        # yourself

    def test_anyone_else_asks_and_can_be_trusted(self):
        result = self.check("OpenAI <noreply@email.openai.com>")
        self.assertEqual(result, {"auto": False, "address": "noreply@email.openai.com", "can_trust": True})

    def test_junk_and_cloud_mailboxes_always_ask(self):
        self.assertFalse(self.check("support@distrokid.com", "junk")["auto"])
        cloud = self.api.message_images_trusted("cloud:hello", "support@distrokid.com", "inbox")
        self.assertEqual((cloud["auto"], cloud["can_trust"]), (False, False))

    def test_offline_sent_folder_just_asks(self):
        self.known.side_effect = OSError("offline")
        self.assertFalse(self.check("sarah@sky.com")["auto"])

    def test_always_for_this_sender_trusts_the_address_once(self):
        self.assertEqual(self.api.trust_sender_images("adam", "OpenAI <NoReply@email.openai.com>"),
                         {"address": "noreply@email.openai.com"})
        self.api.trust_sender_images("adam", "noreply@email.openai.com")
        self.assertEqual(self.cfg.accounts()[0]["trusted"], ["distrokid.com", "noreply@email.openai.com"])
        self.assertEqual(self.cfg.saves, 1)
        self.assertTrue(self.check("noreply@email.openai.com")["auto"])
        with self.assertRaises(ValueError):
            self.api.trust_sender_images("adam", "no address here")



class KnownSendersTests(unittest.TestCase):
    def test_lookups_at_the_same_time_read_sent_once(self):
        import threading, time, mailapp
        mailapp._KNOWN.pop("solo", None)
        reads = []

        def slow(imap, folder):
            reads.append(folder)
            time.sleep(0.2)
            return {"sarah@sky.com"}
        imap = Mock()
        with patch("mailapp.connect_imap", return_value=(imap, ["Sent"], None)),              patch("mailapp.pick_sent_folder", return_value="Sent"), patch("mail_screen.known_senders", side_effect=slow):
            results = []
            threads = [threading.Thread(target=lambda: results.append(mailapp.known_senders({"id": "solo"})))
                       for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(reads, ["Sent"])
        self.assertEqual(results, [{"sarah@sky.com"}] * 3)
        mailapp._KNOWN.pop("solo", None)


if __name__ == "__main__":
    unittest.main()
