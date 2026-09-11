# -*- coding: utf-8 -*-
"""验证打包产物内嵌数据文件：auth_config.json 指向腾讯云、version.json 与本地一致

v2.3.8 起 onedir 打包：数据文件不再嵌入 EXE，而是落盘在
dist\鲁岳企业服务_综合智能平台\_internal\ 下；本脚本兼容两种形态——
先查 EXE 内嵌（onefile 旧形态/回归用），再查 _internal 磁盘文件（onedir）。
用法: python _verify_exe.py <exe路径>
"""
import os
import sys
import json

from PyInstaller.archive.readers import CArchiveReader

EXE_PATH = sys.argv[1] if len(sys.argv) > 1 else \
    r'D:\鲁岳企业服务\重点群体项目\鲁岳企业服务_综合智能平台\dist\鲁岳企业服务_综合智能平台\鲁岳企业服务_综合智能平台.exe'

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根


def load_data_file(target):
    """返回 (文本, 来源说明)；onedir 优先查 _internal 磁盘，其次查 EXE 内嵌"""
    internal = os.path.join(os.path.dirname(EXE_PATH), '_internal', target)
    if os.path.isfile(internal):
        with open(internal, encoding='utf-8') as f:
            return f.read(), '_internal 磁盘文件（onedir）'
    # onefile 兼容：数据嵌在 EXE 的 CArchive 里
    exe = CArchiveReader(EXE_PATH)
    toc = exe.toc
    names = list(toc.keys()) if isinstance(toc, dict) else [t[0] for t in toc]
    for n in names:
        if os.path.basename(n) == target:
            return exe.extract(n).decode('utf-8', errors='ignore'), 'EXE 内嵌（onefile）'
    return None, None


print(f'验证目标: {EXE_PATH}')
assert os.path.isfile(EXE_PATH), 'EXE 不存在！'

for target in ('auth_config.json', 'version.json'):
    text, source = load_data_file(target)
    if not text:
        print(f'[FAIL] {target} 未找到（_internal 与 EXE 内嵌均无）')
        sys.exit(1)
    print(f'--- {target}（{source}） ---')
    try:
        obj = json.loads(text)
        if target == 'auth_config.json':
            print(json.dumps(obj, ensure_ascii=False, indent=2))
            assert 'pythonanywhere' not in text.lower(), '仍含 PythonAnywhere!'
            assert obj.get('auth_server_url', '').startswith('http://124.223.156.93'), '地址非腾讯云!'
            print('[OK] auth_config 已指向腾讯云认证服务器')
        else:
            # 期望版本动态取自项目根 version.json（不再硬编码）
            with open(os.path.join(BASE, 'version.json'), encoding='utf-8') as f:
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
