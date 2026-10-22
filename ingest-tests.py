"""Fixtures test mechanics, not fresh-agent skill triggering or semantic accuracy."""
import copy, importlib.util, json, subprocess, sys, tempfile, unittest, zipfile
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('ingest', Path(__file__).with_name('ingest.py'))
i = importlib.util.module_from_spec(spec)
spec.loader.exec_module(i)


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'course'
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_new_changed_retry_exclusions(self):
        # Prompt: 新放的英语通知帮我导入，旧的别重复，失败的重试。
        p = self.root / 'notice.txt'
        p.write_text('下次课交演讲提纲，具体日期待定。', encoding='utf-8-sig')
        for folder in ('待办日历', '.agents', 'node_modules', '.git', 'cache'):
            d = self.root / folder
            d.mkdir()
            (d / 'ignored.txt').write_text('not a task')
        (self.root / '~$draft.txt').write_text('ignore')
        (self.root / '.env').write_text('ignore')
        first = i.scan(self.root, [], self.base / 'run1')
        self.assertEqual(len(first['files']), 1)
        prior = copy.deepcopy(first['files'])
        prior[0]['status'] = 'processed'
        self.assertEqual(i.scan(self.root, prior, self.base / 'run2')['skippedUnchanged'], 1)
        prior[0]['status'] = 'no_tasks'
        self.assertEqual(i.scan(self.root, prior, self.base / 'run3')['skippedUnchanged'], 1)
        for status in ('failed', 'needs_review'):
            prior[0]['status'] = status
            self.assertEqual(len(i.scan(self.root, prior, self.base / status)['files']), 1)
        prior[0]['status'] = 'processed'
        p.write_text('新增：阅读第三章。', encoding='gb18030')
        changed = i.scan(self.root, prior, self.base / 'run4')
        self.assertNotEqual(changed['files'][0]['sha256'], prior[0]['sha256'])

    def test_office_order_notes_tables_and_media(self):
        # Prompt: PPT备注和Word表格中的作业也别漏，图片看不懂就待核对。
        p = self.root / 'lecture.pptx'
        a = 'http://schemas.openxmlformats.org/drawingml/2006/main'
        r = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        with zipfile.ZipFile(p, 'w') as z:
            z.writestr('ppt/presentation.xml', f'<p xmlns:r="{r}"><sldId r:id="r2"/><sldId r:id="r1"/></p>')
            z.writestr('ppt/_rels/presentation.xml.rels', '<Relationships><Relationship Id="r1" Target="slides/slide1.xml"/><Relationship Id="r2" Target="slides/slide2.xml"/></Relationships>')
            for n, text in [(1, '第二页'), (2, '第一页作业')]:
                z.writestr(f'ppt/slides/slide{n}.xml', f'<s xmlns:a="{a}"><a:p><a:r><a:t>{text}</a:t></a:r></a:p></s>')
            z.writestr('ppt/slides/_rels/slide2.xml.rels', '<Relationships><Relationship Type="x/notesSlide" Target="../notesSlides/notesSlide1.xml"/></Relationships>')
            z.writestr('ppt/notesSlides/notesSlide1.xml', f'<s xmlns:a="{a}"><a:p><a:r><a:t>备注：独立完成</a:t></a:r></a:p></s>')
            z.writestr('ppt/media/image1.png', b'fixture')
        rows, warnings = i.extract(p)
        self.assertEqual([r['text'] for r in rows], ['第一页作业', '备注：独立完成', '第二页'])
        self.assertIn('speaker-note', rows[1]['locator'])
        self.assertIn('embedded_media_review', warnings)
        p = self.root / 'assignment.docx'
        with zipfile.ZipFile(p, 'w') as z:
            z.writestr('word/document.xml', '<document><body><p><t>通知</t></p><tbl><tr><tc><p><t>表格作业</t></p></tc></tr></tbl></body></document>')
        self.assertEqual([r['text'] for r in i.extract(p)[0]], ['通知', '表格作业'])

    def test_validation_dates_identity_and_bounded_chunks(self):
        # Prompt: 通知写下周五但没有课程日期，不要猜年份；先校验批次。
        p = self.root / 'notice.txt'
        p.write_text('A' * 30000, encoding='utf-8')
        manifest = i.scan(self.root, [], self.base / 'run')
        f = manifest['files'][0]
        self.assertGreater(len(f['chunks']), 1)
        for chunk in f['chunks']:
            self.assertLess((self.base / 'run' / chunk).stat().st_size, 16000)
        t = dict(title='提交演讲提纲', category='Sample Course', priority=3, priorityDefault=True, due='', remind=4320, duration=30, completed=False, notes='独立完成', source='notice.txt', sourceNotes='line:1 原文证据', sourceTiming='下周五，缺少锚点', kind='assignment', firedKey='', createdAt=i.stamp(), updatedAt=i.stamp())
        batch = {'batchId': 'test_01', 'files': [dict(path=f['path'], sha256=f['sha256'], status='processed', tasks=[t])]}
        i.validate(batch, manifest)
        original = t['id']
        batch_path = self.base / 'batch.json'
        batch_path.write_text(json.dumps(batch), encoding='utf-8')
        check = subprocess.run([sys.executable, str(Path(__file__).with_name('ingest.py')), 'submit', '--batch', str(batch_path), '--manifest', str(self.base / 'run' / 'manifest.json'), '--dry-run'], capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertTrue(json.loads(check.stdout)['valid'])
        t['due'] = '2026-09-20T12:00'
        self.assertEqual(i.task_id('notice.txt', t), original)
        t['due'] = '2026-02-30T12:00'
        with self.assertRaises(ValueError):
            i.validate(batch, manifest)
        t['due'] = ''
        p.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'source_changed_rescan'):
            i.validate(batch, manifest)
        for relative in ('../escape.txt', '.env', 'C:/secret.txt', '待办日历/log.txt'):
            with self.assertRaises(ValueError):
                i.safe_source(self.root, relative)
        self.assertNotIn('.pem', i.EXT)

    def test_endpoint_and_project_privacy(self):
        for base in ('http://127.0.0.1:8766', 'http://localhost:8766', 'http://[::1]:8766'):
            self.assertEqual(i.API(base, 'fixture').base, base)
        for base in ('https://planner.example', 'https://planner.example:8443'):
            with self.assertRaisesRegex(ValueError, 'remote_api_requires_explicit_approval'):
                i.API(base, 'fixture')
            self.assertEqual(i.API(base, 'fixture', allow_remote=True).base, base)
        for base in ('http://example.invalid', 'https://user:secret@example.invalid', 'https://planner.example/path'):
            with self.assertRaises(ValueError):
                i.API(base, 'fixture', allow_remote=True)
        with self.assertRaisesRegex(ValueError, 'api_redirect_refused'):
            i.NoRedirect().redirect_request(None, None, None, None, None, None)
        with self.assertRaisesRegex(ValueError, 'project_source_excluded'):
            i.scan(i.HERE, [], self.base/'private-output')
        # Model an existing project data directory without relying on release state.
        project = self.base / 'sample-project'
        (project / 'data').mkdir(parents=True)
        with patch.object(i, 'HERE', project.resolve()):
            for root in (project, project / 'data'):
                with self.assertRaisesRegex(ValueError, 'project_source_excluded'):
                    i.scan(root, [], self.base/'private-output')

    def test_generated_output_and_symlink_exclusions(self):
        (self.root/'notice.txt').write_text('real course source')
        out = self.root/'generated'
        out.mkdir()
        (out/'extracted.txt').write_text('generated text is not a source')
        result = i.scan(self.root, [], out)
        self.assertEqual([f['path'] for f in result['files']], ['notice.txt'])
        outside = self.base/'private.txt'
        outside.write_text('must not be read')
        link = self.root/'linked.txt'
        try:
            link.symlink_to(outside)
        except OSError:
            return  # Windows without symlink privilege; explicit path guards still tested.
        self.assertEqual([f['path'] for f in i.scan(self.root, [], out)['files']], ['notice.txt'])
        with self.assertRaisesRegex(ValueError, 'unsafe_source_link'):
            i.safe_source(self.root, 'linked.txt')

    def test_legacy_and_image_not_complete(self):
        for ext, warning in [('.doc', 'requires_conversion'), ('.ppt', 'requires_conversion'), ('.png', 'needs_ocr')]:
            p = self.root / ('old' + ext)
            p.write_bytes(b'fixture')
            self.assertIn(warning, i.extract(p)[1])


if __name__ == '__main__':
    unittest.main(verbosity=2)
