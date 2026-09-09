# -*- coding: utf-8 -*-
"""v2.3.2 测试

1. 合同整理页两卡片展开/折叠：默认折叠、按钮图标+文字反馈、状态相互独立
2. 社保 OCR 并行化：线程本地引擎、结果按原序归位、错误隔离、取消/暂停语义
3. MCP 三工具选择项提示：合同两阶段（preview_only 预览 → confirm_task_id+choices
   确认执行，重名归属候选内联）+ 三工具 effective_params 生效参数回显
"""
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.mcp.core import adapters, artifacts, tasks as mcp_tasks, tools
from modules.insurance import blueprint as ins_bp
from modules.insurance.core import ocr_engine

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT_DIR = os.path.join(PROJECT, 'modules', 'contract')


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def _payload(text):
    return json.loads(text)


# ==================== 1. 合同页折叠（静态断言） ====================

class TestContractCollapseUI(unittest.TestCase):
    """上传花名册/上传文件两卡片：默认折叠 + 展开/折叠按钮 + 状态独立"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(CONTRACT_DIR, 'templates', 'contract_index.html'),
                  encoding='utf-8') as f:
            cls.html = f.read()
        with open(os.path.join(CONTRACT_DIR, 'static', 'css', 'style.css'),
                  encoding='utf-8') as f:
            cls.css = f.read()
        with open(os.path.join(CONTRACT_DIR, 'static', 'js', 'app.js'),
                  encoding='utf-8') as f:
            cls.js = f.read()

    def test_both_sections_default_collapsed(self):
        """两卡片页面加载即折叠（collapsed class 直接渲染，避免加载闪烁）"""
        self.assertIn('<section class="card collapsible collapsed" id="rosterSection">',
                      self.html)
        self.assertIn('<section class="card collapsible collapsed" id="uploadSection">',
                      self.html)

    def test_collapse_buttons_present_with_feedback(self):
        """两卡片头部各有折叠按钮：aria-expanded=false + 默认文字"展开" """
        for btn_id in ('rosterCollapseBtn', 'uploadCollapseBtn'):
            self.assertIn('id="%s"' % btn_id, self.html)
        self.assertEqual(self.html.count('class="collapse-btn"'), 2)
        self.assertEqual(self.html.count('aria-expanded="false"'), 2)
        self.assertEqual(self.html.count('<span class="collapse-text">展开</span>'), 2)

    def test_buttons_toggle_independent_sections(self):
        """两个按钮分别绑定各自 section（状态互不干扰的结构前提）"""
        self.assertIn("toggleSection('rosterSection', this)", self.html)
        self.assertIn("toggleSection('uploadSection', this)", self.html)

    def test_css_collapsed_hides_body(self):
        self.assertIn('.card.collapsed > .card-body', self.css)
        self.assertIn('display: none', self.css)
        self.assertIn('.collapse-btn', self.css)

    def test_js_toggle_function(self):
        """toggleSection：切换 collapsed class + 图标 ▸/▾ + 文字 展开/折叠 反馈"""
        self.assertIn('function toggleSection(sectionId, btn)', self.js)
        self.assertIn("classList.toggle('collapsed')", self.js)
        self.assertIn("'▸'", self.js)
        self.assertIn("'▾'", self.js)
        self.assertIn("'展开'", self.js)
        self.assertIn("'折叠'", self.js)
        self.assertIn("setAttribute('aria-expanded'", self.js)

    def test_upload_elements_intact(self):
        """折叠改造不影响上传功能：关键上传元素全部保留"""
        for marker in ('id="rosterFileInput"', 'id="rosterUploadBox"',
                       'id="dropzone"', 'id="fileInput"', 'pickFolder()',
                       'id="fileList"', 'onclick="startProcess()"'):
            self.assertIn(marker, self.html)


# ==================== 2. 并行 OCR ====================

class TestThreadLocalEngine(unittest.TestCase):
    """get_engine 线程本地单例：同线程复用、跨线程独立"""

    def setUp(self):
        if hasattr(ocr_engine._engines, 'engine'):
            del ocr_engine._engines.engine
        # v2.3.3：_ocr_all_items 会设置模块级限核值，测试间必须隔离，
        # 否则 FakeRapidOCR（不收 kwargs）会被限核参数砸中
        self._orig_threads = ocr_engine._intra_op_threads
        ocr_engine._intra_op_threads = None

    def tearDown(self):
        if hasattr(ocr_engine._engines, 'engine'):
            del ocr_engine._engines.engine
        ocr_engine._intra_op_threads = self._orig_threads

    def test_per_thread_instances(self):
        created = []

        class FakeRapidOCR:
            def __init__(self):
                created.append(self)

        fake_mod = types.ModuleType('rapidocr_onnxruntime')
        fake_mod.RapidOCR = FakeRapidOCR
        with mock.patch.dict(sys.modules, {'rapidocr_onnxruntime': fake_mod}):
            e1 = ocr_engine.get_engine()
            self.assertIs(e1, ocr_engine.get_engine())  # 同线程复用
            holder = []
            t = threading.Thread(target=lambda: holder.append(ocr_engine.get_engine()))
            t.start()
            t.join()
            self.assertEqual(len(holder), 1)
            self.assertIsNot(holder[0], e1)             # 跨线程独立实例
            self.assertEqual(len(created), 2)           # 每线程各加载一次


class TestParallelOcrItems(unittest.TestCase):
    """_ocr_all_items：并行识别 + 原序归位 + 错误隔离 + 取消/暂停"""

    def setUp(self):
        self.task_id = 'testv232ocr'
        with ins_bp.tasks_lock:
            ins_bp.tasks[self.task_id] = {
                'status': 'processing', 'current': 0, 'total': 0,
                'message': '', 'cancelled': False, 'paused': False,
            }
        self._orig_workers = ins_bp.OCR_MAX_WORKERS
        ins_bp.OCR_MAX_WORKERS = 3

    def tearDown(self):
        ins_bp.OCR_MAX_WORKERS = self._orig_workers
        with ins_bp.tasks_lock:
            ins_bp.tasks.pop(self.task_id, None)

    @staticmethod
    def _items(n):
        return [('name%d.jpg' % i, 'path%d' % i, 'origin%d' % i) for i in range(n)]

    def test_results_keep_input_order_under_parallel(self):
        """完成顺序打乱时，结果仍按 all_items 原序归位"""
        def fake_parse(fp, province_code=None):
            idx = int(fp.replace('path', ''))
            time.sleep((8 - idx) * 0.02)  # 后面的先完成
            return {'insurance_type': '养老', 'name': 'n%d' % idx,
                    'period': None, 'company_name': ''}

        with mock.patch.object(ins_bp, 'parse_ocr_result_from_image', fake_parse):
            results = ins_bp._ocr_all_items(self.task_id, self._items(8), None)

        self.assertEqual(len(results), 8)
        for i, r in enumerate(results):
            self.assertEqual(r['filename'], 'name%d.jpg' % i)
            self.assertEqual(r['_source_path'], 'path%d' % i)
            self.assertEqual(r['_source_origin'], 'origin%d' % i)
            self.assertNotIn('error', r)
        with ins_bp.tasks_lock:
            self.assertEqual(ins_bp.tasks[self.task_id]['current'], 8)

    def test_error_isolated_to_single_image(self):
        """单张识别异常不影响其他图片，错误记录在原位"""
        def fake_parse(fp, province_code=None):
            if fp == 'path3':
                raise RuntimeError('引擎崩溃')
            return {'insurance_type': None, 'name': 'x', 'period': None,
                    'company_name': ''}

        with mock.patch.object(ins_bp, 'parse_ocr_result_from_image', fake_parse):
            results = ins_bp._ocr_all_items(self.task_id, self._items(6), None)

        self.assertEqual(len(results), 6)
        self.assertEqual(results[3]['error'], '引擎崩溃')
        self.assertEqual(results[3]['filename'], 'name3.jpg')
        for i in (0, 1, 2, 4, 5):
            self.assertNotIn('error', results[i])

    def test_cancel_stops_and_returns_none(self):
        """识别中取消：停止派新活、状态回写 cancelled、返回 None"""
        def fake_parse(fp, province_code=None):
            if fp == 'path0':
                with ins_bp.tasks_lock:
                    ins_bp.tasks[self.task_id]['cancelled'] = True
            time.sleep(0.01)
            return {'insurance_type': None, 'name': 'x', 'period': None,
                    'company_name': ''}

        with mock.patch.object(ins_bp, 'parse_ocr_result_from_image', fake_parse):
            results = ins_bp._ocr_all_items(self.task_id, self._items(10), None)

        self.assertIsNone(results)
        with ins_bp.tasks_lock:
            self.assertEqual(ins_bp.tasks[self.task_id]['status'], 'cancelled')

    def test_pause_blocks_new_dispatch_until_resumed(self):
        """暂停时不再派新活；恢复后继续完成全部"""
        with ins_bp.tasks_lock:
            ins_bp.tasks[self.task_id]['paused'] = True

        def fake_parse(fp, province_code=None):
            return {'insurance_type': None, 'name': 'x', 'period': None,
                    'company_name': ''}

        holder = {}

        def run():
            with mock.patch.object(ins_bp, 'parse_ocr_result_from_image', fake_parse):
                holder['results'] = ins_bp._ocr_all_items(
                    self.task_id, self._items(4), None)

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.4)  # 暂停中：不应有任何进展
        with ins_bp.tasks_lock:
            self.assertEqual(ins_bp.tasks[self.task_id]['current'], 0)
        self.assertNotIn('results', holder)
        with ins_bp.tasks_lock:
            ins_bp.tasks[self.task_id]['paused'] = False
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(holder['results']), 4)


# ==================== 3. MCP 选择项提示 ====================

class TestContractTwoPhase(unittest.TestCase):
    """合同整理两阶段：预览返回重名待确认清单 → choices 确认执行"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_a = artifacts.data_dir
        self._orig_b = adapters.data_dir
        artifacts.data_dir = lambda: self.tmp
        adapters.data_dir = lambda: self.tmp
        self.src = os.path.join(self.tmp, 'src')
        os.makedirs(self.src)
        self.names = ['张三.jpg', '李四.jpg', '王五.jpg']
        for n in self.names:
            with open(os.path.join(self.src, n), 'wb') as f:
                f.write(b'x')
        self.file_paths = [os.path.join(self.src, n) for n in self.names]
        self.roster = os.path.join(self.tmp, 'roster.csv')
        with open(self.roster, 'w', encoding='utf-8-sig') as f:
            f.write('序号,姓名,身份证号\n'
                    '1,张三,110101199001010011\n'
                    '2,李四,11010119900101002X\n'
                    '3,王五,110101199001010038\n'
                    '4,王五,110101199001010046\n')
        self.created_tasks = []

    def tearDown(self):
        artifacts.data_dir = self._orig_a
        adapters.data_dir = self._orig_b
        for tid in self.created_tasks:
            with mcp_tasks._LOCK:
                mcp_tasks._TASKS.pop(tid, None)
        _rmtree(self.tmp)

    def _preview(self):
        text, is_err = tools.tool_contract_organize({
            'file_paths': self.file_paths, 'roster_path': self.roster,
            'preview_only': True})
        self.assertFalse(is_err, text)
        data = _payload(text)
        self.created_tasks.append(data['task_id'])
        return data

    def _confirm(self, task_id, choices=None):
        args = {'confirm_task_id': task_id}
        if choices is not None:
            args['choices'] = choices
        text, is_err = tools.tool_contract_organize(args)
        self.assertFalse(is_err, text)
        deadline = time.time() + 15
        while time.time() < deadline:
            task = mcp_tasks.get(task_id)
            if task.get('status') in ('success', 'error'):
                return task
            time.sleep(0.1)
        self.fail('确认执行超时')

    def test_preview_returns_selection_items_without_executing(self):
        """预览：状态 waiting_confirm + 重名待确认（含候选归属人）内联，源文件不动"""
        data = self._preview()
        self.assertEqual(data['status'], 'waiting_confirm')
        self.assertEqual(data['summary']['auto'], 2)
        self.assertEqual(data['summary']['duplicates'], 1)
        self.assertEqual(len(data['needs_selection']), 1)
        item = data['needs_selection'][0]
        self.assertEqual(item['original'], '王五.jpg')
        seqs = sorted(c['seq'] for c in item['candidates'])
        self.assertEqual(seqs, [3, 4])
        self.assertIn('confirm_task_id=%s' % data['task_id'], data['how_to_confirm'])
        self.assertTrue(data['artifacts'])  # 完整计划落盘卡片
        # 未执行：源文件原样，输出目录没有重命名产物
        for n in self.names:
            self.assertTrue(os.path.isfile(os.path.join(self.src, n)))

    def test_confirm_with_choices_resolves_duplicate(self):
        """确认：choices 选 seq=4 的候选人 → 王五.jpg 按 04 号重命名"""
        data = self._preview()
        task = self._confirm(data['task_id'],
                             [{'original': '王五.jpg', 'seq': 4}])
        self.assertEqual(task.get('status'), 'success', task.get('error'))
        summary = (task.get('result') or {}).get('summary') or {}
        self.assertEqual(summary.get('renamed'), 3)
        self.assertEqual(summary.get('pending'), 0)
        out = summary.get('output_dir')
        out_files = sorted(os.listdir(out))
        self.assertIn('01-张三-0011.jpg', out_files)
        self.assertIn('02-李四-002X.jpg', out_files)
        self.assertIn('04-王五-0046.jpg', out_files)

    def test_confirm_without_choices_sends_duplicate_to_pending(self):
        """确认但不选归属：重名项移入「待处理」，auto 两项照常重命名"""
        data = self._preview()
        task = self._confirm(data['task_id'])
        self.assertEqual(task.get('status'), 'success', task.get('error'))
        summary = (task.get('result') or {}).get('summary') or {}
        self.assertEqual(summary.get('renamed'), 2)
        self.assertEqual(summary.get('pending'), 1)
        out = summary.get('output_dir')
        pending_dir = os.path.join(out, '待处理')
        self.assertTrue(os.path.isdir(pending_dir))
        self.assertIn('王五.jpg', os.listdir(pending_dir))

    def test_confirm_rejects_wrong_status(self):
        """非 waiting_confirm 状态的任务不可确认"""
        task_id = mcp_tasks.create('contract', {})
        self.created_tasks.append(task_id)
        text, is_err = tools.tool_contract_organize({'confirm_task_id': task_id})
        self.assertTrue(is_err)
        self.assertIn('waiting_confirm', text)

    def test_confirm_rejects_unknown_task(self):
        text, is_err = tools.tool_contract_organize({'confirm_task_id': 'deadbeef'})
        self.assertTrue(is_err)
        self.assertIn('不存在', text)

    def test_get_task_result_guides_when_waiting_confirm(self):
        """waiting_confirm 状态取结果：明确"待确认"并引导 confirm_task_id"""
        data = self._preview()
        text, is_err = tools.tool_get_task_result({'task_id': data['task_id']})
        self.assertTrue(is_err)
        payload = _payload(text)
        self.assertIn('待确认', payload['error'])
        self.assertIn('confirm_task_id=%s' % data['task_id'],
                      payload['suggestion'])


class TestBuildConfirmedRenames(unittest.TestCase):
    """build_confirmed_renames 纯函数：覆盖/归属/待处理合成"""

    PLAN = {
        'auto': [{'original': 'a.jpg', 'new_name': '01-甲-0011.jpg', 'seq': 1}],
        'duplicates': [
            {'original': 'b.jpg', 'guessed': '乙', 'reason': '重名',
             'candidates': [{'seq': 2, 'name': '乙', 'idcard_tail': '0022'},
                            {'seq': 3, 'name': '乙', 'idcard_tail': '0033'}]},
            {'original': 'c.jpg', 'guessed': '丙', 'reason': '重名',
             'candidates': [{'seq': 4, 'name': '丙', 'idcard_tail': ''}]},
        ],
        'unmatched': [{'original': 'd.jpg', 'guessed': '', 'reason': '未匹配'}],
    }

    def test_override_auto_new_name(self):
        renames, pending = adapters.build_confirmed_renames(
            self.PLAN, [{'original': 'a.jpg', 'new_name': '自定义.jpg'},
                        {'original': 'b.jpg', 'seq': 3},
                        {'original': 'c.jpg', 'seq': 4}])
        by_orig = {r['original']: r for r in renames}
        self.assertEqual(by_orig['a.jpg']['new_name'], '自定义.jpg')
        self.assertEqual(by_orig['b.jpg']['new_name'], '03-乙-0033.jpg')
        self.assertEqual(by_orig['c.jpg']['new_name'], '04-丙.jpg')  # 无证号尾不带
        self.assertEqual(pending, ['d.jpg'])

    def test_unresolved_duplicate_goes_pending(self):
        renames, pending = adapters.build_confirmed_renames(self.PLAN, [])
        self.assertEqual([r['original'] for r in renames], ['a.jpg'])
        self.assertEqual(sorted(pending), ['b.jpg', 'c.jpg', 'd.jpg'])

    def test_seq_not_in_candidates_goes_pending(self):
        renames, pending = adapters.build_confirmed_renames(
            self.PLAN, [{'original': 'b.jpg', 'seq': 99}])
        self.assertEqual([r['original'] for r in renames], ['a.jpg'])
        self.assertIn('b.jpg', pending)


class TestEffectiveParamsEcho(unittest.TestCase):
    """三工具提交响应回显 effective_params（平台上需要选择的项可见）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_a = artifacts.data_dir
        self._orig_b = adapters.data_dir
        artifacts.data_dir = lambda: self.tmp
        adapters.data_dir = lambda: self.tmp
        self.img = os.path.join(self.tmp, 'p.jpg')
        with open(self.img, 'wb') as f:
            f.write(b'x')
        self.created_tasks = []

    def tearDown(self):
        artifacts.data_dir = self._orig_a
        adapters.data_dir = self._orig_b
        for tid in self.created_tasks:
            with mcp_tasks._LOCK:
                mcp_tasks._TASKS.pop(tid, None)
        _rmtree(self.tmp)

    def _province_code(self):
        from modules.insurance.core import template_engine
        return template_engine.get_provinces()[0]['province_code']

    def test_insurance_submit_echoes_effective_params(self):
        with mock.patch.object(adapters, 'start_insurance', lambda *a, **k: None):
            text, is_err = tools.tool_insurance_calculate({
                'file_paths': [self.img], 'province': self._province_code()})
        self.assertFalse(is_err, text)
        data = _payload(text)
        self.created_tasks.append(data['task_id'])
        eff = data.get('effective_params')
        self.assertIsNotNone(eff)
        self.assertEqual(eff['province'], self._province_code())
        self.assertEqual(eff['tax_mode'], '退税')
        self.assertEqual(eff['roster_path'], '不使用')
        self.assertEqual(eff['year_range'], '参保数据全区间')

    def test_convert_to_pdf_echoes_output_mode(self):
        with mock.patch.object(adapters, 'start_pdf2word', lambda *a, **k: None):
            text, is_err = tools.tool_convert_to_pdf({
                'file_paths': [self.img], 'output_mode': 'merge'})
        self.assertFalse(is_err, text)
        data = _payload(text)
        self.created_tasks.append(data['task_id'])
        eff = data.get('effective_params')
        self.assertIsNotNone(eff)
        self.assertEqual(eff['direction'], 'topdf')
        self.assertEqual(eff['output_mode'], 'merge')

    def test_contract_submit_echoes_effective_params(self):
        roster = os.path.join(self.tmp, 'r.csv')
        with open(roster, 'w', encoding='utf-8-sig') as f:
            f.write('序号,姓名\n1,张三\n')
        with mock.patch.object(adapters, 'start_contract', lambda *a, **k: None):
            text, is_err = tools.tool_contract_organize({
                'file_paths': [self.img], 'roster_path': roster})
        self.assertFalse(is_err, text)
        data = _payload(text)
        self.created_tasks.append(data['task_id'])
        eff = data.get('effective_params')
        self.assertIsNotNone(eff)
        self.assertEqual(eff['roster_path'], roster)
        self.assertFalse(eff['preview_only'])


if __name__ == '__main__':
    unittest.main()


# ==================== 4. v2.3.3 OCR 线程数封顶 ====================

class TestIntraOpThreadCap(unittest.TestCase):
    """onnxruntime 默认每会话吃满全核，并行 N 引擎 = N×核数 线程抢核；
    set_intra_op_threads 后 get_engine 必须把 det/cls/rec 三路限核传下去"""

    def setUp(self):
        if hasattr(ocr_engine._engines, 'engine'):
            del ocr_engine._engines.engine
        self._orig = ocr_engine._intra_op_threads

    def tearDown(self):
        ocr_engine.set_intra_op_threads(self._orig)
        if hasattr(ocr_engine._engines, 'engine'):
            del ocr_engine._engines.engine

    def _fake_rapidocr(self, holder):
        class FakeRapidOCR:
            def __init__(self, **kwargs):
                holder.append(kwargs)
        fake_mod = types.ModuleType('rapidocr_onnxruntime')
        fake_mod.RapidOCR = FakeRapidOCR
        return fake_mod

    def test_cap_threads_passed_to_engine(self):
        holder = []
        ocr_engine.set_intra_op_threads(2)
        with mock.patch.dict(sys.modules, {'rapidocr_onnxruntime': self._fake_rapidocr(holder)}):
            ocr_engine.get_engine()
        self.assertEqual(holder, [{'det_intra_op_num_threads': 2,
                                   'cls_intra_op_num_threads': 2,
                                   'rec_intra_op_num_threads': 2}])

    def test_no_cap_means_no_kwargs(self):
        holder = []
        ocr_engine.set_intra_op_threads(None)
        with mock.patch.dict(sys.modules, {'rapidocr_onnxruntime': self._fake_rapidocr(holder)}):
            ocr_engine.get_engine()
        self.assertEqual(holder, [{}])

    def test_dispatch_sets_threads_from_cpu_and_workers(self):
        """_ocr_all_items 起池前按 核数//workers 设置限核"""
        recorded = []
        orig_set = ocr_engine.set_intra_op_threads
        ocr_engine.set_intra_op_threads = lambda n: recorded.append(n)

        task_id = 'testv233cap'
        with ins_bp.tasks_lock:
            ins_bp.tasks[task_id] = {'status': 'processing', 'current': 0,
                                     'total': 0, 'message': '',
                                     'cancelled': False, 'paused': False}
        orig_workers = ins_bp.OCR_MAX_WORKERS
        ins_bp.OCR_MAX_WORKERS = 4
        try:
            fake_parse = lambda fp, province_code=None: {
                'insurance_type': None, 'name': 'x', 'period': None,
                'company_name': ''}
            with mock.patch.object(ins_bp, 'parse_ocr_result_from_image', fake_parse):
                ins_bp._ocr_all_items(task_id, [('a.jpg', 'p0', 'o0')], None)
        finally:
            ins_bp.OCR_MAX_WORKERS = orig_workers
            ocr_engine.set_intra_op_threads = orig_set
            with ins_bp.tasks_lock:
                ins_bp.tasks.pop(task_id, None)

        import os as _os
        expect = max(1, (_os.cpu_count() or 4) // 4)
        self.assertEqual(recorded, [expect])
