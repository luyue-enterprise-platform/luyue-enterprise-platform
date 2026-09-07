# -*- coding: utf-8 -*-
"""多省份参保证明识别扩展（第一阶段）测试

规则（省份手动必选版）：
1. 省份完全以用户手动选择为准（必选项）：系统不从文件内容推断省份，
   也不校验文件与所选省份是否相符——锚点仅用于同省多版式择优排序。
2. 模板引擎：内置陕西模板、省份列表数据驱动、外置 JSON 模板热加载（含坏文件容错）
3. 端点：/api/provinces；upload 省份必选（缺失 400；未知省份 400）
4. 省份关联与传递：解析记录逐条携带 province_code，任务结果透传
5. 存量行为零回归：陕西真实样本经省份路由解析结果与直接调用 data_parser 一致
"""
import io as _io
import json
import os
import sys
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app
from modules.insurance.core import template_engine as te
from modules.insurance.core import data_parser as dp
from modules.insurance import blueprint as bp

# 真实陕西样本（tests/test_v1_1_50.py 同源）
SN_UNEMPLOY_TEXT = '''陕西省社会保险权益记录单（失业保险）
姓名：薛宇行 个人编号：612052142345
证件号码：610524200006026016
现缴费单位名称：陕西陕煤澄合矿业有限公司 单位：元（小数点后保留两位）
序号 缴费年度 实缴月份 实缴月数 单位缴费 个人缴费 对应缴费单位名称 经办机构
1 2023 202304-202312 9 272.94 116.97 澄城县
2 2024 202401-202412 12 597.12 255.96 澄城县
现参保经办机构：澄城县 打印时间：20260819'''

# 医保版式（无"陕西"字样 —— 省份提示词不阻断的关键场景）
SN_MEDICAL_TEXT = '''城镇职工基本医疗保险参保缴费证明
姓名：刘伟民 身份证号：610502197505123456
缴费年度 缴费月份 缴费年度 缴费月份 缴费年度 缴费月份
2016 2（月） 2021 8（月） 2024 12（月）'''


# ==================== 1. 模板引擎 ====================
class TestTemplateEngine(unittest.TestCase):

    def test_builtin_shaanxi_loaded_and_default(self):
        provinces = te.get_provinces()
        self.assertTrue(len(provinces) >= 1)
        first = provinces[0]
        self.assertEqual(first['province_code'], '610000')
        self.assertEqual(first['province_name'], '陕西')
        self.assertTrue(first['default'])

    def test_anchor_score(self):
        tpl = te.BUILTIN_TEMPLATES[0]
        self.assertGreaterEqual(tpl.anchor_score(SN_UNEMPLOY_TEXT), 1)
        self.assertGreaterEqual(tpl.anchor_score(SN_MEDICAL_TEXT), 1)
        self.assertEqual(tpl.anchor_score('随便一张照片'), 0)
        self.assertEqual(tpl.anchor_score(''), 0)

    def test_match_template_ok(self):
        tpl, score, err = te.match_template(SN_UNEMPLOY_TEXT, '610000')
        self.assertIsNotNone(tpl, err)
        self.assertEqual(tpl.parser, 'builtin_shaanxi')
        self.assertGreaterEqual(score, 1)

    def test_match_template_unsupported_province(self):
        tpl, score, err = te.match_template(SN_UNEMPLOY_TEXT, '999999')
        self.assertIsNone(tpl)
        self.assertIn('暂未支持', err)
        self.assertIn('陕西', err)  # 提示可用省份

    def test_match_template_no_content_judgment(self):
        """省份路由不做内容校验：任意内容都路由到所选省份模板（择优不拦截）"""
        tpl, score, err = te.match_template('劳动合同甲方乙方签字盖章', '610000')
        self.assertIsNotNone(tpl)  # 不因内容不符返回 None
        self.assertEqual(err, '')
        self.assertEqual(tpl.parser, 'builtin_shaanxi')

    def test_parse_with_province_equivalent_to_legacy(self):
        """省份路由模式解析结果 = 兼容模式（陕西行为零变化）+ 省份盖章"""
        routed = te.parse_with_province(SN_UNEMPLOY_TEXT, '610000')
        legacy = dp.parse_ocr_result(SN_UNEMPLOY_TEXT)
        for key in ('insurance_type', 'name', 'idcard', 'company_name', 'period'):
            self.assertEqual(routed[key], legacy[key], key)
        self.assertEqual(routed['template_id'], '610000_si_builtin')
        self.assertEqual(routed['province_code'], '610000')  # 省份关联到文件数据

    def test_parse_with_province_medical_no_shaanxi_word(self):
        """医保版式无"陕西"字样 → 正常解析（省份不做内容校验）"""
        routed = te.parse_with_province(SN_MEDICAL_TEXT, '610000')
        self.assertNotIn('error', routed)
        self.assertEqual(routed['name'], '刘伟民')

    def test_parse_with_province_no_content_blocking(self):
        """非参保证明内容：不拦截省份路由，按原有规则解析（后续自然进失败桶）"""
        routed = te.parse_with_province('甲方乙方经协商一致签订本合同，货款两清', '610000')
        self.assertNotIn('error', routed)          # 省份不做内容校验
        self.assertEqual(routed['template_id'], '610000_si_builtin')
        self.assertEqual(routed['province_code'], '610000')
        self.assertEqual(routed['name'], '')        # 原有规则解析（无姓名）

    def test_parse_with_province_unknown_province(self):
        routed = te.parse_with_province(SN_UNEMPLOY_TEXT, '999999')
        self.assertIn('error', routed)
        self.assertIn('暂未支持', routed['error'])

    def test_parse_ocr_result_from_image_legacy_mode(self):
        """兼容模式（province_code=None）签名向后兼容"""
        import inspect
        sig = inspect.signature(dp.parse_ocr_result_from_image)
        self.assertIsNone(sig.parameters['province_code'].default)


# ==================== 2. 外置模板热加载 ====================
class TestExternalTemplates(unittest.TestCase):

    def setUp(self):
        self._old_dir = te.EXTERNAL_TEMPLATE_DIR
        self.tmp = tempfile.mkdtemp()
        te.EXTERNAL_TEMPLATE_DIR = os.path.join(self.tmp, 'insurance_templates')
        os.makedirs(te.EXTERNAL_TEMPLATE_DIR)
        te._registry_cache = None
        te._registry_mtime = None

    def tearDown(self):
        te.EXTERNAL_TEMPLATE_DIR = self._old_dir
        te._registry_cache = None
        te._registry_mtime = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, data):
        with open(os.path.join(te.EXTERNAL_TEMPLATE_DIR, name), 'w',
                  encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)

    def test_external_province_appears_in_list(self):
        self._write('henan.json', {
            'template_id': '410000_si_2026',
            'province_code': '410000',
            'province_name': '河南',
            'anchors': [{'text': '河南省', 'weight': 3},
                        {'text': '参保', 'weight': 1}],
            'min_match_score': 3,
            'parser': 'regex',
            'fields': {
                'name': {'regex': r'姓名[：:]?\s*(\S{2,4})', 'group': 1},
                'idcard': {'regex': r'(\d{17}[\dXx])', 'group': 1},
            },
            'period': {'regex': r'(\d{4})[年.\-/](\d{1,2})月?\s*[-至~]\s*(\d{4})[年.\-/](\d{1,2})月?'},
        })
        provinces = te.get_provinces()
        codes = [p['province_code'] for p in provinces]
        self.assertIn('610000', codes)
        self.assertIn('410000', codes)
        self.assertEqual(provinces[0]['province_code'], '610000')  # 默认省仍置顶

    def test_external_regex_template_parse(self):
        self._write('henan.json', {
            'template_id': '410000_si_2026',
            'province_code': '410000',
            'province_name': '河南',
            'anchors': [{'text': '河南省', 'weight': 3}],
            'min_match_score': 3,
            'parser': 'regex',
            'fields': {
                'name': {'regex': r'姓名[：:]?\s*(\S{2,4})', 'group': 1},
                'idcard': {'regex': r'(\d{17}[\dXx])', 'group': 1},
                'insurance_type': {'patterns': {'养老保险': ['养老']}},
            },
            'period': {'regex': r'(\d{4})[年.\-/](\d{1,2})月?\s*[-至~]\s*(\d{4})[年.\-/](\d{1,2})月?'},
        })
        henan_text = ('河南省社会保险参保证明\n姓名：王大锤 证件号码：410102199001011234\n'
                      '养老保险 缴费起止：2019年1月至2025年6月')
        routed = te.parse_with_province(henan_text, '410000')
        self.assertNotIn('error', routed, routed.get('error'))
        self.assertEqual(routed['name'], '王大锤')
        self.assertEqual(routed['idcard'], '410102199001011234')
        self.assertEqual(routed['insurance_type'], '养老保险')
        self.assertEqual(routed['period'], ('2019-01', '2025-06'))
        self.assertEqual(routed['template_id'], '410000_si_2026')

    def test_external_template_no_content_blocking(self):
        """省份以用户选择为准：陕西文件选河南 → 不拦截，按河南模板尽力解析"""
        self._write('henan.json', {
            'template_id': '410000_si_2026',
            'province_code': '410000',
            'province_name': '河南',
            'anchors': [{'text': '河南省', 'weight': 3}],
            'min_match_score': 3,
            'parser': 'regex',
            'fields': {
                'name': {'regex': r'姓名[：:]?\s*(\S{2,4})', 'group': 1},
            },
        })
        # 陕西文本选河南：不做内容校验，仍用河南模板解析（姓名可提取）
        routed = te.parse_with_province(SN_UNEMPLOY_TEXT, '410000')
        self.assertNotIn('error', routed)
        self.assertEqual(routed['template_id'], '410000_si_2026')
        self.assertEqual(routed['province_code'], '410000')
        self.assertEqual(routed['name'], '薛宇行')

    def test_broken_external_file_ignored(self):
        """坏 JSON / 缺省份字段的外置模板：跳过不影响内置模板"""
        with open(os.path.join(te.EXTERNAL_TEMPLATE_DIR, 'broken.json'), 'w',
                  encoding='utf-8') as f:
            f.write('{not valid json')
        self._write('nofields.json', {'template_id': 'x', 'version': 1})  # 缺 province_code
        provinces = te.get_provinces()
        codes = [p['province_code'] for p in provinces]
        self.assertEqual(codes, ['610000'])  # 只剩内置陕西

    def test_registry_hot_reload_on_change(self):
        self.assertEqual([p['province_code'] for p in te.get_provinces()], ['610000'])
        self._write('henan.json', {
            'template_id': '410000_si', 'province_code': '410000',
            'province_name': '河南', 'anchors': [], 'parser': 'regex', 'fields': {},
        })
        # 注册表按目录签名自动重载
        codes = [p['province_code'] for p in te.get_provinces()]
        self.assertIn('410000', codes)


# ==================== 3. 端点 ====================
class TestProvinceEndpoints(unittest.TestCase):

    def setUp(self):
        self.c = flask_app.test_client()
        with self.c.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'tester'

    def test_api_provinces(self):
        r = self.c.get('/insurance/api/provinces')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn('provinces', data)
        self.assertEqual(data['default'], '610000')
        sn = [p for p in data['provinces'] if p['province_code'] == '610000']
        self.assertEqual(len(sn), 1)
        self.assertEqual(sn[0]['province_name'], '陕西')
        self.assertTrue(sn[0]['default'])

    def _png_bytes(self):
        import io as _io
        from PIL import Image
        buf = _io.BytesIO()
        Image.new('RGB', (60, 40), (255, 255, 255)).save(buf, 'PNG')
        return buf.getvalue()

    def test_upload_rejects_unsupported_province(self):
        r = self.c.post('/insurance/api/upload', data={
            'province': '999999',
            'roster': '[]',
            'files': [(_io.BytesIO(self._png_bytes()), 'a.png')],
        }, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)
        self.assertIn('暂未支持', r.get_json()['error'])

    def test_upload_requires_province(self):
        """省份必选：不传 province → 400 明确提示（不再默认回退）"""
        r = self.c.post('/insurance/api/upload', data={
            'roster': '[]',
            'files': [(_io.BytesIO(self._png_bytes()), 'white.png')],
        }, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)
        self.assertIn('请先选择', r.get_json()['error'])
        self.assertIn('省份', r.get_json()['error'])

    def test_upload_accepts_shaanxi_province(self):
        r = self.c.post('/insurance/api/upload', data={
            'province': '610000',
            'roster': '[]',
            'files': [(_io.BytesIO(self._png_bytes()), 'white.png')],
        }, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['province'], '610000')


# ==================== 4. 前端接入 ====================
class TestFrontendProvinceUI(unittest.TestCase):

    def test_html_has_province_selector(self):
        tpl_path = os.path.join(os.path.dirname(
            os.path.abspath(bp.__file__)), 'templates', 'insurance_index.html')
        with open(tpl_path, encoding='utf-8') as f:
            html = f.read()
        self.assertIn('id="provinceSelect"', html)
        self.assertIn('参保证明省份', html)
        # 省份选择器位于统计年月范围之前（先选省份）
        self.assertLess(html.index('provinceSelect'), html.index('yearStart'))

    def test_js_loads_provinces_and_sends_with_upload(self):
        js_path = os.path.join(os.path.dirname(
            os.path.abspath(bp.__file__)), 'static', 'js', 'app.js')
        with open(js_path, encoding='utf-8') as f:
            js = f.read()
        self.assertIn("/insurance/api/provinces", js)
        self.assertIn("formData.append('province', currentProvince)", js)
        # 省份必选：占位提示 + 上传前校验拦截
        self.assertIn("-- 请选择省份 --", js)
        self.assertIn("function validateProvinceSelected", js)
        self.assertIn("if (!validateProvinceSelected())", js)
        # 不再自动回填上次选择（省份必须由用户本次手动选择）
        self.assertNotIn("localStorage.getItem(PROVINCE_STORAGE_KEY)", js)
        self.assertNotIn("localStorage.setItem(PROVINCE_STORAGE_KEY", js)

    def test_html_province_required_ui(self):
        tpl_path = os.path.join(os.path.dirname(
            os.path.abspath(bp.__file__)), 'templates', 'insurance_index.html')
        with open(tpl_path, encoding='utf-8') as f:
            html = f.read()
        self.assertIn('province-required-mark', html)      # 必选红星标记
        self.assertIn('请先选择省份（必选）', html)          # 必选提示文案


if __name__ == '__main__':
    unittest.main(verbosity=2)
