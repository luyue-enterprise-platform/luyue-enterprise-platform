# -*- coding: utf-8 -*-
"""v2.4.0 OCR 模型热更新测试（阶段一：模型与代码解耦）

需求覆盖（见《OCR模型迭代机制技术方案分析.md》方案一）：
1. model_store：外置模型目录/manifest 读写、逐文件 SHA256 校验（含防目录穿越）、
   resolve 顺序（外置校验通过优先，坏模型降级内置基线不抛异常）、
   apply_staging 原子轮换（当前→backup、staging→当前、补记 applied_at）、rollback 反向轮换。
2. model_updater：check_remote 版本比对 / min_platform 门槛 / pack_url COS 白名单、
   网络失败容错、重复启动拒绝、更新中禁止回滚。
3. /api/model/* 五端点登录保护（未登录 401，登录后可访问）。
4. ocr_engine kwargs 合并：外置模型存在时引擎收到显式模型路径。
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import model_store, model_updater


def _sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


class _StoreBase(unittest.TestCase):
    """model_store 测试基类：数据目录隔离到临时目录"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ly_model_test_')
        self._orig_data_dir = model_store.data_dir
        model_store.data_dir = lambda: self.tmp
        self.root = model_store.model_root()

    def tearDown(self):
        model_store.data_dir = self._orig_data_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _slot_bytes(fname):
        return b'MODEL_' + fname.encode()

    @classmethod
    def _manifest(cls, version_code, det, cls_slot, rec, **over):
        m = {
            'model_version': 'v%d' % version_code,
            'model_version_code': version_code,
            'models': {
                'det': {'file': det[0], 'sha256': _sha256_bytes(cls._slot_bytes(det[0]))},
                'cls': {'file': cls_slot[0], 'sha256': _sha256_bytes(cls._slot_bytes(cls_slot[0]))},
                'rec': {'file': rec[0], 'sha256': _sha256_bytes(cls._slot_bytes(rec[0]))},
            },
        }
        m.update(over)
        return m

    def _install_external(self, manifest):
        """把 manifest 及其模型文件写入外置模型目录（模拟一次已应用的热更新）"""
        os.makedirs(self.root, exist_ok=True)
        for slot in model_store.MODEL_SLOTS:
            fname = manifest['models'][slot]['file']
            with open(os.path.join(self.root, fname), 'wb') as f:
                f.write(self._slot_bytes(fname))
        with open(model_store.manifest_path(), 'w', encoding='utf-8') as f:
            json.dump(manifest, f, ensure_ascii=False)


class TestManifestIO(_StoreBase):

    def test_read_manifest_missing_returns_none(self):
        self.assertIsNone(model_store.read_manifest())

    def test_read_manifest_corrupt_returns_none(self):
        os.makedirs(self.root, exist_ok=True)
        with open(model_store.manifest_path(), 'w', encoding='utf-8') as f:
            f.write('{ not valid json !!!')
        self.assertIsNone(model_store.read_manifest())

    def test_read_manifest_non_dict_returns_none(self):
        os.makedirs(self.root, exist_ok=True)
        with open(model_store.manifest_path(), 'w', encoding='utf-8') as f:
            f.write('[1, 2, 3]')
        self.assertIsNone(model_store.read_manifest())


class TestResolveOrder(_StoreBase):
    """加载顺序：外置校验通过 → 用外置；否则回退内置基线（None = 默认解析）"""

    def test_no_external_resolves_none_and_empty_kwargs(self):
        self.assertIsNone(model_store.resolve_model_paths())
        self.assertEqual(model_store.engine_model_kwargs(), {})

    def test_valid_external_resolves_paths(self):
        m = self._manifest(3, ('det_a.onnx', b'a'), ('cls_a.onnx', b'b'),
                           ('rec_a.onnx', b'c'))
        self._install_external(m)
        paths = model_store.resolve_model_paths()
        self.assertIsNotNone(paths)
        for slot in model_store.MODEL_SLOTS:
            self.assertTrue(os.path.isabs(paths[slot]))
            self.assertTrue(paths[slot].startswith(self.root))
        kwargs = model_store.engine_model_kwargs()
        self.assertEqual(kwargs['det_model_path'], paths['det'])
        self.assertEqual(kwargs['cls_model_path'], paths['cls'])
        self.assertEqual(kwargs['rec_model_path'], paths['rec'])

    def test_bad_sha_degrades_to_baseline(self):
        """校验失败不抛异常：降级 None，平台永远可用"""
        m = self._manifest(3, ('det_a.onnx', b'a'), ('cls_a.onnx', b'b'),
                           ('rec_a.onnx', b'c'))
        self._install_external(m)
        # 篡改 det 文件内容
        with open(os.path.join(self.root, 'det_a.onnx'), 'wb') as f:
            f.write(b'TAMPERED')
        self.assertIsNone(model_store.resolve_model_paths())
        self.assertEqual(model_store.engine_model_kwargs(), {})

    def test_missing_file_degrades_to_baseline(self):
        m = self._manifest(3, ('det_a.onnx', b'a'), ('cls_a.onnx', b'b'),
                           ('rec_a.onnx', b'c'))
        self._install_external(m)
        os.remove(os.path.join(self.root, 'rec_a.onnx'))
        self.assertIsNone(model_store.resolve_model_paths())


class TestVerifyManifestFiles(_StoreBase):

    def test_verify_ok(self):
        m = self._manifest(1, ('det_a.onnx', b'a'), ('cls_a.onnx', b'b'),
                           ('rec_a.onnx', b'c'))
        self._install_external(m)
        ok, issues = model_store.verify_manifest_files(m, self.root)
        self.assertTrue(ok)
        self.assertEqual(issues, [])

    def test_verify_missing_file_reports_issue(self):
        m = self._manifest(1, ('det_a.onnx', b'a'), ('cls_a.onnx', b'b'),
                           ('rec_a.onnx', b'c'))
        self._install_external(m)
        os.remove(os.path.join(self.root, 'cls_a.onnx'))
        ok, issues = model_store.verify_manifest_files(m, self.root)
        self.assertFalse(ok)
        self.assertTrue(any('不存在' in s for s in issues))

    def test_verify_rejects_path_traversal(self):
        """防目录穿越：文件名含路径分隔符必须拒绝"""
        m = self._manifest(1, ('../../evil.onnx', b'a'), ('cls_a.onnx', b'b'),
                           ('rec_a.onnx', b'c'))
        os.makedirs(self.root, exist_ok=True)
        for slot in ('cls', 'rec'):
            fname = m['models'][slot]['file']
            with open(os.path.join(self.root, fname), 'wb') as f:
                f.write(b'x')
        ok, issues = model_store.verify_manifest_files(m, self.root)
        self.assertFalse(ok)
        self.assertTrue(any('非法' in s for s in issues))

    def test_verify_decl_missing_fields(self):
        m = {'models': {'det': {'file': 'a.onnx'}}}  # 无 sha256，cls/rec 缺失
        ok, issues = model_store.verify_manifest_files(m, self.root)
        self.assertFalse(ok)
        self.assertEqual(len(issues), 3)


class TestApplyAndRollback(_StoreBase):
    """原子轮换 + 回滚（热更新的核心安全机制）"""

    def _staging_with(self, manifest):
        staging = model_store.staging_root()
        os.makedirs(staging, exist_ok=True)
        for slot in model_store.MODEL_SLOTS:
            fname = manifest['models'][slot]['file']
            with open(os.path.join(staging, fname), 'wb') as f:
                f.write(b'STAGING_' + slot.encode())
        return staging

    def test_apply_staging_rotates_and_records_applied_at(self):
        old = self._manifest(1, ('det_v1.onnx', b'x1'), ('cls_v1.onnx', b'y1'),
                             ('rec_v1.onnx', b'z1'))
        self._install_external(old)
        new = self._manifest(2, ('det_v2.onnx', b'x2'), ('cls_v2.onnx', b'y2'),
                             ('rec_v2.onnx', b'z2'), notes='第二版')
        staging = self._staging_with(new)

        applied = model_store.apply_staging(staging, new)

        # 当前 = 新版
        self.assertEqual(applied['model_version_code'], 2)
        self.assertTrue(applied.get('applied_at'))
        cur = model_store.read_manifest()
        self.assertEqual(cur['model_version_code'], 2)
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'det_v2.onnx')))
        # backup = 旧版（文件 + manifest）
        self.assertTrue(os.path.isfile(
            os.path.join(model_store.backup_dir(), 'det_v1.onnx')))
        with open(os.path.join(model_store.backup_dir(), 'manifest.json'),
                  encoding='utf-8') as f:
            self.assertEqual(json.load(f)['model_version_code'], 1)

    def test_apply_staging_missing_slot_raises(self):
        new = self._manifest(2, ('det_v2.onnx', b'x2'), ('cls_v2.onnx', b'y2'),
                             ('rec_v2.onnx', b'z2'))
        staging = self._staging_with(new)
        os.remove(os.path.join(staging, 'rec_v2.onnx'))
        with self.assertRaises(RuntimeError):
            model_store.apply_staging(staging, new)

    def test_rollback_restores_previous(self):
        old = self._manifest(1, ('det_v1.onnx', b'x1'), ('cls_v1.onnx', b'y1'),
                             ('rec_v1.onnx', b'z1'))
        self._install_external(old)
        new = self._manifest(2, ('det_v2.onnx', b'x2'), ('cls_v2.onnx', b'y2'),
                             ('rec_v2.onnx', b'z2'))
        model_store.apply_staging(self._staging_with(new), new)

        restored = model_store.rollback()

        self.assertEqual(restored['model_version_code'], 1)
        cur = model_store.read_manifest()
        self.assertEqual(cur['model_version_code'], 1)
        self.assertTrue(os.path.isfile(os.path.join(self.root, 'det_v1.onnx')))
        self.assertFalse(os.path.isfile(os.path.join(self.root, 'det_v2.onnx')))

    def test_rollback_without_backup_raises(self):
        with self.assertRaises(RuntimeError):
            model_store.rollback()


class TestCurrentModelInfo(_StoreBase):

    def test_baseline_info_when_no_external(self):
        info = model_store.current_model_info()
        self.assertEqual(info['source'], '内置基线')
        self.assertTrue(info['verified'])

    def test_external_info_verified(self):
        m = self._manifest(5, ('det_a.onnx', b'a'), ('cls_a.onnx', b'b'),
                           ('rec_a.onnx', b'c'))
        self._install_external(m)
        info = model_store.current_model_info()
        self.assertEqual(info['source'], '外置模型')
        self.assertEqual(info['model_version_code'], 5)
        self.assertTrue(info['verified'])

    def test_external_info_unverified_lists_issues(self):
        m = self._manifest(5, ('det_a.onnx', b'a'), ('cls_a.onnx', b'b'),
                           ('rec_a.onnx', b'c'))
        self._install_external(m)
        with open(os.path.join(self.root, 'cls_a.onnx'), 'wb') as f:
            f.write(b'BROKEN')
        info = model_store.current_model_info()
        self.assertFalse(info['verified'])
        self.assertTrue(info['issues'])


class _UpdaterBase(unittest.TestCase):
    """model_updater 测试基类：远端清单 mock + 状态机复位"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='ly_upd_test_')
        self._orig_data_dir = model_store.data_dir
        model_store.data_dir = lambda: self.tmp
        # 复位状态机（快照恢复）
        self._orig_state = dict(model_updater._state)
        model_updater._state.update({
            'status': 'idle', 'percent': 0, 'downloaded': 0, 'total': 0,
            'remote_version': '', 'remote_version_code': 0,
            'update_available': False, 'last_error': '',
        })

    def tearDown(self):
        model_store.data_dir = self._orig_data_dir
        model_updater._state.clear()
        model_updater._state.update(self._orig_state)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _remote_manifest(code=10, min_platform=0, pack_url=None):
        return {
            'model_version': '2026.09.12-%d' % code,
            'model_version_code': code,
            'min_platform_version_code': min_platform,
            'models': {
                'det': {'file': 'det.onnx', 'sha256': '0' * 64},
                'cls': {'file': 'cls.onnx', 'sha256': '0' * 64},
                'rec': {'file': 'rec.onnx', 'sha256': '0' * 64},
            },
            'pack_url': pack_url or (model_updater.ALLOWED_PREFIX
                                     + 'ocr_model/pack.zip'),
            'pack_sha256': '1' * 64,
            'pack_size': 16000000,
            'notes': '远端说明',
        }


class TestCheckRemote(_UpdaterBase):

    def test_network_failure_tolerated(self):
        with mock.patch.object(model_updater, '_fetch_json',
                               side_effect=OSError('timeout')):
            r = model_updater.check_remote(2400)
        self.assertFalse(r['ok'])
        self.assertIn('网络', r['error'])
        self.assertEqual(model_updater.snapshot()['status'], 'idle')

    def test_no_update_when_remote_not_newer(self):
        remote = self._remote_manifest(code=2)
        with mock.patch.object(model_updater, '_fetch_json',
                               return_value=remote), \
             mock.patch.object(model_store, 'read_manifest',
                               return_value={'model_version_code': 2}):
            r = model_updater.check_remote(2400)
        self.assertTrue(r['ok'])
        self.assertFalse(r['update_available'])

    def test_update_available(self):
        remote = self._remote_manifest(code=10)
        with mock.patch.object(model_updater, '_fetch_json',
                               return_value=remote), \
             mock.patch.object(model_store, 'read_manifest',
                               return_value={'model_version_code': 2}):
            r = model_updater.check_remote(2400)
        self.assertTrue(r['ok'])
        self.assertTrue(r['update_available'])
        self.assertEqual(r['pack_url'], remote['pack_url'])
        self.assertEqual(model_updater.snapshot()['update_available'], True)

    def test_min_platform_gate(self):
        remote = self._remote_manifest(code=10, min_platform=3000)
        with mock.patch.object(model_updater, '_fetch_json',
                               return_value=remote), \
             mock.patch.object(model_store, 'read_manifest',
                               return_value={'model_version_code': 2}):
            r = model_updater.check_remote(2400)
        self.assertFalse(r['update_available'])
        self.assertIn('升级软件', r['error'])

    def test_pack_url_whitelist_rejects_foreign_host(self):
        remote = self._remote_manifest(
            code=10, pack_url='https://evil.example.com/pack.zip')
        with mock.patch.object(model_updater, '_fetch_json',
                               return_value=remote), \
             mock.patch.object(model_store, 'read_manifest',
                               return_value={'model_version_code': 2}):
            r = model_updater.check_remote(2400)
        self.assertFalse(r['update_available'])
        self.assertIn('白名单', r['error'])

    def test_pack_url_must_be_zip(self):
        remote = self._remote_manifest(
            code=10, pack_url=model_updater.ALLOWED_PREFIX + 'ocr_model/pack.exe')
        with mock.patch.object(model_updater, '_fetch_json',
                               return_value=remote), \
             mock.patch.object(model_store, 'read_manifest',
                               return_value={'model_version_code': 2}):
            r = model_updater.check_remote(2400)
        self.assertFalse(r['update_available'])


class TestUpdateLifecycle(_UpdaterBase):

    def test_start_update_rejects_duplicate(self):
        model_updater._state['status'] = 'downloading'
        ok, err = model_updater.start_update(2400)
        self.assertFalse(ok)
        self.assertIn('进行中', err)

    def test_rollback_blocked_while_updating(self):
        model_updater._state['status'] = 'applying'
        ok, err = model_updater.do_rollback()
        self.assertFalse(ok)
        self.assertIn('暂不能回滚', err)

    def test_rollback_success_sets_restart_required(self):
        with mock.patch.object(model_store, 'rollback',
                               return_value={'model_version': 'v9'}):
            ok, info = model_updater.do_rollback()
        self.assertTrue(ok)
        self.assertEqual(info, 'v9')
        self.assertEqual(model_updater.snapshot()['status'], 'restart_required')

    def test_rollback_failure_reports_error(self):
        with mock.patch.object(model_store, 'rollback',
                               side_effect=RuntimeError('没有可回滚的历史模型版本')):
            ok, err = model_updater.do_rollback()
        self.assertFalse(ok)
        self.assertIn('没有可回滚', err)
        self.assertEqual(model_updater.snapshot()['status'], 'error')


class TestModelEndpoints(unittest.TestCase):
    """5 个 /api/model/* 端点：登录保护 + 登录后可用"""

    def setUp(self):
        from app import app as flask_app
        self.flask_app = flask_app
        self.tmp = tempfile.mkdtemp(prefix='ly_ep_test_')
        self._orig_data_dir = model_store.data_dir
        model_store.data_dir = lambda: self.tmp
        self._orig_state = dict(model_updater._state)
        model_updater._state.update({'status': 'idle', 'last_error': ''})

    def tearDown(self):
        model_store.data_dir = self._orig_data_dir
        model_updater._state.clear()
        model_updater._state.update(self._orig_state)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_endpoints_require_login(self):
        with self.flask_app.test_client() as c:
            self.assertEqual(c.get('/api/model/status').status_code, 401)
            self.assertEqual(
                c.post('/api/model/check').status_code, 401)
            self.assertEqual(
                c.post('/api/model/start_update').status_code, 401)
            self.assertEqual(
                c.get('/api/model/update_progress').status_code, 401)
            self.assertEqual(
                c.post('/api/model/rollback').status_code, 401)

    def _login(self, c):
        with c.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'tester'

    def test_status_returns_model_and_update_state(self):
        with self.flask_app.test_client() as c:
            self._login(c)
            r = c.get('/api/model/status')
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data['ok'])
        self.assertIn('model', data)
        self.assertIn('update', data)
        self.assertEqual(data['model']['source'], '内置基线')

    def test_update_progress_reflects_state_machine(self):
        model_updater._state.update({'status': 'downloading', 'percent': 42,
                                     'downloaded': 100, 'total': 200})
        with self.flask_app.test_client() as c:
            self._login(c)
            r = c.get('/api/model/update_progress')
        data = r.get_json()
        self.assertEqual(data['status'], 'downloading')
        self.assertEqual(data['percent'], 42)

    def test_start_update_conflict_returns_409(self):
        model_updater._state['status'] = 'downloading'
        with self.flask_app.test_client() as c:
            self._login(c)
            r = c.post('/api/model/start_update')
        self.assertEqual(r.status_code, 409)
        self.assertFalse(r.get_json()['ok'])


class TestOcrEngineIntegration(unittest.TestCase):
    """ocr_engine kwargs 合并：外置模型存在 → 引擎收到显式路径"""

    def test_engine_receives_external_model_paths(self):
        from modules.insurance.core import ocr_engine
        fake_kwargs = {'det_model_path': 'D:/m/det.onnx',
                       'cls_model_path': 'D:/m/cls.onnx',
                       'rec_model_path': 'D:/m/rec.onnx'}
        captured = {}

        class _FakeRapidOCR:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        # 清线程本地缓存，保证重新走初始化分支
        ocr_engine._engines = __import__('threading').local()
        with mock.patch('rapidocr_onnxruntime.RapidOCR', _FakeRapidOCR), \
             mock.patch('core.model_store.engine_model_kwargs',
                        return_value=fake_kwargs):
            ocr_engine.get_engine()
        self.assertEqual(captured.get('det_model_path'), 'D:/m/det.onnx')
        self.assertEqual(captured.get('rec_model_path'), 'D:/m/rec.onnx')

    def test_model_store_failure_does_not_block_engine(self):
        """模型仓库抛异常 → 静默回退内置基线，OCR 永不因模型目录问题启动失败"""
        from modules.insurance.core import ocr_engine
        captured = {}

        class _FakeRapidOCR:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        ocr_engine._engines = __import__('threading').local()

        def _boom():
            raise RuntimeError('manifest corrupted')

        with mock.patch('rapidocr_onnxruntime.RapidOCR', _FakeRapidOCR), \
             mock.patch('core.model_store.engine_model_kwargs',
                        side_effect=_boom):
            engine = ocr_engine.get_engine()  # 不得抛异常
        self.assertIsNotNone(engine)
        self.assertIsNone(captured.get('det_model_path'))


class TestPortalFrontend(unittest.TestCase):
    """前端静态检查：模型更新入口/弹窗/JS 均已就位（v2.4.0）"""

    def setUp(self):
        self.html = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'portal', 'templates', 'portal.html')
        with open(self.html, encoding='utf-8') as f:
            self.content = f.read()

    def test_dropdown_entry_exists(self):
        self.assertIn('id="btnModelUpdate"', self.content)
        self.assertIn('openModelUpdateModal', self.content)

    def test_modal_and_controls_exist(self):
        for marker in ('id="modelUpdateModal"', 'id="btnModelCheck"',
                       'id="btnModelStart"', 'id="btnModelRollback"',
                       'id="modelProgressBar"', 'id="modelRestartHint"'):
            self.assertIn(marker, self.content)

    def test_js_calls_all_five_endpoints(self):
        for ep in ('/api/model/status', '/api/model/check',
                   '/api/model/start_update', '/api/model/update_progress',
                   '/api/model/rollback'):
            self.assertIn("'%s'" % ep, self.content)

    def test_warm_update_restart_hint_present(self):
        """温更新提示：重启后生效，不代重启"""
        self.assertIn('重启后加载生效', self.content)


if __name__ == '__main__':
    unittest.main()
