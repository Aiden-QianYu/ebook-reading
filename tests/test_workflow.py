import copy
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))


def module(name):
    spec = importlib.util.spec_from_file_location(name, SKILL / 'scripts' / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


epub = module('epub_session')
save = module('save_session')
library = module('books_library')
chapters = module('epub_chapters')


class ReadingSkillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.book_dir = self.root / 'book.epub'
        (self.book_dir / 'META-INF').mkdir(parents=True)
        (self.book_dir / 'OEBPS').mkdir()
        (self.book_dir / 'META-INF/container.xml').write_text(
            '<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        (self.book_dir / 'OEBPS/content.opf').write_text(
            '<package><metadata/><manifest><item id="z" href="z.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="extra" href="extra.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="z"/><itemref id="ref-a" idref="a"/>'
            '<itemref idref="extra" linear="no"/></spine></package>')
        (self.book_dir / 'OEBPS/z.xhtml').write_text(
            '<html><head><title>ignore</title></head><body id="body"><p id="p">😀甲乙<b>重要</b>尾部。</p>'
            '<p>' + '学习需要回忆。' * 25 + '</p></body></html>')
        (self.book_dir / 'OEBPS/a.xhtml').write_text(
            '<html><head/><body><h1 id="c2">第二章</h1><p>' + '反思帮助理解。' * 20 + '</p></body></html>')
        opf = self.book_dir / 'OEBPS/content.opf'
        opf.write_text(opf.read_text().replace('</manifest>',
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest>'))
        (self.book_dir / 'OEBPS/nav.xhtml').write_text(
            '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>'
            '<li><a href="z.xhtml#p">第1章 第一课</a></li>'
            '<li><a href="a.xhtml#c2">第2章 第二课</a></li></ol></nav></body></html>')
        self.pub = epub.Publication(self.book_dir)
        self.book = {'asset_id': 'fixture', 'title': '测试 ` 书籍', 'author': '测试作者',
                     'path': str(self.book_dir), 'reading_progress': 0.3}
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.config = {'vault_path': str(self.vault), 'output_subfolder': '读书摘要', 'timezone': 'Asia/Shanghai'}
        self.body = '# 本次摘要\n\n<!-- VERIFIED_QUOTES -->\n\n正文测试。'
        self.chapter_index = chapters.make_index(self.pub, ['toc-1', 'toc-2'], self.book)

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, index=0, offset=0, previous=None, minutes=0.5):
        raw = epub.build_session(self.pub, self.book, index, offset, minutes, 40, 20, 'test anchor', previous)
        return chapters.annotate_time_session(self.pub, raw, self.chapter_index)

    def test_spine_and_zip_parity(self):
        self.assertEqual([d['member'] for d in self.pub.docs], ['OEBPS/z.xhtml', 'OEBPS/a.xhtml'])
        zipped = self.root / 'zip.epub'
        with zipfile.ZipFile(zipped, 'w') as z:
            for p in self.book_dir.rglob('*'):
                if p.is_file():
                    z.write(p, p.relative_to(self.book_dir).as_posix())
        zipped_pub = epub.Publication(zipped)
        self.assertEqual(zipped_pub.fingerprint, self.pub.fingerprint)
        self.assertEqual(zipped_pub.docs[0]['text'], self.pub.docs[0]['text'])

    def test_utf16_cfi_and_range(self):
        point = 'epubcfi(/6/2[z]!/4[body]/2[p]/1:2)'
        index, offset = self.pub.cfi_location(point)
        self.assertEqual(index, 0)
        self.assertTrue(self.pub.docs[0]['text'][offset:].startswith('甲乙重要尾部。'))
        self.assertEqual(self.pub.cfi_location('epubcfi(/6/2[z]!/4[body]/2[p]/1,:2,:3)'), (index, offset))
        with self.assertRaises(ValueError):
            self.pub.cfi_location('epubcfi(/6/2[wrong]!/4/2/1:2)')
        with self.assertRaises(ValueError):
            self.pub.cfi_location('epubcfi(/6/2[z]!/4/2/1:2[text])')

    def test_budget_and_chapter_boundary(self):
        session = self.session()
        self.assertLessEqual(session['estimated_minutes'], 0.55)
        self.assertGreater(session['estimated_minutes'], 0)
        bounded = epub.build_session(self.pub, self.book, 0, 0, 100, 40, 20, 'chapter', through_member='OEBPS/z.xhtml')
        self.assertEqual(len(bounded['segments']), 1)
        self.assertEqual(bounded['next_cursor'], {'format': 'epub', 'member': 'OEBPS/a.xhtml', 'offset': 0})
        self.assertFalse(bounded['at_book_end'])

    def test_quote_validation_and_idempotent_resume(self):
        first = self.session()
        segment = first['segments'][0]
        pos = segment['text'].index('甲乙') + segment['start_offset']
        quote = {'member': segment['member'], 'text': '甲乙重要尾部。', 'start_offset': pos}
        bad = dict(quote, text='原文没有这句话')
        with self.assertRaises(ValueError):
            save.save(self.config, first, self.body, [bad])
        self.assertFalse((self.vault / '读书摘要').exists())
        stored = save.save(self.config, first, self.body, [quote])
        state_file = Path(stored['state_path'])
        cursor = json.loads(state_file.read_text())['next_cursor']
        second = self.session(self.pub.member_index(cursor['member']), cursor['offset'], stored['session_id'])
        self.assertEqual(first['end_locator'], second['start_locator'])
        latest = save.save(self.config, second, self.body, [])
        state_before = state_file.read_bytes()
        duplicate = save.save(self.config, first, self.body, [quote])
        self.assertEqual(duplicate['status'], 'already_saved')
        self.assertEqual(state_before, state_file.read_bytes())
        self.assertEqual(len(list((self.vault / '读书摘要').glob('*.md'))), 2)
        self.assertIn('> 甲乙重要尾部。', Path(stored['note_path']).read_text())
        self.assertNotEqual(stored['session_id'], latest['session_id'])

    def test_stale_state_and_source_change(self):
        first = self.session()
        stored = save.save(self.config, first, self.body, [])
        stale = self.session(offset=30)
        with self.assertRaises(ValueError):
            save.save(self.config, stale, self.body, [])
        book_json = self.root / 'book.json'
        book_json.write_text(json.dumps(self.book))
        (self.book_dir / 'OEBPS/a.xhtml').write_text('<html><body><p>新版</p></body></html>')
        result = subprocess.run(['python3', str(SKILL / 'scripts/epub_session.py'), 'slice', '--book-json',
                                 str(book_json), '--resume-state', stored['state_path']], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Source version', result.stderr)

    def test_unverified_position_and_ambiguous_anchor(self):
        book_json = self.root / 'book.json'
        book_json.write_text(json.dumps(self.book))
        base = ['python3', str(SKILL / 'scripts/epub_session.py'), 'slice', '--book-json', str(book_json)]
        for extra in (['--cfi', 'epubcfi(/6/2[z]!/4/2/1:2)'], ['--anchor', '学习需要回忆。'], []):
            result = subprocess.run(base + extra, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
        unique = subprocess.run(base + ['--anchor', '甲乙重要尾部。'], capture_output=True, text=True)
        self.assertEqual(unique.returncode, 0, unique.stderr)

    def test_symlink_escape(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.vault / '读书摘要').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            save.save(self.config, self.session(), self.body, [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_readonly_library_unknown_progress(self):
        docs = self.root / 'documents'
        (docs / 'BKLibrary').mkdir(parents=True)
        db = docs / 'BKLibrary/library.sqlite'
        con = sqlite3.connect(db)
        con.execute('CREATE TABLE ZBKLIBRARYASSET(ZASSETID TEXT,ZTITLE TEXT,ZREADINGPROGRESS REAL,ZPATH TEXT)')
        con.execute('INSERT INTO ZBKLIBRARYASSET VALUES (?,?,?,?)', ('fixture', '未知进度书籍', None, str(self.book_dir)))
        con.commit()
        con.close()
        before = db.read_bytes()
        result = library.read_library(docs)
        self.assertIsNone(result['books'][0]['progress_percent'])
        self.assertEqual(db.read_bytes(), before)
        con = library.connect(db)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute('DELETE FROM ZBKLIBRARYASSET')
        con.close()

    def test_crash_after_cursor_update_can_recover(self):
        reading = self.session()
        real_write = save.atomic_json

        def fail_receipt(path, data):
            if path.name.startswith('session-'):
                raise OSError('simulated receipt failure')
            real_write(path, data)

        save.atomic_json = fail_receipt
        try:
            with self.assertRaises(OSError):
                save.save(self.config, reading, self.body, [])
        finally:
            save.atomic_json = real_write
        recovered = save.save(self.config, reading, self.body, [])
        self.assertEqual(recovered['status'], 'saved')
        self.assertEqual(len(list((self.vault / '读书摘要').glob('*.md'))), 1)

    def test_explicit_chapters_filename_and_content_order(self):
        reading = chapters.chapter_session(self.pub, self.book, self.chapter_index, requested='1-2')
        reading['created_at'] = '2026-10-06T14:30:00+00:00'
        self.assertIsNone(reading['planned_minutes'])
        self.assertEqual(reading['actual_chapter_count'], 2)
        self.assertEqual(reading['last_sentence'], '反思帮助理解。')
        self.assertEqual(''.join(s['text'] for s in reading['segments']), ''.join(d['text'] for d in self.pub.docs))
        result = save.save(self.config, reading, self.body, [])
        path = Path(result['note_path'])
        self.assertEqual(path.name, '2026-10-06_22-30-00-测试 ` 书籍-第1章 第一课.md')
        content = path.read_text()
        self.assertTrue(content.startswith('**本次阅读范围：** 第1章'))
        self.assertLess(content.index('本次最后一句'), content.index('# 本次摘要'))
        self.assertLess(content.index('正文测试。'), content.index('阅读记录与下次接续'))
        self.assertNotIn('---\nbook:', content)

    def test_relative_chapters_include_unfinished_current_chapter(self):
        cursor = {'member': self.pub.docs[0]['member'], 'offset': 5}
        reading = chapters.chapter_session(self.pub, self.book, self.chapter_index, count=2, cursor=cursor)
        self.assertEqual(reading['start_locator'], cursor)
        self.assertEqual(reading['actual_chapter_count'], 2)
        self.assertTrue(reading['chapter_ranges'][0]['partial_start'])
        boundary = self.chapter_index['chapters'][0]['end']
        remaining = chapters.chapter_session(self.pub, self.book, self.chapter_index, count=2, cursor=boundary)
        self.assertEqual(remaining['actual_chapter_count'], 1)
        self.assertEqual(remaining['chapter_ranges'][0]['number'], 2)
        whitespace_tail = {'member': self.pub.docs[0]['member'], 'offset': len(self.pub.docs[0]['text']) - 1}
        remaining = chapters.chapter_session(self.pub, self.book, self.chapter_index, count=2, cursor=whitespace_tail)
        self.assertEqual(remaining['chapter_ranges'][0]['number'], 2)
        self.assertEqual(remaining['actual_chapter_count'], 1)

    def test_shared_file_nested_toc_and_noncontiguous_chapters(self):
        (self.book_dir / 'OEBPS/z.xhtml').write_text(
            '<html><head/><body><h2 id="c1">第一章</h2><p>甲章正文。</p>'
            '<h3 id="s1">章内小节</h3><p>甲章小节。</p>'
            '<h2 id="c2">第二章</h2><p>乙章正文。</p>'
            '<h2 id="c3">第三章</h2><p>丙章正文。</p></body></html>')
        opf = self.book_dir / 'OEBPS/content.opf'
        opf.write_text(opf.read_text().replace('<itemref id="ref-a" idref="a"/>', ''))
        (self.book_dir / 'OEBPS/nav.xhtml').write_text(
            '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>'
            '<li><a href="z.xhtml#c1">第一部</a><ol>'
            '<li><a href="z.xhtml#c1">第一章</a><ol><li><a href="z.xhtml#s1">小节</a></li></ol></li>'
            '<li><a href="z.xhtml#c2">第二章</a></li>'
            '<li><a href="z.xhtml#c3">第三章</a></li></ol></li></ol></nav></body></html>')
        pub = epub.Publication(self.book_dir)
        index = chapters.make_index(pub, ['toc-2', 'toc-4', 'toc-5'], self.book)
        second = chapters.chapter_session(pub, self.book, index, requested='2')
        text = ''.join(s['text'] for s in second['segments'])
        self.assertIn('乙章正文。', text)
        self.assertNotIn('甲章正文。', text)
        self.assertNotIn('丙章正文。', text)
        separate = chapters.chapter_session(pub, self.book, index, requested='1,3')
        text = ''.join(s['text'] for s in separate['segments'])
        self.assertIn('甲章小节。', text)
        self.assertIn('丙章正文。', text)
        self.assertNotIn('乙章正文。', text)
        self.assertIn('非连续章节', separate['reading_range'])

    def test_long_unicode_names_and_same_second_collisions(self):
        first = self.session()
        first['title'] = '测试书名' * 80
        first['starting_chapter_title'] = '起始章节' * 80
        first['created_at'] = '2026-10-06T14:30:00+00:00'
        one = save.save(self.config, first, self.body, [])
        second = self.session(offset=30, previous=one['session_id'])
        second['title'] = first['title']
        second['starting_chapter_title'] = first['starting_chapter_title']
        second['created_at'] = first['created_at']
        two = save.save(self.config, second, self.body, [])
        self.assertNotEqual(one['note_path'], two['note_path'])
        self.assertIn('22-30-00.000000', Path(two['note_path']).name)
        self.assertLessEqual(len(Path(two['note_path']).name.encode('utf-8')), 255)
        self.assertEqual(save.save(self.config, first, self.body, [])['note_path'], one['note_path'])

    def test_local_config_override_on_command_line(self):
        public = self.root / 'config.json'
        public.write_text(json.dumps(dict(self.config, vault_path=None)))
        (self.root / 'config.local.json').write_text(json.dumps({'vault_path': str(self.vault)}))
        reading = self.root / 'reading.json'
        reading.write_text(json.dumps(self.session()))
        body = self.root / 'body.md'
        body.write_text(self.body)
        quotes = self.root / 'quotes.json'
        quotes.write_text('[]')
        result = subprocess.run(['python3', str(SKILL / 'scripts/save_session.py'), '--config', str(public),
                                 '--reading', str(reading), '--body', str(body), '--quotes', str(quotes)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(Path(json.loads(result.stdout)['note_path']).exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
