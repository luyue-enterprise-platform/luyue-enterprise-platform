# -*- coding: utf-8 -*-
"""验证 EXE 内 Python 模块是否包含 v1.1.46 关键逻辑字符串
用法: python _verify_exe_code.py <exe路径>
"""
import os
import sys

from PyInstaller.archive.readers import CArchiveReader

EXE_PATH = sys.argv[1] if len(sys.argv) > 1 else \
    r'D:\鲁岳企业服务\重点群体项目\鲁岳企业服务_综合智能平台\dist\鲁岳企业服务_综合智能平台.exe'

# 需要确认的关键串 -> 所在模块
CHECKS = {
    'core.auth': ['认证服务暂时不可用，请检查网络或联系管理员'],
    'app': [
        'https://luyue-1466112667.cos.ap-shanghai.myqcloud.com/version.json',
        'https://raw.githubusercontent.com/luyue-enterprise-platform/luyue-enterprise-platform/main/version.json',
        # v1.1.49 自动升级
        '/VERYSILENT',
        '/api/app/start_update',
        '/api/app/update_progress',
        'https://luyue-1466112667.cos.ap-shanghai.myqcloud.com/',
    ],
    # v1.1.50 缴费单位误解析修复（序号/经办机构拒判 + 公司后缀投票兜底）
    'modules.insurance.core.data_parser': [
        '现缴费单位名称',
        '对应缴费单位名称',
        '经办机构',
        '事务所',
    ],
}


def collect_consts(code, out):
    for c in code.co_consts:
        if hasattr(c, 'co_consts'):          # 嵌套 code
            collect_consts(c, out)
        elif isinstance(c, str):
            out.add(c)
        elif isinstance(c, tuple):           # 常量可能包在 tuple 里（marshal 后依旧）
            for x in c:
                if isinstance(x, str):
                    out.add(x)
        elif isinstance(c, frozenset):
            for x in c:
                if isinstance(x, str):
                    out.add(x)


def has_string(mod_code, needle):
    seen = set()
    collect_consts(mod_code, seen)
    return needle in seen


exe = CArchiveReader(EXE_PATH)
pyz = exe.open_embedded_archive('PYZ.pyz')

ok = True
for mod, needles in CHECKS.items():
    print(f'--- 模块 {mod} ---')
    try:
        code = pyz.extract(mod)
    except Exception as e:
        print(f'  [FAIL] 提取模块失败: {e}')
        ok = False
        continue
    for needle in needles:
        if has_string(code, needle):
            print(f'  [OK] 含字符串: {needle[:70]}')
        else:
            print(f'  [MISS] 缺少字符串: {needle[:70]}')
            ok = False

print('\n代码逻辑验证', '通过 ✓' if ok else '存在缺失 ✗')
sys.exit(0 if ok else 1)
