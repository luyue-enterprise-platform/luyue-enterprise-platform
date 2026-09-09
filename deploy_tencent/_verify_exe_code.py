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
        # v2.3.1 需求3：严格花名册索引（身份证号唯一标识，防重名错配）
        '花名册严格索引——身份证号为唯一匹配标识',
    ],
    # v2.3.1 需求1/3：重命名严格匹配 + 未匹配记录归异常图片保留原名
    'modules.insurance.core.file_organizer': [
        '重命名以身份证号为唯一匹配标识',
        '保留原文件名归入',
        '花名册外人员/无身份证号',
    ],
    'modules.insurance.blueprint': [
        '合同比对提示',
        # v1.1.54 公开 person_stats 合同展示字段
        '劳动合同起止时间展示文本',
        # v1.1.57 退税/抵税模式切换端点（路由常量 + 操作记录动作 + docstring 针刺）
        '/api/tax_mode/<task_id>',
        '切换税种模式',
        '切换退税/抵税模式（互斥单选）',
        # v2.3.1 需求1：花名册为统计唯一数据基准（记录级过滤 + 姓名以证号查册覆盖）
        '花名册为统计唯一数据基准',
        '身份证号不在花名册中，已剔除统计',
        '无身份证号，无法与花名册核实，已剔除统计',
        '花名册过滤',
        # v2.3.2 并行 OCR：有界窗口 + 原序归位 + 错误隔离（docstring 针刺）
        '并行识别全部图片',
        '有界窗口提交',
        '异常绝不抛出线程外',
        # v2.3.3 速度回归：默认回退串行（环境变量可开并行）+ 每会话限核开关
        'LY_OCR_WORKERS',
        '并行（workers>1）时才压每会话线程数',
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
        # v2.3.1 需求2：年度台账序号按行重排 + 需求3 仅证号匹配
        '序号按行重新排列（1..N），不沿用花名册原始序号',
        '仅按身份证号精确匹配',
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
        # v2.1.2 图片横竖版主体内容判定（convert_image_to_pdf docstring 针刺）
        '按主体内容判定横竖版',
    ],
    # v2.1.2 新增：图片主体内容方向判定模块（EXIF 归一化 + OCR 文字区域，水印鲁棒）
    'modules.pdf2word.core.content_orient': [
        '图片主体内容方向判定',
        '依据图片实际主体内容',
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
        # v2.1.2 图片校验方向按主体内容预期（validate_image_to_pdf docstring 针刺）
        '方向符合主体内容预期',
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
    # v2.2.0 新增 MCP 服务端：协议层 / 工具 / 适配器 / 令牌 / 路由
    'modules.mcp.core.protocol': [
        '以 JSON-RPC 2.0 实现 MCP 服务端所需方法',
        'luyue-enterprise-platform',
    ],
    'modules.mcp.core.security': [
        'MCP 访问令牌与总开关',
        'mcp_config.json',
    ],
    'modules.mcp.core.tasks': [
        'MCP 任务登记表',
    ],
    'modules.mcp.blueprint': [
        'MCP 服务端 — Flask Blueprint',
        '未授权：需要有效的 Bearer Token',
        '本服务仅支持 POST JSON-RPC（不提供 SSE 流）',
    ],
    # v2.2.1 瘦返回交付 + v2.3.0 集中式缺参/服务端等待
    # （⚠️ 同名键合并——此前 tools/adapters 各有重复键，后者覆盖前者致 v2.2.0 针刺未生效）
    'modules.mcp.core.artifacts': [
        'MCP 结果落盘与文件卡片',
        '不覆盖历史结果',
        'mcp-%s-%s-%s-%s.%s',
        'file://',
    ],
    'modules.mcp.core.tools': [
        'MCP 工具定义',
        'insurance_provinces',
        '社保智能核算：批量识别参保证明',
        '瘦返回交付模式（v2.2.1）',
        '完整明细已落盘',
        '任务尚未完成：状态=',
        # v2.3.0 集中式缺参校验 + Server 端等待（docstring/纯字面量针刺，
        # 勿用 % 格式串与 MAX_WAIT_SEC 表达式——均不进 co_consts）
        '集中式缺参校验（v2.3.0 设计）',
        '必填项缺失或非法，任务未提交',
        '轮询至终态或期限，完成即一次性返回瘦结果',
        # v2.3.2 合同两阶段 + 生效参数回显（字面量针刺）
        'effective_params',
        'needs_selection',
        'preview_only',
        'confirm_task_id',
        '重命名计划已生成，未执行任何重命名，等待确认',
    ],
    'modules.mcp.core.adapters': [
        'MCP 业务能力适配器',
        '省份不支持或未提供',
        '花名册解析为空',
        '交付模式（v2.2.1 瘦返回）',
        '禁止内联回传',
        # v2.3.2 合同两阶段：预览暂存计划 + 确认执行（docstring/字面量针刺）
        '同步生成重命名计划（不执行）',
        '按确认选择执行合同重命名',
        'waiting_confirm',
    ],
    # v2.3.2 并行 OCR：引擎改线程本地单例（每线程独立实例，规避跨线程风险）
    'modules.insurance.core.ocr_engine': [
        '线程本地单例',
        '将PDF每页渲染为PNG图片',
        # v2.3.3 限核传参（rapidocr 按 det_/cls_/rec_ 前缀分发到三个子模型会话）
        '设置后续创建引擎的每会话线程数',
        'det_intra_op_num_threads',
        'cls_intra_op_num_threads',
        'rec_intra_op_num_threads',
    ],
    # v2.3.1 需求4：合同整理同一人多图全角编号（1）（2）（3），首张必带（1）
    # （⚠️ f-string 片段如 （{idx + 1}） 不可作针刺，须用 docstring/日志字面量）
    'modules.contract.core.file_renamer': [
        '全部追加全角编号',
        '首张必带（1）',
        '仅一个文件时不带编号',
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
