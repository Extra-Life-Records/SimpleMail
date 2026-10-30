"""Group a UID FETCH response by message, wherever the server puts the UID.

Fasthosts answers `12 (UID 345 BODY[...] {n}`; Outlook.com answers
`12 (BODY[...] {n}` ... ` UID 345)` - the UID only arrives after the payloads.
"""
import re

_START = re.compile(rb"^\s*(\d+) \(")
_UID = re.compile(rb"UID (\d+)")
_FLAGS = re.compile(rb"FLAGS \(([^)]*)\)")


def fetch_groups(fdata):
    """[(uid or None, flags or None, [(desc_text, payload), ...])] in server order."""
    groups = []
    for item in fdata or []:
        desc = item[0] if isinstance(item, tuple) else item
        if not isinstance(desc, bytes):
            continue
        if _START.match(desc) or not groups:
            groups.append([None, None, []])
        group = groups[-1]
        uid = _UID.search(desc)
        if uid and group[0] is None:
            group[0] = int(uid.group(1))
        flags = _FLAGS.search(desc)
        if flags and group[1] is None:
            group[1] = flags.group(1).decode("utf-8", "replace")
        if isinstance(item, tuple):
            group[2].append((desc.decode("utf-8", "replace"), item[1]))
    return [tuple(g) for g in groups]
