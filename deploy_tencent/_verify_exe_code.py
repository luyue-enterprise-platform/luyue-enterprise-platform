# -*- coding: utf-8 -*-
"""验证 EXE 内 Python 模块是否包含 v1.1.46 关键逻辑字符串
用法: python _verify_exe_code.py <exe路径>
"""
import os
import sys

from PyInstaller.archive.readers import CArchiveReader

EXE_PATH = sys.argv[1] if len(sys.argv) > 1 else \
    r'D:\鲁岳企业服务\重点群体项目\鲁岳企业服务_综合智能平台\dist\鲁岳企业服务_综合智能平台.exe'

# 需要确认的关键串 -> 所在模块（注意：注释不进字节码，针刺必须来自字符串常量/docstring）
CHECKS = {
    # v1.1.55 需求5 账号管理远程化（get_user_by_id 走云端列表 + 写操作不假成功回退）
    'core.auth': [
        '认证服务暂时不可用，请检查网络或联系管理员',
        '云端无单用户查询端点，走用户列表接口按 id 过滤',
        '非200不再静默回退本地',
    ],
    'app': [
        'https://luyue-1466112667.cos.ap-shanghai.myqcloud.com/version.json',
        'https://raw.githubusercontent.com/luyue-enterprise-platform/luyue-enterprise-platform/main/version.json',
        # v1.1.49 自动升级
        '/VERYSILENT',
        '/api/app/start_update',
        '/api/app/update_progress',
        'https://luyue-1466112667.cos.ap-shanghai.myqcloud.com/',
        # v1.1.52 安装器存活观察（f-string 片段，需子串匹配）
        '安装程序异常退出',
        # v1.1.56 无人值守升级：cmd 延时启动器 + 短观察后退出释放 EXE 锁
        # （docstring 针刺——区分“最终 cmd 延时版”与仅 CloseApplications=no 的中间版）
        '释放被锁定的运行中 EXE',
        '_UPDATE_LAUNCH_DELAY_SEC 秒后才真正 start 安装器',
        # v1.1.56 cmd 启动器代码常量（ComSpec 兜底字面量）
        'cmd.exe',
    ],
    # 注：launcher.py 是 PyInstaller 入口脚本，编入 bootloader 而非 PYZ，
    # 无法用 PYZ 提取针刺——其 v1.1.56 互斥/清理逻辑由 tests/test_v1_1_56.py 静态断言
    # v1.1.50 缴费单位误解析修复（序号/经办机构拒判 + 公司后缀投票兜底）
    # v1.1.51 中断信息明细剔除（'中断' 关键字 + 中断数据行日期正则）
    'modules.insurance.core.data_parser': [
        '现缴费单位名称',
        '对应缴费单位名称',
        '经办机构',
        '事务所',
        '中断',
        r'(\d{4}\s*[-./年]\s*\d{1,2})|(\d{6})',
        # v1.1.54 中断统计开始时间规则（中断结束月+1）
        '中断信息明细统计规则',
    ],
    # v1.1.53 劳动合同叠加比对（花名册合同列 + 有效参保期裁剪 + 操作记录提示）
    'modules.insurance.core.contract_overlap': [
        '无固定期限',
        '合同比对',
        '有效参保期',
        # v1.1.54 合同起止时间展示文本
        '劳动合同起止时间展示文本',
    ],
    'modules.insurance.core.roster_parser': [
        '合同起止',
        '劳动合同',
    ],
    'modules.insurance.blueprint': [
        '合同比对提示',
        # v1.1.54 公开 person_stats 合同展示字段
        '劳动合同起止时间展示文本',
        # v1.1.57 退税/抵税模式切换端点（路由常量 + 操作记录动作 + docstring 针刺）
        '/api/tax_mode/<task_id>',
        '切换税种模式',
        '切换退税/抵税模式（互斥单选）',
    ],
    # v1.1.54 统计表/预览列表新增劳动合同起止时间列
    # v1.1.55 需求4 打开即重算（docstring 针刺）
    'modules.insurance.core.excel_generator': [
        '劳动合同起止时间',
        '打开即重算',
        # v1.1.57 tax_mode 展示文案标签（字面量常量 + docstring 针刺）
        '抵税',
        'tax_mode（退税/抵税）',
        '抵税模式跳过年度台账生成',
    ],
    # v1.1.55 需求1 统计结果起点与统计时间段对齐（apply_stat_range_clamp docstring 针刺）
    'modules.insurance.core.stats_calculator': [
        '统计结果起点 = max(统计开始, 重叠起点)',
    ],
    # v2.0.0 可逆转换：反向引擎 + blueprint 分流 + 模块版本
    # v2.1.0 全面保真（A4 规范化 + 逐项校验）针刺合并于此
    'modules.pdf2word.core.to_pdf': [
        '批量转 PDF 主入口',
        '独立模式命名：与源文件同名仅换扩展名',
        'v2.1.0 全面保真',
        '宽度适配一页防截断',
    ],
    'modules.pdf2word.blueprint': [
        '与 _process_task 完全隔离',
        'output_mode',
        '该文件夹中没有可转换的文件',
        # v2.1.1 多文件夹 pick_ids 全部消费（getlist 修复）+ 失效 pick 提示（字面量）
        '一个文件夹选择',
        '请移除后重新选择文件夹',
    ],
    'modules.pdf2word': [
        '可逆转换',
        '2.1.0',
        # v2.1.0 双向保真（模块 docstring 针刺）
        'v2.1.0 双向保真',
    ],
    # v2.1.0 A4 规范化引擎 + 逐项校验器
    'modules.pdf2word.core.page_norm': [
        'A4 页面规范化引擎',
        '方向绝不改变',
    ],
    'modules.pdf2word.core.validator': [
        '转换校验器',
        '自动修正后重新校验',
    ],
    # v2.0.0 多省份：模板引擎 + 省份必选端点
    'modules.insurance.core.template_engine': [
        '省份完全以用户手动选择为准',
        '按用户所选省份路由模板并解析',
        '该省份暂未支持',
        # v2.0.1 省份功能收敛为仅读取规则匹配（docstring 针刺——区分收敛版与盖章版）
        '不添加任何额外字段',
        '供补充上传沿用',
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
    # 子串匹配：f-string 会被编译成若干常量片段，精确匹配整串会漏报
    return any(needle in s for s in seen)


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
