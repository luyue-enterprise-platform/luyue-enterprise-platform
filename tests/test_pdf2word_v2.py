# -*- coding: utf-8 -*-
"""pdf2word v2.0.0 可逆转换测试

需求覆盖：
1. 版本号：模块 __version__ == '2.0.0'
2. 格式支持与容错：支持格式清单、不支持格式/空文件给出明确原因、单文件失败不中断
3. 独立模式：命名与源一致仅换扩展名、重名自动加序号、一一对应可溯源
4. 合并模式：按传入顺序合并为单个 PDF（含页序校验）
5. 双向隔离：pdf2word 原有流程/输出不变（converter 未改动 + 功能回归）
6. 端点：direction 分发、非法方向 400、空上传 400、result 携带方向/模式
"""
import io
import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app
from modules.pdf2word import __version__ as MODULE_VERSION
from modules.pdf2word.core import to_pdf
from modules.pdf2word.core import converter
from modules.pdf2word import blueprint as bp


def _make_pdf(path, texts):
    """生成含指定文本（每页一条）的测试 PDF（中文字体，可提取校验）"""
    import fitz
    doc = fitz.open()
    for t in texts:
        page = doc.new_page()
        page.insert_text((72, 100), t, fontsize=18, fontname='china-s')
    doc.save(path)
    doc.close()
    return path


def _make_png(path, size=(60, 40), color=(200, 30, 30)):
    from PIL import Image
    Image.new('RGB', size, color).save(path)
    return path


def _make_txt(path, content):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    return path


# ==================== 1. 版本与格式支持 ====================
class TestVersionAndFormats(unittest.TestCase):

    def test_module_version(self):
        self.assertEqual(MODULE_VERSION, '2.1.0')

    def test_health_reports_version(self):
        with flask_app.test_client() as c:
            with c.session_transaction() as sess:
                sess['user_id'] = 1
                sess['username'] = 'tester'
            r = c.get('/pdf2word/api/health')
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()['version'], '2.1.0')

    def test_supported_exts(self):
        for ext in ['.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tif',
                    '.tiff', '.webp', '.doc', '.docx', '.xls', '.xlsx',
                    '.txt', '.md', '.pdf']:
            self.assertTrue(to_pdf.is_supported('x' + ext), ext)
        for ext in ['.exe', '.zip', '.mp4', '.pptx', '.html', '']:
            self.assertFalse(to_pdf.is_supported('x' + ext), ext)


# ==================== 2. 单格式转换 ====================
class TestSingleConverters(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_image_to_pdf(self):
        src = _make_png(os.path.join(self.tmp, 'img.png'))
        dst = os.path.join(self.tmp, 'img.pdf')
        pages, _orient = to_pdf.convert_image_to_pdf(src, dst)
        self.assertGreaterEqual(pages, 1)
        self.assertTrue(os.path.isfile(dst))

    def test_text_to_pdf_chinese_and_wrap(self):
        src = _make_txt(os.path.join(self.tmp, 't.txt'),
                        '第一行中文内容\n' + '长' * 4000 + '\nEND')
        dst = os.path.join(self.tmp, 't.pdf')
        pages, _report = to_pdf.convert_text_to_pdf(src, dst)
        self.assertGreaterEqual(pages, 2)  # 长文本须分页
        import fitz
        with fitz.open(dst) as d:
            all_text = ''.join(p.get_text() for p in d)
        self.assertIn('第一行中文内容', all_text)
        self.assertIn('END', all_text)

    def test_pdf_passthrough(self):
        src = _make_pdf(os.path.join(self.tmp, 'a.pdf'), ['A1', 'A2'])
        dst = os.path.join(self.tmp, 'copy.pdf')
        pages, _report = to_pdf.convert_pdf_to_pdf(src, dst)
        self.assertEqual(pages, 2)

    def test_empty_file_raises_with_reason(self):
        src = os.path.join(self.tmp, 'empty.txt')
        open(src, 'w').close()
        with self.assertRaises(ValueError) as ctx:
            to_pdf.convert_one(src, os.path.join(self.tmp, 'o.pdf'))
        self.assertIn('空文件', str(ctx.exception))

    def test_unsupported_ext_raises_with_reason(self):
        src = os.path.join(self.tmp, 'bad.exe')
        with open(src, 'wb') as f:
            f.write(b'MZ')
        with self.assertRaises(ValueError) as ctx:
            to_pdf.convert_one(src, os.path.join(self.tmp, 'o.pdf'))
        self.assertIn('不支持的格式', str(ctx.exception))

    def test_unique_out_path(self):
        used = set()
        p1 = to_pdf._unique_out_path(self.tmp, '报告', used)
        p2 = to_pdf._unique_out_path(self.tmp, '报告', used)
        self.assertEqual(os.path.basename(p1), '报告.pdf')
        self.assertEqual(os.path.basename(p2), '报告(1).pdf')

    def test_word_com_to_pdf_if_available(self):
        """Word COM 实测（本机装有 Office 时执行，无 COM 则跳过）"""
        try:
            import win32com.client  # noqa: F401
        except ImportError:
            self.skipTest('pywin32 未安装')
        try:
            app = win32com.client.Dispatch('Word.Application')
            _ = app.Version  # 探测可用性（不 Quit：避免 ROT 返回垂死实例）
        except Exception:
            self.skipTest('Word COM 不可用')
        # 生成 docx（用 python-docx）
        from docx import Document
        docx_path = os.path.join(self.tmp, '文档.docx')
        d = Document()
        d.add_paragraph('Word COM 转换实测内容')
        d.save(docx_path)
        dst = os.path.join(self.tmp, '文档.pdf')
        pages, _report = to_pdf.convert_word_to_pdf(docx_path, dst)
        self.assertGreaterEqual(pages, 1)
        import fitz
        with fitz.open(dst) as pdf:
            self.assertIn('Word COM', pdf[0].get_text())

    def test_excel_com_to_pdf_if_available(self):
        """Excel COM 实测（本机装有 Office 时执行，无 COM 则跳过）"""
        try:
            import win32com.client
            app = win32com.client.Dispatch('Excel.Application')
            _ = app.Version  # 探测可用性（不 Quit）
        except (ImportError, Exception):
            self.skipTest('Excel COM 不可用')
        import openpyxl
        xlsx_path = os.path.join(self.tmp, '表格.xlsx')
        wb = openpyxl.Workbook()
        wb.active['A1'] = 'ExcelCOM实测'
        wb.save(xlsx_path)
        dst = os.path.join(self.tmp, '表格.pdf')
        pages, _report = to_pdf.convert_excel_to_pdf(xlsx_path, dst)
        self.assertGreaterEqual(pages, 1)
        import fitz
        with fitz.open(dst) as pdf:
            self.assertIn('ExcelCOM', pdf[0].get_text())


# ==================== 3. 批量：独立模式 ====================
class TestBatchIndividual(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, 'out')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_individual_naming_and_traceability(self):
        f1 = _make_pdf(os.path.join(self.tmp, '甲.pdf'), ['甲'])
        f2 = _make_png(os.path.join(self.tmp, '乙.png'))
        f3 = _make_txt(os.path.join(self.tmp, '丙.txt'), '丙内容')
        results, skipped = to_pdf.batch_to_pdf(
            [f1, f2, f3], self.out, output_mode='individual')
        self.assertEqual(len(skipped), 0)
        self.assertEqual(len(results), 3)
        names = [r['out_name'] for r in results]
        self.assertEqual(names, ['甲.pdf', '乙.pdf', '丙.pdf'])
        self.assertEqual([r['action'] for r in results], ['saved'] * 3)
        for r in results:
            self.assertTrue(os.path.isfile(r['out_path']))

    def test_individual_duplicate_names_get_sequence(self):
        # 两个不同目录下的同名文件
        d1 = os.path.join(self.tmp, 'd1')
        d2 = os.path.join(self.tmp, 'd2')
        os.makedirs(d1); os.makedirs(d2)
        f1 = _make_pdf(os.path.join(d1, '同名.pdf'), ['one'])
        f2 = _make_pdf(os.path.join(d2, '同名.pdf'), ['two'])
        results, skipped = to_pdf.batch_to_pdf(
            [f1, f2], self.out, output_mode='individual')
        self.assertEqual(len(skipped), 0)
        self.assertEqual(set(r['out_name'] for r in results),
                         {'同名.pdf', '同名(1).pdf'})

    def test_failures_do_not_break_batch(self):
        good = _make_txt(os.path.join(self.tmp, '好.txt'), '好')
        empty = os.path.join(self.tmp, '空.txt')
        open(empty, 'w').close()
        bad = os.path.join(self.tmp, '坏.exe')
        with open(bad, 'wb') as f:
            f.write(b'\x00\x01')
        results, skipped = to_pdf.batch_to_pdf(
            [good, empty, bad], self.out, output_mode='individual')
        self.assertEqual(len(results), 1)   # 好文件成功
        self.assertEqual(results[0]['out_name'], '好.pdf')
        self.assertEqual(len(skipped), 2)   # 空/坏文件有明确原因
        reasons = {s['name']: s['reason'] for s in skipped}
        self.assertIn('空文件', reasons['空.txt'])
        self.assertIn('不支持的格式', reasons['坏.exe'])


# ==================== 4. 批量：合并模式 ====================
class TestBatchMerge(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, 'out')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_merge_order_preserved(self):
        """按传入顺序合并：页序 = 文件顺序 × 页序"""
        import fitz
        f1 = _make_pdf(os.path.join(self.tmp, '一.pdf'), ['P1-A', 'P1-B'])
        f2 = _make_png(os.path.join(self.tmp, '二.png'))
        f3 = _make_txt(os.path.join(self.tmp, '三.txt'), '三的内容')
        results, skipped = to_pdf.batch_to_pdf(
            [f1, f2, f3], self.out, output_mode='merge')
        self.assertEqual(len(skipped), 0)
        self.assertEqual(len(results), 3)
        self.assertEqual([r['action'] for r in results], ['merged'] * 3)
        merged = os.path.join(self.out, '合并结果.pdf')
        self.assertTrue(os.path.isfile(merged))
        with fitz.open(merged) as d:
            # 2页PDF + 1页图片 + 1页文本 = 4 页
            self.assertEqual(d.page_count, 4)
            self.assertIn('P1-A', d[0].get_text())
            self.assertIn('P1-B', d[1].get_text())
            self.assertIn('三的内容', d[3].get_text())

    def test_merge_failure_excluded_but_batch_completes(self):
        import fitz
        f1 = _make_pdf(os.path.join(self.tmp, '一.pdf'), ['一'])
        empty = os.path.join(self.tmp, '空.png')
        open(empty, 'wb').close()  # 空文件 → 失败
        f3 = _make_pdf(os.path.join(self.tmp, '三.pdf'), ['三'])
        results, skipped = to_pdf.batch_to_pdf(
            [f1, empty, f3], self.out, output_mode='merge')
        self.assertEqual(len(results), 2)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]['name'], '空.png')
        with fitz.open(os.path.join(self.out, '合并结果.pdf')) as d:
            self.assertEqual(d.page_count, 2)
            self.assertIn('一', d[0].get_text())
            self.assertIn('三', d[1].get_text())

    def test_progress_callback_invoked(self):
        f1 = _make_pdf(os.path.join(self.tmp, 'a.pdf'), ['a'])
        f2 = _make_pdf(os.path.join(self.tmp, 'b.pdf'), ['b'])
        calls = []
        to_pdf.batch_to_pdf([f1, f2], self.out, output_mode='merge',
                            progress_callback=lambda c, t: calls.append((c, t)))
        self.assertEqual(calls, [(1, 2), (2, 2)])


# ==================== 5. 双向隔离：原有 pdf2word 流程不变 ====================
class TestDirectionIsolation(unittest.TestCase):

    def test_converter_module_untouched(self):
        """converter.py 原有入口保持原样"""
        self.assertTrue(hasattr(converter, 'convert_pdf_to_docx'))
        self.assertTrue(hasattr(converter, 'batch_convert'))
        src = os.path.join(os.path.dirname(converter.__file__), 'converter.py')
        with open(src, encoding='utf-8') as f:
            code = f.read()
        self.assertIn('def convert_pdf_to_docx(', code)
        self.assertIn('def batch_convert(', code)

    def test_to_pdf_does_not_touch_pdf2docx(self):
        """to_pdf 引擎不导入 pdf2docx（两方向逻辑隔离）"""
        src = os.path.join(os.path.dirname(to_pdf.__file__), 'to_pdf.py')
        with open(src, encoding='utf-8') as f:
            code = f.read()
        self.assertNotIn('pdf2docx', code)
        self.assertNotIn('convert_pdf_to_docx', code)

    def test_original_pdf2word_flow_regression(self):
        """原有方向功能回归：PDF 仍可转 Word，输出 .docx"""
        tmp = tempfile.mkdtemp()
        try:
            pdf = _make_pdf(os.path.join(tmp, '原.pdf'), ['原有方向回归'])
            out_dir = os.path.join(tmp, 'out')
            results = converter.batch_convert([pdf], out_dir)
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0]['ok'], results[0].get('error'))
            self.assertTrue(results[0]['docx_name'].endswith('.docx'))
            self.assertTrue(os.path.isfile(
                os.path.join(out_dir, results[0]['docx_name'])))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ==================== 6. 端点：方向分发与校验 ====================
class TestEndpoints(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_upload = bp.UPLOAD_DIR
        self._old_output = bp.OUTPUT_DIR
        bp.UPLOAD_DIR = os.path.join(self.tmp, 'uploads')
        bp.OUTPUT_DIR = os.path.join(self.tmp, 'outputs')
        os.makedirs(bp.UPLOAD_DIR, exist_ok=True)
        os.makedirs(bp.OUTPUT_DIR, exist_ok=True)
        self.c = flask_app.test_client()
        with self.c.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'tester'

    def tearDown(self):
        bp.UPLOAD_DIR = self._old_upload
        bp.OUTPUT_DIR = self._old_output
        with bp.tasks_lock:
            bp.tasks.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _upload(self, files, direction=None, output_mode=None):
        """files: [('files', (BytesIO, filename)), ...] 兼容当前 werkzeug"""
        data = {}
        if direction:
            data['direction'] = direction
        if output_mode:
            data['output_mode'] = output_mode
        if files:
            # 按字段名分组：{'files': [(stream, name), ...]}
            grouped = {}
            for key, val in files:
                grouped.setdefault(key, []).append(val)
            data.update(grouped)
        else:
            # 显式空上传
            data['files'] = (io.BytesIO(b''), '')
        return self.c.post('/pdf2word/api/upload', data=data,
                           content_type='multipart/form-data')

    def _wait_done(self, task_id, timeout=30):
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            with bp.tasks_lock:
                st = bp.tasks.get(task_id, {}).get('status')
            if st in ('done', 'error'):
                return st
            time.sleep(0.2)
        return 'timeout'

    def test_invalid_direction_rejected(self):
        r = self._upload({}, direction='sideways')
        self.assertEqual(r.status_code, 400)

    def test_empty_upload_rejected(self):
        r = self._upload({})
        self.assertEqual(r.status_code, 400)

    def test_pdf2word_direction_rejects_non_pdf(self):
        png = io.BytesIO(b'\x89PNG\r\n\x1a\n')
        r = self._upload([('files', (png, '图.png'))], direction='pdf2word')
        self.assertEqual(r.status_code, 400)
        data = r.get_json()
        self.assertIn('skipped', data)

    def test_topdf_merge_endpoint_flow(self):
        """topdf 方向：图片+文本合并为单个 PDF，result 携带方向与模式"""
        png_path = _make_png(os.path.join(self.tmp, '图.png'))
        txt_path = _make_txt(os.path.join(self.tmp, '文.txt'), '端点测试内容')
        with open(png_path, 'rb') as f:
            png_bytes = f.read()
        with open(txt_path, 'rb') as f:
            txt_bytes = f.read()
        r = self._upload([
            ('files', (io.BytesIO(png_bytes), '图.png')),
            ('files', (io.BytesIO(txt_bytes), '文.txt')),
        ], direction='topdf', output_mode='merge')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        task_id = r.get_json()['task_id']
        self.assertEqual(self._wait_done(task_id), 'done')
        res = self.c.get(f'/pdf2word/api/result/{task_id}')
        data = res.get_json()
        self.assertEqual(data['direction'], 'topdf')
        self.assertEqual(data['output_mode'], 'merge')
        self.assertEqual(len(data['results']), 2)
        self.assertEqual(data['results'][0]['out_name'], '合并结果.pdf')
        merged = os.path.join(bp.OUTPUT_DIR, task_id, '合并结果.pdf')
        self.assertTrue(os.path.isfile(merged))

    def test_topdf_individual_with_failure_summary(self):
        """独立模式 + 空文件：失败清单含文件名与原因，成功文件不受影响"""
        txt_bytes = b'\xe6\x88\x90\xe5\x8a\x9f\xe6\x96\x87\xe4\xbb\xb6'  # 成功文件
        r = self._upload([
            ('files', (io.BytesIO(txt_bytes), '成功.txt')),
            ('files', (io.BytesIO(b''), '空.txt')),  # 0 字节 → 保存后转换失败
        ], direction='topdf', output_mode='individual')
        self.assertEqual(r.status_code, 200)
        task_id = r.get_json()['task_id']
        self.assertEqual(self._wait_done(task_id), 'done')
        data = self.c.get(f'/pdf2word/api/result/{task_id}').get_json()
        self.assertEqual(data['output_mode'], 'individual')
        self.assertEqual(len(data['results']), 1)
        self.assertEqual(data['results'][0]['out_name'], '成功.pdf')
        self.assertEqual(len(data['skipped']), 1)
        self.assertEqual(data['skipped'][0]['name'], '空.txt')
        self.assertIn('空文件', data['skipped'][0]['reason'])

    def test_pick_folder_requires_folder_selection(self):
        """pick_folder 端点已注册（用户取消 → cancelled）"""
        # tkinter 对话框无法在无人值守环境弹出，打桩为返回取消
        import tkinter.filedialog as fd
        orig = fd.askdirectory
        fd.askdirectory = lambda **kw: ''
        try:
            r = self.c.post('/pdf2word/api/pick_folder')
        finally:
            fd.askdirectory = orig
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get('cancelled'))

    def test_pick_folder_recursive_parse(self):
        """文件夹递归解析：子目录文件按序收集，仅保留支持格式"""
        folder = os.path.join(self.tmp, 'fold')
        sub = os.path.join(folder, '子目录')
        os.makedirs(sub)
        _make_pdf(os.path.join(folder, '甲.pdf'), ['甲'])
        _make_txt(os.path.join(sub, '乙.txt'), '乙')
        _make_txt(os.path.join(folder, '丙.txt'), '丙')
        with open(os.path.join(folder, '跳过.exe'), 'wb') as f:
            f.write(b'MZ')
        import tkinter.filedialog as fd
        orig = fd.askdirectory
        fd.askdirectory = lambda **kw: folder
        try:
            r = self.c.post('/pdf2word/api/pick_folder')
        finally:
            fd.askdirectory = orig
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        data = r.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['file_count'], 3)
        self.assertEqual(data['unsupported_count'], 1)
        self.assertEqual(data['files'], ['丙.txt', '甲.pdf', '乙.txt'])
        # 暂存可被 upload 消费
        with bp.picked_folders_lock:
            self.assertIn(data['pick_id'], bp.picked_folders)
        # 清理暂存，避免影响其他用例
        with bp.picked_folders_lock:
            bp.picked_folders.pop(data['pick_id'], None)

    def test_upload_consumes_all_pick_ids(self):
        """v2.1.1 回归：多个 pick_ids 必须全部消费（此前 get() 只取第一个，
        其余文件夹被静默丢弃——用户 7 个文件夹 461 个文件未转换的根因）"""
        import io as _io
        from PIL import Image
        pick_ids = []
        for i in range(3):
            folder = os.path.join(self.tmp, 'pf%d' % i)
            os.makedirs(folder)
            files = []
            for j in range(2):
                fp = os.path.join(folder, 'img%d.png' % j)
                Image.new('RGB', (60, 40), (i * 40, j * 40, 0)).save(fp)
                files.append(fp)
            pid = 'ut_pick_%d' % i
            with bp.picked_folders_lock:
                bp.picked_folders[pid] = {'folder': folder, 'files': files}
            pick_ids.append(pid)

        buf = _io.BytesIO()
        Image.new('RGB', (60, 40), (1, 2, 3)).save(buf, 'PNG')
        r = self.c.post('/pdf2word/api/upload', data={
            'direction': 'topdf', 'output_mode': 'individual',
            'files': [(buf, 'direct.png')],
            'pick_ids': pick_ids,   # 同名字段多值（与前端逐个 append 一致）
        }, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.get_json())
        resp = r.get_json()
        # 1 直接文件 + 3 文件夹 × 2 = 7，全部进入任务
        self.assertEqual(resp['total_files'], 7, resp)
        # 服务端暂存全部消费，无残留
        with bp.picked_folders_lock:
            for pid in pick_ids:
                self.assertNotIn(pid, bp.picked_folders)

    def test_upload_invalid_pick_id_warns(self):
        """v2.1.1：失效的 pick（已消费/重启后残留引用）明确提示，不静默跳过"""
        import io as _io
        from PIL import Image
        buf = _io.BytesIO()
        Image.new('RGB', (60, 40), (1, 2, 3)).save(buf, 'PNG')
        r = self.c.post('/pdf2word/api/upload', data={
            'direction': 'topdf', 'output_mode': 'individual',
            'files': [(buf, 'x.png')],
            'pick_ids': ['pid_not_exist_123'],
        }, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.get_json())
        skipped = r.get_json()['skipped']
        self.assertEqual(len(skipped), 1)
        self.assertIn('已失效', skipped[0]['reason'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
