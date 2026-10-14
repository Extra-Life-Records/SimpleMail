"""Package locally embedded signature images as related MIME parts."""
import base64
import binascii
import re
from email.utils import make_msgid
from html.parser import HTMLParser


def set_message_body(message, text, html=None):
    message.set_content(text or "")
    if not html:
        return
    replacements, images, seen = [], [], {}
    lines = html.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    class Images(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag != "img":
                return
            source = dict(attrs).get("src", "")
            if not source.lower().startswith("data:"):
                return
            match = re.fullmatch(r"data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/=\s]+)", source, re.I)
            if not match:
                raise ValueError("Use a PNG, JPEG, GIF or WebP signature image")
            if source not in seen:
                if len(source) > 7 * 1024 * 1024:
                    raise ValueError("Signature images must total less than 5 MB")
                try:
                    data = base64.b64decode(re.sub(r"\s", "", match[2]), validate=True)
                except (ValueError, binascii.Error) as error:
                    raise ValueError("Invalid signature image") from error
                if not data or sum(len(image[2]) for image in images) + len(data) > 5 * 1024 * 1024:
                    raise ValueError("Signature images must total less than 5 MB")
                cid = make_msgid()
                seen[source] = cid
                images.append((cid, match[1].lower(), data))
            raw = self.get_starttag_text()
            line, column = self.getpos()
            start = offsets[line - 1] + column
            replacements.append((start, start + len(raw), raw.replace(source, "cid:" + seen[source][1:-1], 1)))

        handle_startendtag = handle_starttag

    Images(convert_charrefs=False).feed(html)
    for start, end, replacement in reversed(replacements):
        html = html[:start] + replacement + html[end:]
    message.add_alternative(html, subtype="html")
    html_part = message.get_payload()[-1]
    for cid, subtype, data in images:
        html_part.add_related(data, maintype="image", subtype=subtype, cid=cid,
                              disposition="inline")
