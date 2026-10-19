"""Operator-configured exact recipients; never derive mailbox ownership from headers.

The original five storage identities are immutable. Additional identities live in
the mail-* namespace, separate from IMAP account IDs and administrative records.
An empty registry retains the deployed pilot behaviour.
"""
import json
import os
import re

RESERVED = ('one', 'two', 'three', 'four', 'five')
NAME = re.compile(r'mail-[a-z0-9][a-z0-9-]{0,39}')
ADDRESS = re.compile(r'[a-z0-9][a-z0-9.!#$%&\x27*+/=?^_`{|}~-]{0,63}@[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\.[a-z]{2,63}')


def address(value):
    if not isinstance(value, str) or len(value) > 254 or not ADDRESS.fullmatch(value):
        raise ValueError('Use an exact lowercase email address')
    if '..' in value or value.split('@')[0].endswith('.'):
        raise ValueError('Invalid email address')
    return value


def registry():
    configured = json.loads(os.environ.get('MAILBOX_REGISTRY', '{}'))
    if not isinstance(configured, dict) or len(configured) > 25:
        raise ValueError('Invalid mailbox registry')
    result = {name: {'address': address(name + '@' + os.environ['DOMAIN']),
                     'recipients': [address(name + '@' + os.environ['RECEIVE_DOMAIN'])]}
              for name in RESERVED}
    for name, spec in configured.items():
        if name not in RESERVED and not NAME.fullmatch(name):
            raise ValueError('Additional mailbox IDs must use the mail- namespace')
        if not isinstance(spec, dict) or set(spec) - {'address', 'aliases'}:
            raise ValueError('Unknown mailbox registry field')
        if name in RESERVED:
            if 'address' in spec and spec['address'] != result[name]['address']:
                raise ValueError('Reserved mailbox identities cannot change')
            box = result[name]
        else:
            box = {'address': address(spec.get('address')), 'recipients': []}
            result[name] = box
        aliases = spec.get('aliases', [])
        if not isinstance(aliases, list) or len(aliases) > 20:
            raise ValueError('Invalid exact alias list')
        box['recipients'] = list(dict.fromkeys([*box['recipients'], box['address'],
                                                *(address(value) for value in aliases)]))
    routes = {}
    for name, spec in result.items():
        for recipient in spec['recipients']:
            if recipient in routes and routes[recipient] != name:
                raise ValueError('Recipient belongs to more than one mailbox')
            routes[recipient] = name
    return result


def routes():
    return {recipient: name for name, spec in registry().items() for recipient in spec['recipients']}


def resolve_recipient(recipient, configured_routes):
    """Resolve an envelope recipient without widening the approved base/domain.

    SES matches plus labels for configured address conditions. Explicit aliases
    win, including aliases assigned to a disabled mailbox; mailbox state is
    checked by the receiver after routing and must never cause fallback.
    """
    if recipient in configured_routes:
        return configured_routes[recipient]
    local, separator, domain = recipient.partition('@')
    base, plus, label = local.partition('+')
    if separator and plus and label:
        return configured_routes.get(base + '@' + domain)
    return None


def folder(value):
    # Folder names are data, never DynamoDB/S3 keys or URL path components.
    return (isinstance(value, str) and 0 < len(value) <= 500 and
            not any(ord(char) < 32 or ord(char) == 127 for char in value))
