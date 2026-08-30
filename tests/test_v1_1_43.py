# -*- coding: utf-8 -*-
"""v1.1.43 社保模块三项优化功能测试

测试链路：
1. _rebuild_result 初始重建（含"序号-姓名-身份证号"自动重命名验证）
2. 手动补录：失败记录 → 并入有效列表 → 重建（文件从异常文件夹移出并重命名）
3. 时间段覆盖：_period_overrides 优先于OCR识别值
4. 恢复识别值：清除覆盖后回到OCR值
5. 操作记录持久化
"""
import os
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.insurance import blueprint as bp

PASS = 0
FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [PASS] {desc}')
    else:
        FAIL += 1
        print(f'  [FAIL] {desc}')


# 测试数据
ID_ZHANG = '11010519491231002X'  # 合法18位
ID_LI = '110105195001010011'    # 校验码应正确
# 计算 LI 的正确校验码
w = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
codes = '10X98765432'
digits = '11010519500101001'
total = sum(int(d) * wi for d, wi in zip(digits, w))
ID_LI = digits + codes[total % 11]
digits_wang = '11010519600101001'
total_wang = sum(int(d) * wi for d, wi in zip(digits_wang, w))
ID_WANG = digits_wang + codes[total_wang % 11]

roster = [
    {'seq': 1, 'name': '张三', 'idcard': ID_ZHANG, 'identity_type': '脱贫人口'},
    {'seq': 2, 'name': '李四', 'idcard': ID_LI, 'identity_type': '失业半年以上'},
    {'seq': 3, 'name': '王五', 'idcard': ID_WANG, 'identity_type': '退役士兵'},
]


def make_img(tmpdir, fname):
    """创建测试用图片文件（内容无需真实图片，organize_files 仅做复制）"""
    p = os.path.join(tmpdir, fname)
    with open(p, 'wb') as f:
        f.write(b'FAKE_JPEG_FOR_TEST')
    return p


def run():
    tmpdir = tempfile.mkdtemp(prefix='test_v1143_')
    task_id = 'testv143'
    out_task = os.path.join(bp.OUTPUT_DIR, task_id)
    try:
        # ===== 构造内部状态 =====
        f1 = make_img(tmpdir, 'img_张三.jpg')
        f2 = make_img(tmpdir, 'img_李四.jpg')
        f3 = make_img(tmpdir, 'img_失败.jpg')

        success = [
            {'filename': 'img_张三.jpg', 'name': '张三', 'idcard': ID_ZHANG,
             'insurance_type': '养老保险', 'period': ('2023-01', '2024-06'),
             'company_name': '鲁岳测试公司', 'raw_text': '', 'error': None,
             '_source_path': f1, '_source_origin': 'img_张三.jpg'},
            {'filename': 'img_李四.jpg', 'name': '李四', 'idcard': ID_LI,
             'insurance_type': '失业保险', 'period': ('2023-03', '2024-06'),
             'company_name': '鲁岳测试公司', 'raw_text': '', 'error': None,
             '_source_path': f2, '_source_origin': 'img_李四.jpg'},
        ]
        failed = [
            {'filename': 'img_失败.jpg', 'name': '', 'idcard': '',
             'insurance_type': None, 'period': None,
             'company_name': '', 'raw_text': '', 'error': 'OCR识别失败',
             '_source_path': f3, '_source_origin': 'img_失败.jpg'},
        ]

        with bp.tasks_lock:
            bp.tasks[task_id] = {
                'status': 'done', 'current': 3, 'total': 3,
                'message': '处理完成', 'files': [], 'created_at': '',
                'paused': False, 'cancelled': False,
                'result': {
                    '_success_results': success,
                    '_excluded_results': [],
                    '_failed_results': failed,
                    '_all_files': ['img_张三.jpg', 'img_李四.jpg', 'img_失败.jpg'],
                    '_task_dir': tmpdir,
                    '_year_range': None,
                    '_roster': roster,
                    '_roster_company': '鲁岳测试公司',
                    '_roster_source_path': '',
                    '_company_name': '鲁岳测试公司',
                    '_ocr_companies': {'鲁岳测试公司': 2},
                    '_company_mismatch_files': [],
                    '_period_overrides': {},
                    '_manual_log': [],
                },
            }

        # ===== 1. 初始重建 =====
        print('\n===== 1. 初始重建（自动重命名 序号-姓名-身份证号）=====')
        res = bp._rebuild_result(task_id)
        check('重建返回公开result', res is not None)
        check('success_count=2', res['success_count'] == 2)
        check('failed_count=1', res['failed_count'] == 1)
        check('person_count=3（含花名册补全王五）', res['person_count'] == 3)
        # 验证自动重命名
        files_yanglao = res['organize_result']['folder_structure'].get('养老保险参保证明', [])
        print('    养老文件夹:', files_yanglao)
        check('张三重命名含身份证号', any(f == f'01-张三-{ID_ZHANG}.jpg' for f in files_yanglao))
        files_shiye = res['organize_result']['folder_structure'].get('失业保险参保证明', [])
        print('    失业文件夹:', files_shiye)
        check('李四重命名含身份证号', any(f == f'02-李四-{ID_LI}.jpg' for f in files_shiye))
        files_abn = res['organize_result']['folder_structure'].get('异常图片', [])
        print('    异常文件夹:', files_abn)
        check('失败图片在异常文件夹（原名）', 'img_失败.jpg' in files_abn)
        check('Excel已生成', os.path.exists(res['excel_path']))
        # 统计基线：张三重叠（单险种无重叠）
        ps_zhang = [p for p in res['person_stats'] if p['name'] == '张三'][0]
        check('张三养老时间段 2023-01~2024-06',
              ps_zhang['insurances'].get('养老保险', {}).get('start') == '2023-01'
              and ps_zhang['insurances'].get('养老保险', {}).get('end') == '2024-06')

        # ===== 2. 手动补录 =====
        print('\n===== 2. 手动补录（img_失败.jpg → 王五/工伤保险）=====')
        with bp.tasks_lock:
            result = bp.tasks[task_id]['result']
            src = result['_failed_results'][0]
            result['_failed_results'].remove(src)
            new_rec = dict(src)
            new_rec.update({
                'name': '王五', 'idcard': ID_WANG,
                'insurance_type': '工伤保险',
                'period': ('2023-02', '2024-01'),
                'company_name': '', 'error': None, 'raw_text': '手动补录',
                '_manual': True,
            })
            result['_success_results'].append(new_rec)
            result['_manual_log'].append({
                'time': '2026-08-22 12:00:00', 'action': '手动补录',
                'name': '王五', 'idcard': ID_WANG,
                'insurance_type': '工伤保险', 'old': '识别失败',
                'new': '2023-02 ~ 2024-01', 'operator': 'tester',
            })
        res2 = bp._rebuild_result(task_id)
        check('补录后 failed_count=0', res2['failed_count'] == 0)
        check('补录后 success_count=3', res2['success_count'] == 3)
        files_gong = res2['organize_result']['folder_structure'].get('工伤保险参保证明', [])
        print('    工伤文件夹:', files_gong)
        check('补录图片按花名册重命名归类', any(f == f'03-王五-{ID_WANG}.jpg' for f in files_gong))
        files_abn2 = res2['organize_result']['folder_structure'].get('异常图片', [])
        check('异常文件夹已清空', len(files_abn2) == 0)
        # 手动补录与自动识别同等效力：王五应有工伤时间段且参与重叠统计
        ps_wang = [p for p in res2['person_stats'] if p['name'] == '王五'][0]
        check('王五工伤时间段来自补录',
              ps_wang['insurances'].get('工伤保险', {}).get('start') == '2023-02')
        check('操作记录返回', len(res2.get('operation_log', [])) == 1)
        # 验证身份证号不合法时不拼接（构造一个不合法花名册号的场景已在file_organizer单测覆盖）

        # ===== 3. 时间段覆盖 =====
        print('\n===== 3. 时间段手动修改（张三养老 2023-01~2024-06 → 2022-07~2024-06）=====')
        with bp.tasks_lock:
            result = bp.tasks[task_id]['result']
            result['_period_overrides'][('张三', ID_ZHANG)] = {
                '养老保险': ('2022-07', '2024-06'),
            }
            result['_manual_log'].append({
                'time': '2026-08-22 12:05:00', 'action': '修改时间段',
                'name': '张三', 'idcard': ID_ZHANG, 'insurance_type': '养老保险',
                'old': '2023-01 ~ 2024-06', 'new': '2022-07 ~ 2024-06',
                'operator': 'tester',
            })
        res3 = bp._rebuild_result(task_id)
        ps_zhang3 = [p for p in res3['person_stats'] if p['name'] == '张三'][0]
        check('覆盖层生效: 养老起始 2022-07',
              ps_zhang3['insurances'].get('养老保险', {}).get('start') == '2022-07')
        check('操作记录累计2条', len(res3.get('operation_log', [])) == 2)

        # ===== 4. 新增时间段（张三新增医疗险种）=====
        print('\n===== 4. 新增时间段（张三新增医疗保险 2024-01~2024-06）=====')
        with bp.tasks_lock:
            result = bp.tasks[task_id]['result']
            result['_period_overrides'][('张三', ID_ZHANG)]['医疗保险'] = ('2024-01', '2024-06')
        res4 = bp._rebuild_result(task_id)
        ps_zhang4 = [p for p in res4['person_stats'] if p['name'] == '张三'][0]
        check('新增医疗险种生效',
              ps_zhang4['insurances'].get('医疗保险', {}).get('start') == '2024-01')
        check('双险重叠计算生效（2024-01~2024-06 共6个月）',
              ps_zhang4.get('overlap_months') == 6)

        # ===== 5. 清除覆盖恢复识别值 =====
        print('\n===== 5. 恢复识别结果（清除张三全部覆盖）=====')
        with bp.tasks_lock:
            result = bp.tasks[task_id]['result']
            result['_period_overrides'].pop(('张三', ID_ZHANG), None)
        res5 = bp._rebuild_result(task_id)
        ps_zhang5 = [p for p in res5['person_stats'] if p['name'] == '张三'][0]
        check('养老恢复识别值 2023-01',
              ps_zhang5['insurances'].get('养老保险', {}).get('start') == '2023-01')
        check('新增的医疗险种消失（无OCR记录）',
              '医疗保险' not in ps_zhang5['insurances'])

        # ===== 6. 操作记录持久化 =====
        print('\n===== 6. 操作记录持久化 =====')
        log_path = os.path.join(bp.OUTPUT_DIR, task_id, '操作记录.json')
        check('操作记录.json已写入', os.path.exists(log_path))
        import json
        with open(log_path, encoding='utf-8') as f:
            log_data = json.load(f)
        check('持久化记录条数=2', len(log_data) == 2)
        print('    记录:', [e['action'] for e in log_data])

        # ===== 7. 验证目录清空重建无残留 =====
        print('\n===== 7. 目录重建 =====')
        all_folder_files = sum(len(v) for v in res5['organize_result']['folder_structure'].values())
        check('整理文件总数=3（张三/李四/王五补录）', all_folder_files == 3)

        print(f'\n========== 测试结果: PASS={PASS} FAIL={FAIL} ==========')
        return FAIL == 0
    finally:
        # 清理
        with bp.tasks_lock:
            bp.tasks.pop(task_id, None)
        for d in (out_task,):
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
        # 清理生成的Excel
        for r in ['res', 'res2', 'res3', 'res4', 'res5']:
            pass
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
        # Excel文件在 OUTPUT_DIR 根下，按测试时间戳删除本次生成的
        import glob
        for fp in glob.glob(os.path.join(bp.OUTPUT_DIR, '申报重点群体税收优惠政策总台账_test*.xlsx')):
            pass  # 时间戳文件名，无法精确匹配，留给人工检查
        # 实际上生成在 OUTPUT_DIR，删除最近5分钟内生成的测试Excel
        import time as _t
        now = _t.time()
        for fp in glob.glob(os.path.join(bp.OUTPUT_DIR, '申报重点群体税收优惠政策总台账_*.xlsx')):
            if now - os.path.getmtime(fp) < 300:
                try:
                    os.remove(fp)
                except Exception:
                    pass


if __name__ == '__main__':
    ok = run()
    sys.exit(0 if ok else 1)
