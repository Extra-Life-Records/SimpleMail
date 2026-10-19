import json
import os
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import boto3
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
import service


@mock_aws
class IndexedSearchTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, AWS_DEFAULT_REGION='eu-west-1', BUCKET='search-test')
        env.start(); self.addCleanup(env.stop)
        self.table = boto3.resource('dynamodb').create_table(TableName='search-test', BillingMode='PAY_PER_REQUEST',
            AttributeDefinitions=[{'AttributeName': k, 'AttributeType': 'S'} for k in ('pk', 'sk', 'received')],
            KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
            GlobalSecondaryIndexes=[{'IndexName': 'Chronological', 'KeySchema': [
                {'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'received', 'KeyType': 'RANGE'}],
                'Projection': {'ProjectionType': 'ALL'}}])
        self.s3 = boto3.client('s3')
        self.s3.create_bucket(Bucket='search-test', CreateBucketConfiguration={'LocationConstraint': 'eu-west-1'})
        self.raw('ordinary', 'Nothing matching here')

    def raw(self, key, body, cc=''):
        message = EmailMessage(); message['Subject'] = 'Unrelated'; message['Cc'] = cc; message.set_content(body)
        self.s3.put_object(Bucket='search-test', Key=key, Body=message.as_bytes())

    def fill(self, count, overrides=None):
        overrides = overrides or {}
        with self.table.batch_writer() as batch:
            for index in range(count):
                batch.put_item(Item={'pk': 'one#first', 'sk': 'MSG#' + str(index), 'id': str(index),
                    'received': (datetime(2026, 10, 19, tzinfo=timezone.utc) - timedelta(seconds=index)).isoformat(),
                    'folder': 'Inbox', 'seen': False, 'search': 'ordinary', 'key': 'ordinary', **overrides.get(index, {})})

    def search(self, cursor=None, **changes):
        query = {'folder': 'All', 'q': 'late indexed (example)', 'search_mode': 'indexed', 'limit': '100', **changes}
        if cursor: query['cursor'] = cursor
        return service.list_messages(self.table, self.s3, 'one#first', query)

    def test_sparse_row_554_surfaces_before_any_full_body_download(self):
        self.fill(651, {553: {'search': 'subject: late indexed (example)'}})
        cursor = None
        with patch.object(self.s3, 'get_object', side_effect=AssertionError('Indexed search downloaded MIME')):
            for number in range(1, 7):
                page = self.search(cursor)
                self.assertEqual([row['id'] for row in page['messages']], ['553'] if number == 6 else [])
                self.assertTrue(page['cursor']); cursor = page['cursor']
                self.assertLessEqual(page['search_progress']['scanned'], 100)

    def test_full_body_and_cc_matches_are_found_once_after_indexed_results(self):
        needle = 'late indexed (example)'
        self.raw('long', 'x' * 200000 + needle)
        self.raw('cc', 'No matching body', '"' + needle + '" <person@example.invalid>')
        self.fill(25, {2: {'search': needle}, 21: {'key': 'long'}, 24: {'key': 'cc'}})
        ids, cursor, cursors = [], None, set()
        while True:
            with patch.object(self.s3, 'get_object', wraps=self.s3.get_object) as reads:
                page = self.search(cursor)
                self.assertLessEqual(reads.call_count, 10)
            ids.extend(row['id'] for row in page['messages'])
            cursor = page['cursor']
            if not cursor: break
            self.assertNotIn(cursor, cursors); cursors.add(cursor)
        self.assertEqual(ids, ['2', '21', '24'])

    def test_no_match_does_not_finish_before_both_bounded_passes_exhaust(self):
        self.fill(25)
        pages, cursor = [], None
        while True:
            page = self.search(cursor); pages.append(page)
            self.assertEqual(page['messages'], [])
            cursor = page['cursor']
            if not cursor: break
        self.assertEqual([page['search_progress']['phase'] for page in pages], ['index', 'body', 'body', 'body'])
        self.assertTrue(all(page['cursor'] for page in pages[:-1]))
        self.assertEqual(sum(p['search_progress']['scanned'] for p in pages if p['search_progress']['phase'] == 'body'), 25)

    def test_query_folder_and_generation_changes_require_explicit_restart(self):
        self.fill(3)
        cursor = self.search()['cursor']
        for change in ({'q': 'other'}, {'folder': 'Sent'}, {'cursor': 'legacy-cursor'}):
            with self.assertRaisesRegex(ValueError, 'start the search again'):
                self.search(**({'cursor': cursor} | change))
        with self.assertRaisesRegex(ValueError, 'start the search again'):
            service.list_messages(self.table, self.s3, 'one#reassigned',
                {'q': 'late indexed (example)', 'folder': 'All', 'limit': '100', 'search_mode': 'indexed', 'cursor': cursor})

    def test_old_search_and_cursors_remain_compatible_without_opt_in(self):
        self.fill(25, {index: {'search': 'needle'} for index in range(25)})
        query = {'q': 'needle', 'folder': 'All', 'limit': '100'}
        first = service.list_messages(self.table, self.s3, 'one#first', query)
        second = service.list_messages(self.table, self.s3, 'one#first', {**query, 'cursor': first['cursor']})
        self.assertEqual(len(first['messages']), 10); self.assertEqual(len(second['messages']), 10)
        self.assertFalse({r['id'] for r in first['messages']} & {r['id'] for r in second['messages']})

    def test_new_search_rejects_overlong_query_and_keeps_full_unicode_casefold(self):
        needle = '\u00df' * 600
        self.fill(1, {0: {'search': needle.casefold()}})
        self.assertEqual(len(self.search(q=needle)['messages']), 1)
        with self.assertRaisesRegex(ValueError, 'Invalid search mode'):
            self.search(q='x' * 1001)


if __name__ == '__main__': unittest.main()
