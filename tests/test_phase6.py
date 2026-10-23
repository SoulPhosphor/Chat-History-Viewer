"""Session 10: bounded pages, literal search, privacy, and startup polish.
All history, files and logs are made up and kept in temporary directories.
"""
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import build_db
import message_ids
import request_limits
import server
from tests import test_query_rules
from tests.test_browser_side import markdown
from tests import test_message_ids


class PagingTests(unittest.TestCase):
    setUp = test_query_rules.QueryRulesTests.setUp
    tearDown = test_query_rules.QueryRulesTests.tearDown
    get = test_query_rules.QueryRulesTests.get

    def long_chat(self):
        conn = server.open_db(self.db_path)
        conn.execute("DELETE FROM messages WHERE conversation_id = 'keep'")
        for i in range(507):
            conn.execute("INSERT INTO messages(conversation_id, seq, role, content, message_uuid, attachments, artifact_ids, siblings, branch_index) VALUES ('keep', ?, ?, ?, ?, ?, ?, ?, 1)",
                         (i, 'user' if i % 2 == 0 else 'assistant', f'body {i}', f'm-{i}',
                          json.dumps([{'name': f'file-{i}.pdf', 'content': 'text'}]),
                          json.dumps(['artifact']) if i == 503 else None,
                          json.dumps([{'content': 'original'}, {'content': 'retry'}]) if i == 502 else None))
        conn.execute("UPDATE conversations SET message_count = 507 WHERE id = 'keep'")
        conn.execute("INSERT INTO artifacts(id, conv_id, msg_seq, title, type, content) VALUES ('artifact', 'keep', 503, 'Work', 'text', 'artifact body')")
        conn.commit()
        conn.close()

    def test_all_messages_page_without_loss_and_keep_features(self):
        self.long_chat()
        offset, seen = 0, []
        while offset is not None:
            page = self.get('/api/conversation/keep', offset=offset, limit=99)
            self.assertEqual(page['total'], 507)
            self.assertLessEqual(len(page['messages']), 100)
            seen.extend(m['seq'] for m in page['messages'])
            if page['next_offset'] is not None:
                self.assertNotEqual(page['messages'][-1]['role'], 'user')
            offset = page['next_offset']
        self.assertEqual(seen, list(range(507)))
        page = self.get('/api/conversation/keep', target_seq=503)
        target = next(m for m in page['messages'] if m['seq'] == 503)
        self.assertEqual(target['message_uuid'], 'm-503')
        self.assertEqual(target['artifact_ids'], ['artifact'])
        self.assertEqual(page['artifacts']['artifact']['title'], 'Work')
        self.assertEqual(next(m for m in page['messages'] if m['seq'] == 502)['siblings'][1]['content'], 'retry')
        self.assertEqual(target['attachments'][0]['name'], 'file-503.pdf')

    def test_one_message_limit_keeps_pairs_without_empty_pages(self):
        self.long_chat()
        first = self.get('/api/conversation/keep', limit=1, offset=1)
        self.assertEqual([m['seq'] for m in first['messages']], [0, 1])
        self.assertEqual(first['next_offset'], 2)
        second = self.get('/api/conversation/keep', limit=1, offset=2)
        self.assertEqual([m['seq'] for m in second['messages']], [2, 3])
        self.assertEqual(second['previous_offset'], 0)

    def test_previous_is_inverse_of_next_when_pair_expands_page(self):
        self.long_chat()
        conn = server.open_db(self.db_path)
        # Consecutive users at the start move the odd/even pair boundaries.
        conn.execute("UPDATE messages SET role = CASE WHEN seq < 2 OR seq % 2 = 1 THEN 'user' ELSE 'assistant' END WHERE conversation_id = 'keep'")
        conn.commit(); conn.close()
        pages, offset = [], 0
        while offset is not None:
            page = self.get('/api/conversation/keep', limit=100, offset=offset)
            pages.append(page)
            offset = page['next_offset']
        self.assertEqual(pages[0]['next_offset'], 101)
        for current, previous in zip(pages[1:], pages):
            restored = self.get('/api/conversation/keep', limit=100, offset=current['previous_offset'])
            self.assertEqual(restored['messages'], previous['messages'])
        target = self.get('/api/conversation/keep', limit=100, target_seq=100)
        self.assertIn(100, [m['seq'] for m in target['messages']])

    def test_saved_offset_after_shorter_reimport_returns_real_messages(self):
        self.long_chat()
        page = self.get('/api/conversation/keep', offset=99999)
        self.assertEqual(page['messages'][-1]['seq'], 506)
        self.assertEqual(page['total'], 507)
        self.assertIsNone(page['next_offset'])

    def test_consecutive_user_messages_do_not_overlap_pages(self):
        self.long_chat()
        conn = server.open_db(self.db_path)
        conn.execute("UPDATE messages SET role = 'user' WHERE conversation_id = 'keep'")
        conn.commit(); conn.close()
        offset, seen = 0, []
        while offset is not None:
            page = self.get('/api/conversation/keep', limit=100, offset=offset)
            seen.extend(m['seq'] for m in page['messages'])
            offset = page['next_offset']
        self.assertEqual(seen, list(range(507)))

    def test_page_caps_and_invalid_numbers(self):
        self.long_chat()
        page = self.get('/api/conversation/keep', limit=999999)
        self.assertLessEqual(len(page['messages']), 201)
        self.assertEqual(page['limit'], 200)
        page = self.get('/api/conversations', limit=-1, offset=-1)
        self.assertEqual(page['limit'], 1)
        self.assertEqual(page['offset'], 0)
        self.assertEqual(self.get('/api/conversations', limit=999999)['limit'], 500)

    def test_gallery_pages_respect_provider_and_deleted_and_rename(self):
        self.long_chat()
        offset, seen = 0, []
        while offset is not None:
            data = self.get('/api/gallery', provider='chatgpt', limit=37, offset=offset)
            attachments = data['sections'][1]
            self.assertLessEqual(attachments['count'], 37)
            for section in data['sections']:
                for group in section['groups']:
                    self.assertNotIn(group['conv_id'], ['other', 'binned'])
                    if group['conv_id'] == 'renamed':
                        self.assertEqual(group['conv_title'], 'Garden renamed')
                    seen.extend((group['conv_id'], item['name']) for item in group['items'] if item['kind'] == 'attachment')
            offset = data['next_offset']
        self.assertEqual(len([x for x in seen if x[0] == 'keep']), 507)
        self.assertEqual(len(seen), len(set(seen)))
        data = self.get('/api/gallery', provider='claude')
        self.assertEqual([g['conv_id'] for g in data['sections'][1]['groups']], ['other'])

    def test_unlinked_files_stay_global_across_pages_and_provider(self):
        source = self.root / 'source'
        files = source / 'files'
        files.mkdir(parents=True, exist_ok=True)
        for name in ['keep.png', 'free.png', 'other.png']:
            (files / name).write_bytes(b'fake image')
        conn = server.open_db(self.db_path)
        for cid in ['keep', 'other']:
            conn.execute("UPDATE messages SET attachments = ? WHERE conversation_id = ? AND role = 'user'", (json.dumps([{'name': cid + '.png', 'type': 'image'}]), cid))
        conn.commit(); conn.close()
        with patch.object(self.server.RequestHandlerClass, 'source_dir', source):
            data = self.get('/api/gallery', provider='chatgpt', limit=1, offset=0)
        self.assertEqual([i['filename'] for i in data['unlinked_images']], ['free.png'])
        self.assertEqual(data['unlinked_total'], 1)

    def test_in_chat_search_is_literal_in_messages_and_artifacts(self):
        conn = server.open_db(self.db_path)
        conn.execute("UPDATE messages SET content = '100% value_a \\path' WHERE conversation_id = 'keep' AND seq = 0")
        conn.execute("UPDATE messages SET content = '100X valueXa' WHERE conversation_id = 'keep' AND seq != 0")
        conn.commit(); conn.close()
        for q in ['%', '_', '\\']:
            matches = self.get('/api/search-in-conversation', conv_id='keep', q=q)['matches']
            self.assertEqual([m['seq'] for m in matches], [0])


class PolishTests(unittest.TestCase):
    def test_bad_dates_are_unknown_and_unicode_filenames_match(self):
        for value in ['', 'broken', None]:
            self.assertIsNone(build_db._iso_to_ts(value))
        self.assertIsInstance(build_db._iso_to_ts('2024-01-01T00:00:00Z'), float)
        self.assertEqual(server._norm_filename('Café image.png'), server._norm_filename('Cafe\u0301_image.png'))
        self.assertIn('The condensed summary', str(request_limits.too_long('condensed_summary', 200000)))
        self.assertNotIn('condensed_summary', str(request_limits.too_long('condensed_summary', 200000)))

    def test_markdown_never_contacts_external_image_before_click(self):
        for url in ['https://tracker.example/open.png', '//tracker.example/open.png', 'http://tracker.example/open.png']:
            html = markdown(f'![A picture]({url})')
            self.assertNotIn('<img', html)
            self.assertIn('Load external image', html)
            self.assertIn('data-remote-image=', html)
        self.assertIn('<img src="/files/photo.png"', markdown('![Photo](/files/photo.png)'))
        for url in ['javascript:alert', 'data:image/svg+xml,test', '/elsewhere/image.png']:
            self.assertNotIn('<img', markdown(f'![Photo]({url})'))

    def test_windows_restart_launches_replacement_and_keeps_port(self):
        with patch.object(server.sys, 'platform', 'win32'), patch.object(server.subprocess, 'Popen') as spawn, patch.object(server.os, '_exit') as leave, patch.object(server.os, 'execv') as execv, patch.dict(server.os.environ, {}, clear=False):
            server._restart_process(54321)
            self.assertEqual(spawn.call_args.kwargs['env']['CHV_PORT'], '54321')
            self.assertEqual(spawn.call_args.kwargs['env']['CHV_RESTART_WAIT_PORT'], '54321')
            leave.assert_called_once_with(0)
            execv.assert_not_called()

    def test_ui_behavior(self):
        subprocess.run(['node', 'tests/phase6_ui.cjs'], cwd=Path(__file__).parents[1], check=True)


class BackfillTests(unittest.TestCase):
    setUp = test_message_ids.MessageIdTests.setUp
    tearDown = test_message_ids.MessageIdTests.tearDown

    def test_new_backup_can_repair_ids_without_rescanning_unchanged_files(self):
        self.conn.execute("UPDATE messages SET message_uuid = NULL, content = 'new text' WHERE seq = 0")
        self.conn.commit()
        message_ids.fill_missing_from_sources(self.db, self.source)
        data = json.loads((self.source / 'conversations.json').read_text())
        data[0]['mapping']['u1']['message']['content']['parts'] = ['new text']
        (self.source / 'additional.json').write_text(json.dumps(data))
        with patch.object(build_db, 'scan_export', wraps=build_db.scan_export) as scan:
            self.assertEqual(message_ids.fill_missing_from_sources(self.db, self.source), 1)
            self.assertEqual([call.args[0].name for call in scan.call_args_list], ['additional.json'])
        self.assertEqual(message_ids.message_seq(self.conn, 'c1', 'u1'), 0)

    def test_failed_legacy_scan_is_retried(self):
        self.conn.execute("UPDATE messages SET message_uuid = NULL")
        self.conn.commit()
        with patch.object(build_db, 'scan_export', side_effect=OSError('temporary')), patch.object(server.errorlog, 'record_exception') as logged:
            message_ids.fill_missing_from_sources(self.db, self.source)
            logged.assert_called()
        self.assertIsNone(self.conn.execute("SELECT signature FROM message_id_backfill").fetchone())
        self.assertEqual(message_ids.fill_missing_from_sources(self.db, self.source), 4)

    def test_unresolvable_ids_do_not_rescan_when_files_arrive(self):
        self.conn.execute("UPDATE messages SET message_uuid = NULL, content = 'unmatchable' WHERE seq = 0")
        self.conn.commit()
        message_ids.fill_missing_from_sources(self.db, self.source)
        (self.source / 'new.json').write_text('[]')
        with patch.object(build_db, 'scan_export', side_effect=AssertionError('Unexpected scan')):
            self.assertEqual(message_ids.fill_missing_from_sources(self.db, self.source), 0)
