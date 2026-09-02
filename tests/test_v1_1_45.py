# -*- coding: utf-8 -*-
"""v1.1.45 社保模块四项调整测试

1. Bug修复：login_required 路径判定——蓝图路径 /insurance/api/* 未登录时
   必须返回 JSON 401（原缺陷 startswith('/api/') 永远不匹配蓝图前缀，
   返回 302 HTML 导致前端 fetch 弹 "Unexpected token '<'"）
2. 异常图片处理：update_excluded_image（手动命名 new_name / 备注 remark /
   编辑后从异常列表归入正常列表，三桶搬移 + 不一致列表同步移除）
3. 图片预览端点：image_preview 按文件名返回原图
4. organize_files 手动命名：_manual_name 优先于花名册规则命名
5. 前端静态检查：折叠面板 / 搜索栏 / 处理弹窗 / apiJson 防御存在
"""
import os
import sys
import json
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app
from modules.insurance import blueprint as bp

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)

# 合法身份证号（GB 11643 校验码正确）
ID_ZHANG = '11010519491231002X'

ROSTER = [
    {'seq': 1, 'name': '张三', 'idcard': ID_ZHANG},
    {'seq': 2, 'name': '李四', 'idcard': ''},
]


def _mkimg(path, content=b'\xff\xd8fakeimg'):
    with open(path, 'wb') as f:
        f.write(content)
    return path


class TestLoginRequiredFix(unittest.TestCase):
    """1. Bug修复：蓝图 API 未登录返回 JSON 401"""

    def setUp(self):
        flask_app.config['TESTING'] = True
        self.c = flask_app.test_client()

    def test_api_json_body_401(self):
        r = self.c.post('/insurance/api/update_period/xxx', json={})
        self.assertEqual(r.status_code, 401)
        self.assertIn('application/json', r.content_type)
        data = r.get_json()
        self.assertTrue(data.get('need_login'))

    def test_api_no_accept_header_401(self):
        # 无 Accept / 无 JSON body，但路径含 /api/ → 仍应 JSON 401
        r = self.c.post('/insurance/api/update_period/xxx')
        self.assertEqual(r.status_code, 401)
        self.assertIn('application/json', r.content_type)

    def test_api_progress_401(self):
        r = self.c.get('/insurance/api/progress/xxx')
        self.assertEqual(r.status_code, 401)
        self.assertIn('application/json', r.content_type)

    def test_page_still_redirects(self):
        # 页面路由未登录仍 302 跳登录页（不能误伤页面跳转）
        r = self.c.get('/insurance/')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login', r.headers.get('Location', ''))


class TestExcludedImageBase(unittest.TestCase):
    """公共：伪造已完成任务（三桶 + 临时文件）"""

    def setUp(self):
        flask_app.config['TESTING'] = True
        self.c = flask_app.test_client()
        with self.c.session_transaction() as s:
            s['user_id'] = 1
            s['username'] = 'tester'

        self.tmp = tempfile.mkdtemp(prefix='test_v1145_')
        # 备份真实 OUTPUT_DIR，Excel/整理输出重定向到临时目录
        self._orig_output = bp.OUTPUT_DIR
        bp.OUTPUT_DIR = os.path.join(self.tmp, 'outputs')
        os.makedirs(bp.OUTPUT_DIR, exist_ok=True)

        self.task_id = 't1145'
        self.img_failed = _mkimg(os.path.join(self.tmp, 'bad.jpg'))
        self.img_mismatch = _mkimg(os.path.join(self.tmp, 'mismatch.jpg'))
        self.img_ok = _mkimg(os.path.join(self.tmp, 'ok.jpg'))

        inner = {
            '_success_results': [{
                'filename': 'ok.jpg', 'name': '张三', 'idcard': ID_ZHANG,
                'insurance_type': '养老保险', 'period': ('2023-01', '2024-06'),
                'company_name': '甲公司', '_source_path': self.img_ok,
                '_source_origin': 'ok.jpg', 'raw_text': '',
            }],
            '_excluded_results': [{
                'filename': 'mismatch.jpg', 'name': '李四', 'idcard': '',
                'insurance_type': '失业保险', 'period': ('2023-01', '2023-06'),
                'company_name': '乙公司', '_source_path': self.img_mismatch,
                '_source_origin': 'mismatch.jpg', 'raw_text': '',
                '_excluded': True,
            }],
            '_failed_results': [{
                'filename': 'bad.jpg', 'error': 'OCR识别失败', 'name': '', 'idcard': '',
                'insurance_type': None, 'period': None, 'company_name': '',
                '_source_path': self.img_failed, '_source_origin': 'bad.jpg',
                'raw_text': '',
            }],
            '_roster': [dict(r) for r in ROSTER],
            '_year_range': None,
            '_company_name': '甲公司',
            '_ocr_companies': {'甲公司': 1},
            '_company_mismatch_files': [
                {'filename': 'mismatch.jpg', 'ocr_company': '乙公司', 'expected_company': '甲公司'}],
            '_all_files': ['ok.jpg', 'mismatch.jpg', 'bad.jpg'],
            '_task_dir': self.tmp,
            '_roster_company': '',
            '_roster_source_path': '',
            '_period_overrides': {},
            '_manual_log': [],
        }
        with bp.tasks_lock:
            bp.tasks[self.task_id] = {
                'status': 'done', 'current': 3, 'total': 3,
                'message': 'done', 'files': ['ok.jpg', 'mismatch.jpg', 'bad.jpg'],
                'result': inner, 'created_at': '2026-09-02',
                'paused': False, 'cancelled': False,
            }

    def tearDown(self):
        with bp.tasks_lock:
            bp.tasks.pop(self.task_id, None)
        bp.OUTPUT_DIR = self._orig_output
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, payload):
        return self.c.post(
            f'/insurance/api/update_excluded_image/{self.task_id}',
            data=json.dumps(payload), content_type='application/json')

    def _inner(self):
        with bp.tasks_lock:
            return bp.tasks[self.task_id]['result']


class TestUpdateExcludedImage(TestExcludedImageBase):
    """2. 异常图片处理端点"""

    def test_handle_failed_image(self):
        # 识别失败图片 → 补全信息保存 → 移入正常列表
        r = self._post({'filename': 'bad.jpg', 'name': '张三', 'idcard': ID_ZHANG,
                        'insurance_type': '养老保险',
                        'start': '2022-01', 'end': '2023-12',
                        'new_name': '01-张三-手填', 'remark': '原图模糊，人工核对后补录'})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        # 失败计数归零，成功计数 +1
        self.assertEqual(data['failed_count'], 0)
        self.assertEqual(data['success_count'], 2)
        self.assertEqual(data['excluded_count'], 1)
        # 异常图片文件夹仅剩未处理的 mismatch.jpg，险种文件夹有手动命名的文件
        folders = data['organize_result']['folder_structure']
        self.assertEqual(folders.get('异常图片'), ['mismatch.jpg'])
        self.assertIn('01-张三-手填.jpg', folders.get('养老保险参保证明', []))
        # image_details 该行状态正常 + 备注回显
        det = [d for d in data['image_details'] if d['filename'] == 'bad.jpg'][0]
        self.assertFalse(det['error'])
        self.assertEqual(det['remark'], '原图模糊，人工核对后补录')
        self.assertEqual(det['manual_name'], '01-张三-手填')
        # 三桶状态
        inner = self._inner()
        self.assertEqual(len(inner['_failed_results']), 0)
        self.assertEqual(len(inner['_success_results']), 2)
        # 操作日志
        self.assertEqual(inner['_manual_log'][-1]['action'], '异常图片处理')

    def test_handle_excluded_image(self):
        # 缴费单位不一致图片 → 处理后从不一致列表移除
        r = self._post({'filename': 'mismatch.jpg', 'name': '李四',
                        'insurance_type': '失业保险',
                        'start': '2023-01', 'end': '2023-06',
                        'remark': '单位名称为子公司，已人工核实'})
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data['excluded_count'], 0)
        self.assertEqual(len(data['company_mismatch_files']), 0)
        inner = self._inner()
        self.assertEqual(len(inner['_excluded_results']), 0)
        self.assertEqual(len(inner['_company_mismatch_files']), 0)
        # 统计人数包含李四
        names = [p['name'] for p in data['person_stats']]
        self.assertIn('李四', names)

    def test_remark_only_no_period_change(self):
        # 已有时间段的记录：起止留空沿用识别值
        r = self._post({'filename': 'mismatch.jpg', 'name': '李四',
                        'insurance_type': '失业保险', 'remark': '仅补充备注'})
        self.assertEqual(r.status_code, 200)
        det = [d for d in r.get_json()['image_details'] if d['filename'] == 'mismatch.jpg'][0]
        self.assertEqual(list(det['period']), ['2023-01', '2023-06'])

    def test_validation_errors(self):
        base = {'filename': 'bad.jpg', 'insurance_type': '养老保险',
                'start': '2023-01', 'end': '2023-12'}
        cases = [
            {**base, 'name': ''},                          # 缺姓名
            {**base, 'name': 'x', 'insurance_type': '公积金'},  # 非四险
            {**base, 'name': 'x', 'start': '2023/01'},    # 年月格式
            {**base, 'name': 'x', 'end': '2022-01'},      # 起晚于止
            {**base, 'name': 'x', 'idcard': '12345'},     # 身份证格式
            {**base, 'name': 'x', 'new_name': 'a/b'},     # 非法字符清洗后非空，应成功? 单独验证
        ]
        for i, payload in enumerate(cases[:5]):
            r = self._post(payload)
            self.assertEqual(r.status_code, 400, f'case {i}: {payload}')

    def test_new_name_illegal_chars_cleaned(self):
        r = self._post({'filename': 'bad.jpg', 'name': '张三',
                        'insurance_type': '养老保险', 'start': '2023-01',
                        'end': '2023-12', 'new_name': '张:三?*' })
        self.assertEqual(r.status_code, 200)
        det = [d for d in r.get_json()['image_details'] if d['filename'] == 'bad.jpg'][0]
        self.assertEqual(det['manual_name'], '张三')

    def test_normal_image_rejected(self):
        # 正常图片不允许处理
        r = self._post({'filename': 'ok.jpg', 'name': '张三',
                        'insurance_type': '养老保险',
                        'start': '2023-01', 'end': '2024-06'})
        self.assertEqual(r.status_code, 400)

    def test_unknown_filename_404(self):
        r = self._post({'filename': 'nope.jpg', 'name': '张三',
                        'insurance_type': '养老保险',
                        'start': '2023-01', 'end': '2023-12'})
        self.assertEqual(r.status_code, 404)

    def test_failed_no_period_requires_input(self):
        # 失败记录无识别时间段：起止必须填写
        r = self._post({'filename': 'bad.jpg', 'name': '张三',
                        'insurance_type': '养老保险'})
        self.assertEqual(r.status_code, 400)


class TestImagePreview(TestExcludedImageBase):
    """3. 图片预览端点"""

    def test_preview_failed_image(self):
        r = self.c.get(f'/insurance/api/image_preview/{self.task_id}?filename=bad.jpg')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b'\xff\xd8fakeimg')

    def test_preview_excluded_image(self):
        r = self.c.get(f'/insurance/api/image_preview/{self.task_id}?filename=mismatch.jpg')
        self.assertEqual(r.status_code, 200)

    def test_preview_missing_404(self):
        r = self.c.get(f'/insurance/api/image_preview/{self.task_id}?filename=nope.jpg')
        self.assertEqual(r.status_code, 404)

    def test_preview_no_filename_400(self):
        r = self.c.get(f'/insurance/api/image_preview/{self.task_id}')
        self.assertEqual(r.status_code, 400)

    def test_preview_requires_login(self):
        c = flask_app.test_client()  # 未登录
        r = c.get(f'/insurance/api/image_preview/{self.task_id}?filename=ok.jpg')
        self.assertEqual(r.status_code, 401)
        self.assertIn('application/json', r.content_type)


class TestOrganizeManualName(unittest.TestCase):
    """4. organize_files 手动命名优先"""

    def test_manual_name_wins(self):
        tmp = tempfile.mkdtemp(prefix='test_org_')
        try:
            fp = _mkimg(os.path.join(tmp, 'a.jpg'))
            rec = {'filename': 'a.jpg', 'name': '张三', 'idcard': ID_ZHANG,
                   'insurance_type': '养老保险', 'period': ('2023-01', '2024-06'),
                   '_source_path': fp, '_source_origin': 'a.jpg',
                   '_manual_name': '定制文件名'}
            out = os.path.join(tmp, 'out')
            result = __import__('modules.insurance.core.file_organizer',
                                fromlist=['organize_files']).organize_files([rec], ROSTER, out)
            self.assertEqual(result['abnormal_count'], 0)
            self.assertIn('定制文件名.jpg',
                          result['folder_structure']['养老保险参保证明'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestFrontendStatics(unittest.TestCase):
    """5. 前端静态检查：折叠面板/搜索/处理弹窗/apiJson 防御"""

    def setUp(self):
        js = os.path.join(PROJ, 'modules/insurance/static/js/app.js')
        css = os.path.join(PROJ, 'modules/insurance/static/css/style.css')
        html = os.path.join(PROJ, 'modules/insurance/templates/insurance_index.html')
        with open(js, encoding='utf-8') as f:
            self.js = f.read()
        with open(css, encoding='utf-8') as f:
            self.css = f.read()
        with open(html, encoding='utf-8') as f:
            self.html = f.read()

    def test_apijson_guard(self):
        self.assertIn('function apiJson', self.js)
        self.assertEqual(self.js.count('.then(apiJson)') >= 9, True)

    def test_collapsible_panel(self):
        # 模板：两个面板均有折叠结构且默认收起
        self.assertIn('collapsible-panel collapsed', self.html)
        self.assertIn('rosterPreviewHeader', self.html)
        self.assertIn('togglePanel', self.js)
        self.assertIn('.collapsible-panel.collapsed .panel-body', self.css)

    def test_search_bar(self):
        self.assertIn('id="resultSearch"', self.html)
        self.assertIn('renderPersonTable', self.js)
        self.assertIn('未找到姓名包含', self.js)  # 空状态提示
        self.assertIn('.table-search-bar', self.css)

    def test_excluded_handle_modal(self):
        self.assertIn('id="excludedHandleModal"', self.html)
        self.assertIn('openExcludedHandle', self.js)
        self.assertIn('submitExcludedHandle', self.js)
        self.assertIn('update_excluded_image', self.js)

    def test_image_preview_lightbox(self):
        self.assertIn('id="imagePreviewModal"', self.html)
        self.assertIn('openImagePreview', self.js)
        self.assertIn('image_preview', self.js)
        self.assertIn('dblclick', self.js)  # 双击预览绑定
        self.assertIn('.lightbox-box', self.css)

    def test_backend_endpoints(self):
        bp_src = os.path.join(PROJ, 'modules/insurance/blueprint.py')
        with open(bp_src, encoding='utf-8') as f:
            src = f.read()
        self.assertIn("'/api/image_preview/<task_id>'", src)
        self.assertIn("'/api/update_excluded_image/<task_id>'", src)
        self.assertIn('_manual_name', src)
        auth_src = os.path.join(PROJ, 'core/auth.py')
        with open(auth_src, encoding='utf-8') as f:
            asrc = f.read()
        self.assertIn('_is_api_request', asrc)
        self.assertNotIn("request.path.startswith('/api/')", asrc)


if __name__ == '__main__':
    unittest.main(verbosity=2)
