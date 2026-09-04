# -*- coding: utf-8 -*-
"""验证打包 EXE 内嵌数据文件：auth_config.json 指向腾讯云、version.json 与本地一致
用法: python _verify_exe.py <exe路径>
"""
import os
import sys
import json

from PyInstaller.archive.readers import CArchiveReader

EXE_PATH = sys.argv[1] if len(sys.argv) > 1 else \
    r'D:\鲁岳企业服务\重点群体项目\鲁岳企业服务_综合智能平台\dist\鲁岳企业服务_综合智能平台.exe'

exe = CArchiveReader(EXE_PATH)
toc = exe.toc
print(f'TOC entries: {len(toc)}')

# PyInstaller 6.x: toc 为 dict（文件名->条目）或 list[(name,...)]
names = list(toc.keys()) if isinstance(toc, dict) else [t[0] for t in toc]

for target in ('auth_config.json', 'version.json'):
    # 匹配条目名（可能含反斜杠路径）
    hit = None
    for n in names:
        base = os.path.basename(n)
        if base == target:
            hit = n
            break
    if not hit:
        print(f'[FAIL] {target} 未找到于 EXE 内嵌数据')
        continue
    data = exe.extract(hit)
    text = data.decode('utf-8', errors='ignore')
    print(f'--- {target} ({hit}) ---')
    try:
        obj = json.loads(text)
        if target == 'auth_config.json':
            print(json.dumps(obj, ensure_ascii=False, indent=2))
            assert 'pythonanywhere' not in text.lower(), '仍含 PythonAnywhere!'
            assert obj.get('auth_server_url', '').startswith('http://124.223.156.93'), '地址非腾讯云!'
            print('[OK] auth_config 已指向腾讯云认证服务器')
        else:
            # v1.1.47 起期望版本动态取自项目根 version.json（不再硬编码）
            with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   'version.json'), encoding='utf-8') as f:
                expect_ver = json.load(f).get('version')
            print(f"version={obj.get('version')} code={obj.get('version_code')}")
            print("download_url:", obj.get('download_url'))
            assert obj.get('version') == expect_ver, f'version 非 {expect_ver}!'
            assert 'myqcloud.com' in obj.get('download_url', ''), '下载地址非 COS!'
            print(f'[OK] version.json 为 v{expect_ver} 且下载地址指向 COS')
    except Exception as e:
        print(f'[FAIL] 解析 {target} 异常: {e}')
        print(text[:500])
        sys.exit(1)

print('\n验证全部通过 ✓')
