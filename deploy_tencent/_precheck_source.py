# -*- coding: utf-8 -*-
"""v2.3.1 打包前源码针刺预检：用 ast 提取 _verify_exe_code.py 的 CHECKS，
对每个模块 find_spec+get_code 跑同款 walk，确保所有针刺在源码字节码常量中命中
（防止打包后 _verify_exe_code.py 才发现 MISS 返工）。不 import 该脚本——其主体在
导入时会执行 EXE 验证。"""
import ast
import importlib.util
import sys

BASE = r'D:\鲁岳企业服务\重点群体项目\鲁岳企业服务_综合智能平台'
sys.path.insert(0, BASE)

with open(BASE + r'\deploy_tencent\_verify_exe_code.py', encoding='utf-8') as f:
    tree = ast.parse(f.read())
CHECKS = None
for node in tree.body:
    if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == 'CHECKS':
        CHECKS = ast.literal_eval(node.value)
assert CHECKS, '未从 _verify_exe_code.py 提取到 CHECKS'


def collect_consts(code, out):
    for c in code.co_consts:
        if hasattr(c, 'co_consts'):
            collect_consts(c, out)
        elif isinstance(c, str):
            out.add(c)
        elif isinstance(c, tuple):
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
    return any(needle in s for s in seen)


ok = True
for mod, needles in CHECKS.items():
    spec = importlib.util.find_spec(mod)
    if spec is None or spec.loader is None:
        print(f'--- 模块 {mod} --- [FAIL] 找不到模块')
        ok = False
        continue
    code = spec.loader.get_code(mod)
    print(f'--- 模块 {mod} ---')
    for needle in needles:
        if has_string(code, needle):
            print(f'  [OK] {needle[:70]}')
        else:
            print(f'  [MISS] {needle[:70]}')
            ok = False

print('\n源码预检', '通过 ✓' if ok else '存在缺失 ✗')
sys.exit(0 if ok else 1)
