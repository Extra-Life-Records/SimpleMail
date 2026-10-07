"""Fetch email pictures only after the reader's explicit Load images action.

Return bounded raster images as data URLs so remote embedding policies do not
break the sandboxed reader. No cookies, authentication or referrer are sent.
"""
import base64
import http.client
import ipaddress
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


MAX_IMAGES = 40
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
MAX_SECONDS = 30
MIME_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp"}


class _Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag != "img":
            return
        raw = (dict(attrs).get("src") or "").strip()
        url = "https:" + raw if raw.startswith("//") else raw
        try:
            if urlsplit(url).scheme.lower() in ("http", "https") and url not in self.urls:
                self.urls.append(url)
        except ValueError:
            pass


class _HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout)
        self._address = address

    def connect(self):
        # Pin the validated address while retaining hostname/certificate checks.
        sock = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _destination(url):
    if len(url) > 8192 or any(ord(char) < 33 or ord(char) == 127 for char in url):
        raise ValueError("Invalid image URL")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None:
        raise ValueError("Only public HTTP images are supported")
    port = 443 if parts.scheme == "https" else 80
    if parts.port not in (None, port):
        raise ValueError("Unsupported image port")
    host = parts.hostname.encode("idna").decode("ascii")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global or
                            ipaddress.ip_address(row[4][0]).is_multicast for row in addresses):
        raise ValueError("Private network images are blocked")
    return parts, host, addresses[0][4][0], port


def _raster(payload, mime):
    return {
        "image/png": payload.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": payload.startswith(b"\xff\xd8\xff"),
        "image/gif": payload.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": payload.startswith(b"RIFF") and payload[8:12] == b"WEBP",
        "image/avif": payload[4:8] == b"ftyp" and payload[8:12] in (b"avif", b"avis"),
        "image/bmp": payload.startswith(b"BM"),
    }.get(mime, False)


def _fetch(url, deadline, limit):
    for redirect in range(4):
        parts, host, address, port = _destination(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Image loading timed out")
        timeout = min(10, remaining)
        conn = (_HTTPSConnection(host, address, timeout) if port == 443 else
                http.client.HTTPConnection(address, port, timeout=timeout))
        try:
            target = parts.path or "/"
            if parts.query:
                target += "?" + parts.query
            conn.request("GET", target, headers={
                "Host": "[" + host + "]" if ":" in host else host,
                "User-Agent": "Mozilla/5.0 SimpleMail",
                "Accept": "image/png,image/jpeg,image/gif,image/webp,image/avif,image/bmp",
                "Accept-Encoding": "identity",
            })
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location or redirect == 3:
                    raise ValueError("Image redirect failed")
                url = urljoin(url, location)
                continue  # The destination is checked again on every redirect.
            mime = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            if response.status != 200 or mime not in MIME_TYPES:
                raise ValueError("Image is unavailable")
            length = response.getheader("Content-Length")
            if length and int(length) > limit:
                raise ValueError("Image is too large")
            payload = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Image loading timed out")
                if conn.sock:
                    conn.sock.settimeout(min(10, remaining))
                chunk = response.read1(min(65536, limit + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > limit:
                    raise ValueError("Image is too large")
            if not _raster(payload, mime):
                raise ValueError("Unsupported image content")
            return bytes(payload), mime
        finally:
            conn.close()


def load_remote_images(html):
    parser = _Images()
    parser.feed(str(html))
    images, total = {}, 0
    deadline = time.monotonic() + MAX_SECONDS
    for url in parser.urls[:MAX_IMAGES]:
        if time.monotonic() >= deadline or total >= MAX_TOTAL_BYTES:
            break
        try:
            payload, mime = _fetch(url, deadline, min(MAX_IMAGE_BYTES, MAX_TOTAL_BYTES - total))
            total += len(payload)
            images[url] = "data:" + mime + ";base64," + base64.b64encode(payload).decode("ascii")
        except (OSError, ValueError, http.client.HTTPException):
            # Do not expose remote URLs, tokens or server responses in errors.
            continue
    return {"images": images, "failed": len(parser.urls) - len(images)}
