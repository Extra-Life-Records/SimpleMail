"""Verify private IMAP backups and import them into isolated native mailboxes.

No SMTP, IMAP mutations, DNS, grant changes or source deletion. Local verification
is the default. AWS writes require --apply, a pinned mailbox generation and a
stack whose human and AI sending parameters both remain false.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'src'))
import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
import mailboxes
import service


def stamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Source INTERNALDATE must include timezone')
    return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def private_file(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError('Archive paths must be relative')
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise ValueError('Archive file missing or outside its private directory')
    return target


def source_key(source_id, folder, validity, uid):
    payload = [source_id, folder, str(validity), str(uid)]
    return hashlib.sha256(json.dumps(payload, separators=(',', ':')).encode()).hexdigest()


def folder_name(folder):
    name = folder['name']
    attrs = {attribute.lower() for attribute in folder.get('attributes', [])}
    if name.upper() == 'INBOX': return 'Inbox'
    for flag, canonical in (('\\sent', 'Sent'), ('\\drafts', 'Drafts'), ('\\junk', 'Junk'), ('\\trash', 'Trash')):
        if flag in attrs: return canonical
    # Special-use flags are authoritative. Fallback handles older Livemail servers.
    fallback = {'sent': 'Sent', 'sent items': 'Sent', 'inbox.sent': 'Sent',
                'drafts': 'Drafts', 'inbox.drafts': 'Drafts', 'junk': 'Junk',
                'spam': 'Junk', 'inbox.junk': 'Junk', 'trash': 'Trash',
                'deleted items': 'Trash', 'inbox.trash': 'Trash'}
    value = fallback.get(name.lower(), name)
    if value == 'All': value = 'Imported/All'
    if not mailboxes.folder(value): raise ValueError('Unsupported folder name')
    return value


def archive(root):
    """Read and fully hash-check the complete snapshot before any AWS writes."""
    root = Path(root).resolve()
    manifest = json.loads(private_file(root, 'mailbox.json').read_text(encoding='utf-8-sig'))
    if manifest.get('schema_version') != 1 or manifest.get('complete') is not True:
        raise ValueError('A complete schema-version-1 mailbox snapshot is required')
    source_id = manifest.get('source_id', '')
    if not re.fullmatch('[a-f0-9]{64}', source_id): raise ValueError('Invalid source identity')
    expected_source = hashlib.sha256((manifest['host'] + '\n' + manifest['username'].lower()).encode()).hexdigest()
    if source_id != expected_source: raise ValueError('Source identity does not match mailbox')
    rows, folder_names, identities, names = [], [], set(), set()
    current_total = 0
    for index in manifest['folders']:
        if index.get('selectable') is False:
            # IMAP hierarchy containers are inventory entries, not mail folders.
            # Keep their names in the source manifest without inventing message data.
            if (index.get('complete') is not True or index.get('message_count') != 0 or
                    index.get('path') or not mailboxes.folder(index.get('name'))):
                raise ValueError('Nonselectable container must be complete and contain no messages')
            continue
        prefix = index['path']
        folder = json.loads(private_file(root, prefix + '/folder.json').read_text(encoding='utf-8-sig'))
        if folder.get('complete') is not True or index.get('complete') is not True:
            raise ValueError('An exported folder is incomplete')
        for field in ('uidvalidity', 'wire_name_b64', 'name'):
            if folder[field] != index[field]: raise ValueError('Folder manifest disagrees with mailbox inventory')
        display = folder_name(folder)
        if display in names: raise ValueError('Two source folders map to one destination; resolve the folder mapping first')
        names.add(display); folder_names.append(display)
        present = set()
        for line in private_file(root, prefix + '/messages.jsonl').read_text(encoding='utf-8-sig').splitlines():
            if not line.strip(): continue
            item = json.loads(line)
            uid = item['uid']; validity = item['uidvalidity']
            if type(uid) is not int or uid < 1 or type(validity) is not int or validity < 1 or validity != folder['uidvalidity']:
                raise ValueError('Invalid source UID or UIDVALIDITY')
            identity = source_key(source_id, folder['wire_name_b64'], validity, uid)
            if identity in identities: raise ValueError('Duplicate source item')
            identities.add(identity)
            path = private_file(root, item['relative_path'])
            raw = path.read_bytes()
            if len(raw) != item['size'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
                raise ValueError('Archive size or SHA256 mismatch')
            if type(item.get('present')) is not bool or not isinstance(item['flags'], list) or any(not isinstance(flag, str) for flag in item['flags']):
                raise ValueError('Invalid presence or flags metadata')
            if item['present']: present.add(uid)
            rows.append({**item, 'id': identity, 'source_id': source_id,
                         'wire_name_b64': folder['wire_name_b64'], 'source_folder': folder['name'],
                         'folder': display, 'received': stamp(item['internal_date']), 'path': path})
        final_uids = folder['final_uids']
        if present != set(final_uids) or len(final_uids) != len(set(final_uids)) or len(present) != folder['message_count'] or len(present) != index['message_count']:
            raise ValueError('Folder UID inventory or count does not reconcile')
        current_total += len(present)
    if current_total != manifest['message_count']:
        raise ValueError('Mailbox totals do not reconcile')
    return manifest, rows, folder_names


def inspect(root):
    manifest, rows, folders = archive(root)
    unsupported = []
    for row in rows:
        if row['size'] > service.MAX_RAW:
            unsupported.append({'id': row['id'], 'reason': 'raw_message_exceeds_native_limit'})
            continue
        parsed = service.parse(row['path'].read_bytes())
        if any(attachment['size'] > service.MAX_RAW for attachment in parsed['attachments']):
            unsupported.append({'id': row['id'], 'reason': 'attachment_exceeds_client_download_limit'})
        if row['folder'] == 'Drafts' and row['present']:
            unsupported.append({'id': row['id'], 'reason': 'source_draft_preserved_as_mime_not_editable_native_draft'})
    return {'source_id': manifest['source_id'], 'verified': True, 'messages': len(rows),
            'current_messages': manifest['message_count'], 'bytes': sum(row['size'] for row in rows),
            'folders': folders, 'nonselectable_containers': [entry['name'] for entry in manifest['folders'] if entry.get('selectable') is False],
            'compatibility_exceptions': unsupported,
            'ready_for_cutover': False}


def provision(table, names, owner, apply=False):
    admin = table.get_item(Key={'pk': 'ADMIN', 'sk': owner}, ConsistentRead=True).get('Item')
    if not admin or not admin.get('enabled'): raise ValueError('Existing enabled owner is required')
    configured = mailboxes.registry(); result = []
    if len(names) != len(set(names)) or any(name in service.NAMES or name not in configured for name in names):
        raise ValueError('Provision only distinct reviewed additional native mailbox IDs')
    for name in names:
        current = table.get_item(Key={'pk': name, 'sk': 'META'}, ConsistentRead=True).get('Item')
        if current:
            if current.get('address') != configured[name]['address']: raise ValueError('Existing mailbox address mismatch')
            result.append({'name': name, 'generation': current['generation'], 'created': False})
            continue
        item = {'pk': name, 'sk': 'META', 'state': 'Reserved', 'generation': uuid.uuid4().hex,
                'generation_started': service.timestamp(), 'address': configured[name]['address']}
        if apply:
            table.put_item(Item=item, ConditionExpression='attribute_not_exists(pk)')
        result.append({'name': name, 'generation': item['generation'] if apply else None, 'created': apply})
    return result


def current_generation(table, name, generation):
    item = service.meta(table, name)
    if item['generation'] != generation or item['state'] != 'Reserved':
        raise ValueError('Import target must remain the pinned owner-only Reserved generation')
    return item


def safe_put(s3, bucket, key, raw, digest):
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=raw, ContentType='message/rfc822',
                      Metadata={'sha256': digest}, IfNoneMatch='*')
    except ClientError as exc:
        if exc.response['Error']['Code'] not in ('PreconditionFailed', '412'): raise
        saved = s3.get_object(Bucket=bucket, Key=key)['Body'].read()
        if hashlib.sha256(saved).hexdigest() != digest:
            raise ValueError('Existing imported raw message differs; no overwrite permitted') from None


def import_archive(table, s3, bucket, name, generation, root, apply=False, limit=None):
    manifest, rows, folders = archive(root)
    item = current_generation(table, name, generation)
    if name in service.NAMES: raise ValueError('History import cannot modify the original five inboxes')
    if manifest['username'].lower() != item['address']:
        raise ValueError('Source login and destination address differ; explicit identity reconciliation required')
    report = {'source_id': manifest['source_id'], 'mailbox': name, 'generation': generation,
              'apply': apply, 'created': 0, 'unchanged': 0, 'metadata_refreshed': 0,
              'conflicts': [], 'total_archive_messages': len(rows), 'limited': limit is not None,
              'ready_for_cutover': False}
    pk = service.partition(item)
    # Register all imported folder names first; never expose another mailbox's folders.
    if apply:
        table.update_item(Key={'pk': name, 'sk': 'META'}, UpdateExpression='SET import_folders=:f',
                          ConditionExpression='generation=:g AND #state=:state',
                          ExpressionAttributeNames={'#state': 'state'},
                          ExpressionAttributeValues={':f': list(dict.fromkeys([*item.get('import_folders', []), *folders])),
                                                     ':g': generation, ':state': 'Reserved'})
    for source in rows[:limit] if limit is not None else rows:
        current_generation(table, name, generation)
        if not source['present']:
            report['conflicts'].append({'id': source['id'], 'reason': 'source_item_no_longer_present_retained_in_backup'})
            continue
        if source['size'] > service.MAX_RAW:
            report['conflicts'].append({'id': source['id'], 'reason': 'raw_message_exceeds_native_limit'})
            continue
        raw = source['path'].read_bytes(); parsed = service.parse(raw)
        key = {'pk': pk, 'sk': 'MSG#' + source['id']}
        object_key = 'imported/' + pk + '/' + source['id'] + '.eml'
        seen = any(flag.lower() == '\\seen' for flag in source['flags'])
        state = {'folder': source['folder'], 'seen': seen, 'flags': source['flags']}
        origin = {field: source[field] for field in ('source_id', 'wire_name_b64', 'source_folder', 'uidvalidity', 'uid', 'sha256', 'size', 'internal_date')}
        old = table.get_item(Key=key, ConsistentRead=True).get('Item')
        if old:
            if old.get('import_source') != origin:
                raise ValueError('Imported identity collision or changed source bytes')
            previous = old.get('import_state', {})
            actual = {field: old.get(field) for field in ('folder', 'seen', 'flags')}
            if actual != previous:
                report['conflicts'].append({'id': source['id'], 'reason': 'destination_changed_since_import'})
                continue
            if apply:
                safe_put(s3, bucket, object_key, raw, source['sha256'])
            if previous == state:
                report['unchanged'] += 1
                continue
            if apply:
                # Compare exact old metadata so an owner action racing the import wins.
                try:
                    table.update_item(Key=key, UpdateExpression='SET folder=:f, seen=:s, flags=:flags, import_state=:new',
                                      ConditionExpression='folder=:of AND seen=:os AND flags=:oldflags AND import_source=:origin',
                                      ExpressionAttributeValues={':f': state['folder'], ':s': seen, ':flags': state['flags'], ':new': state,
                                                                 ':of': previous['folder'], ':os': previous['seen'], ':oldflags': previous['flags'], ':origin': origin})
                except ClientError as exc:
                    if exc.response['Error']['Code'] != 'ConditionalCheckFailedException': raise
                    report['conflicts'].append({'id': source['id'], 'reason': 'destination_changed_during_import'})
                    continue
            report['metadata_refreshed'] += 1
            continue
        row = {**key, 'id': source['id'], 'key': object_key, **state,
               'received': source['received'], 'subject': parsed['subject'][:1000],
               'sender': parsed['sender'][:1000], 'date': parsed['date'][:100],
               'import_source': origin, 'import_state': state,
               'scan_status': 'historical_source_not_rescanned',
               'search': service.search_text(parsed['subject'] + '\n' + parsed['sender'] + '\n' + parsed['to'] + '\n' + parsed['body'])}
        if apply:
            safe_put(s3, bucket, object_key, raw, source['sha256'])
            current_generation(table, name, generation)
            table.put_item(Item=row, ConditionExpression='attribute_not_exists(pk)')
        report['created'] += 1
    # Include archived destination entries absent from this complete source snapshot.
    if limit is None:
        expected = {row['id'] for row in rows if row['present']}
        args = {'KeyConditionExpression': Key('pk').eq(pk) & Key('sk').begins_with('MSG#'), 'ConsistentRead': True}
        while True:
            page = table.query(**args)
            for row in page['Items']:
                if row.get('import_source', {}).get('source_id') == manifest['source_id'] and row['id'] not in expected:
                    report['conflicts'].append({'id': row['id'], 'reason': 'destination_item_absent_from_current_source'})
            if not page.get('LastEvaluatedKey'): break
            args['ExclusiveStartKey'] = page['LastEvaluatedKey']
    return report


def reconcile_archive(table, s3, bucket, name, generation, root):
    """Read back every current source item; this is storage proof, not live delivery."""
    manifest, rows, _ = archive(root)
    item = current_generation(table, name, generation)
    if name in service.NAMES or manifest['username'].lower() != item['address']:
        raise ValueError('Source and destination mailbox identity mismatch')
    pk = service.partition(item)
    report = {'source_id': manifest['source_id'], 'mailbox': name, 'generation': generation,
              'verified_messages': 0, 'verified_bytes': 0, 'differences': [],
              'storage_reconciled': False, 'ready_for_cutover': False}
    expected = set()
    for source in rows:
        if not source['present']: continue
        expected.add(source['id'])
        saved = table.get_item(Key={'pk': pk, 'sk': 'MSG#' + source['id']}, ConsistentRead=True).get('Item')
        if not saved:
            report['differences'].append({'id': source['id'], 'reason': 'missing_message'}); continue
        obj = s3.get_object(Bucket=bucket, Key=saved['key'])
        raw = obj['Body'].read()
        if len(raw) != source['size'] or hashlib.sha256(raw).hexdigest() != source['sha256']:
            report['differences'].append({'id': source['id'], 'reason': 'raw_bytes_differ'}); continue
        if (saved.get('folder') != source['folder'] or saved.get('flags') != source['flags'] or
                saved.get('seen') != any(flag.lower() == '\\seen' for flag in source['flags']) or
                saved.get('received') != source['received']):
            report['differences'].append({'id': source['id'], 'reason': 'folder_or_state_differs'}); continue
        report['verified_messages'] += 1; report['verified_bytes'] += len(raw)
    args = {'KeyConditionExpression': Key('pk').eq(pk) & Key('sk').begins_with('MSG#'), 'ConsistentRead': True}
    while True:
        page = table.query(**args)
        for saved in page['Items']:
            if saved.get('import_source', {}).get('source_id') == manifest['source_id'] and saved['id'] not in expected:
                report['differences'].append({'id': saved['id'], 'reason': 'extra_imported_message'})
        if not page.get('LastEvaluatedKey'): break
        args['ExclusiveStartKey'] = page['LastEvaluatedKey']
    current_generation(table, name, generation)
    report['storage_reconciled'] = not report['differences'] and report['verified_messages'] == manifest['message_count']
    return report


def check_stack(session, stack):
    value = session.client('cloudformation').describe_stacks(StackName=stack)['Stacks'][0]
    parameters = {row['ParameterKey']: row.get('ParameterValue') for row in value['Parameters']}
    if (any(parameters.get(key) != 'false' for key in ('SendingEnabled', 'AiSendingEnabled')) or
            parameters.get('HumanSendingEnabled', 'false') != 'false'):
        raise ValueError('All replacement application sending controls must remain false')
    if value['StackStatus'] not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'):
        raise ValueError('Stack must be stable before migration')
    os.environ.update(DOMAIN=parameters['Domain'], RECEIVE_DOMAIN=parameters['ReceiveDomain'],
                      MAILBOX_REGISTRY=parameters.get('MailboxRegistry', '{}'))
    return {row['OutputKey']: row['OutputValue'] for row in value['Outputs']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    verify = commands.add_parser('verify'); verify.add_argument('archive', type=Path)
    registry = commands.add_parser('registry')
    registry.add_argument('file', type=Path)
    registry.add_argument('--domain', default='extraliferecords.com')
    registry.add_argument('--receive-domain', default='inbox.playloudr.com')
    for command in ('provision', 'import', 'reconcile'):
        sub = commands.add_parser(command)
        sub.add_argument('--profile'); sub.add_argument('--region', default='eu-west-1', choices=['eu-west-1'])
        sub.add_argument('--stack', default='playloudr-reserved-mail')
        if command != 'reconcile': sub.add_argument('--apply', action='store_true')
        if command == 'provision':
            sub.add_argument('--owner-sub', required=True); sub.add_argument('--mailbox', action='append', required=True)
        else:
            sub.add_argument('archive', type=Path); sub.add_argument('--mailbox', required=True)
            sub.add_argument('--expected-generation', required=True)
            if command == 'import': sub.add_argument('--limit', type=int)
    args = parser.parse_args()
    if args.command == 'registry':
        os.environ.update(DOMAIN=args.domain, RECEIVE_DOMAIN=args.receive_domain, MAILBOX_REGISTRY=args.file.read_text(encoding='utf-8-sig'))
        print(json.dumps({'valid': True, 'mailboxes': mailboxes.registry(), 'live_changes': False}, indent=2)); return
    if args.command == 'verify':
        print(json.dumps(inspect(args.archive), indent=2)); return
    if getattr(args, 'limit', None) is not None and args.limit < 1: raise ValueError('Limit must be positive')
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    outputs = check_stack(session, args.stack)
    table = session.resource('dynamodb').Table(outputs['Table'])
    if args.command == 'provision':
        result = provision(table, args.mailbox, args.owner_sub, args.apply)
    else:
        bucket = outputs['Bucket']
        s3 = session.client('s3')
        if getattr(args, 'apply', False) and s3.get_bucket_versioning(Bucket=bucket).get('Status') != 'Enabled':
            raise ValueError('Destination bucket versioning must be enabled')
        if args.command == 'reconcile':
            result = reconcile_archive(table, s3, bucket, args.mailbox, args.expected_generation, args.archive)
        else:
            result = import_archive(table, s3, bucket, args.mailbox, args.expected_generation, args.archive, args.apply, args.limit)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        # Provider exception payloads and message content must not reach a terminal.
        print(json.dumps({'ok': False, 'error_type': type(error).__name__,
                          'message': 'Migration stopped without deleting source data. Inspect the private archive and operator configuration.'}), file=sys.stderr)
        raise SystemExit(1) from None
