"""花名册解析模块 — 支持 Excel (.xlsx/.xls) 和 CSV"""

import re
import csv
import logging

logger = logging.getLogger(__name__)


def parse_roster_from_table(file_path):
    """解析花名册文件，返回有序人员列表
    
    Args:
        file_path: Excel或CSV文件路径
    
    Returns:
        [{'seq': 1, 'name': '张三', 'idcard': '...'}, ...]
    """
    ext = file_path.rsplit('.', 1)[-1].lower() if '.' in file_path else ''
    if ext in ('xlsx', 'xls'):
        return _parse_excel(file_path)
    elif ext == 'csv':
        return _parse_csv(file_path)
    else:
        raise ValueError(f'不支持的花名册格式: .{ext}，请使用 .xlsx / .xls / .csv')


def _parse_excel(file_path):
    """解析Excel花名册"""
    try:
        import openpyxl
    except ImportError:
        raise ImportError('请安装 openpyxl: pip install openpyxl')
    
    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    ws = wb.active
    
    rows = []
    for row in ws.iter_rows(values_only=True):
        rows.append([str(c) if c is not None else '' for c in row])
    
    wb.close()
    return _extract_roster_from_rows(rows)


def _parse_csv(file_path):
    """解析CSV花名册"""
    rows = []
    for encoding in ['utf-8-sig', 'utf-8', 'gbk', 'gb2312', 'gb18030']:
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                reader = csv.reader(f)
                for row in reader:
                    rows.append([str(c) if c is not None else '' for c in row])
            break
        except (UnicodeDecodeError, UnicodeError):
            rows = []
            continue
    
    if not rows:
        raise ValueError('无法识别CSV文件编码，请保存为UTF-8格式')
    
    return _extract_roster_from_rows(rows)


def _extract_roster_from_rows(rows):
    """从表格行中提取人员信息"""
    if not rows:
        return []
    
    # 在前10行中查找表头
    name_col = None
    seq_col = None
    idcard_col = None
    
    header_keywords = {
        'name': ['姓名', '名字', '员工姓名', '人员姓名', '姓  名'],
        'seq': ['序号', '编号', '序', '行号', 'No', 'NO'],
        'idcard': ['身份证', '身份证号', '证件号码', '身份证号码', '身份证明', '证件号'],
    }
    
    header_row = 0
    for row_idx in range(min(10, len(rows))):
        for col_idx, cell in enumerate(rows[row_idx]):
            cell_clean = cell.strip()
            if name_col is None and any(kw in cell_clean for kw in header_keywords['name']):
                name_col = col_idx
            if seq_col is None and any(kw == cell_clean or kw in cell_clean for kw in header_keywords['seq']):
                seq_col = col_idx
            if idcard_col is None and any(kw in cell_clean for kw in header_keywords['idcard']):
                idcard_col = col_idx
        
        if name_col is not None:
            header_row = row_idx
            break
    
    if name_col is None:
        raise ValueError('花名册中未找到"姓名"列，请检查表头是否包含"姓名"')
    
    # 数据从表头下一行开始
    data_start = header_row + 1
    
    # 跳过表头词汇的黑名单
    header_words = {
        '姓名', '名字', '序号', '编号', '身份证号', '证件号码', '身份证号码',
        '人员姓名', '员工姓名', '序', '行号', 'No', 'NO', '身份证明',
        '姓  名', '人员身份类型', '身份类型', '人员类别', '身份', '人员类型',
        '备注', '说明', '表头', '合计', '总计', '小计', '示例',
        '退休时间', '退役时间', '退役证编号', '联系电话', '手机号',
        '性别', '年龄', '部门', '岗位', '入职时间',
    }
    
    persons = []
    # v1.1.36: 不再按姓名去重 —— 花名册中同名员工全部保留（各自序号），
    # 由重命名阶段检测同名冲突并交人工确认
    for row in rows[data_start:]:
        if name_col >= len(row):
            continue
        name = row[name_col].strip()

        # 跳过空行和非中文姓名
        if not name or not re.search(r'[\u4e00-\u9fa5]', name):
            continue
        if name in header_words:
            continue
        
        # 如果姓名过长（可能包含额外信息），只取前2-4个中文字符
        name = re.match(r'[\u4e00-\u9fa5]{2,4}', name)
        if not name:
            continue
        name = name.group()
        
        seq = None
        if seq_col is not None and seq_col < len(row):
            try:
                seq = int(float(row[seq_col].strip()))
            except (ValueError, TypeError):
                pass
        
        idcard = ''
        if idcard_col is not None and idcard_col < len(row):
            raw = row[idcard_col].strip()
            # 清理浮点数格式的身份证号
            raw = re.sub(r'\.0+$', '', raw)
            if re.match(r'^\d{15,18}[\dXx]?$', raw):
                idcard = raw
        
        persons.append({'name': name, 'idcard': idcard, '_raw_seq': seq})
    
    # 保持花名册原有顺序，不进行自动排序
    # 使用花名册中的原始序号；无序号列则按出现顺序自动编号
    for i, p in enumerate(persons):
        if p.get('_raw_seq') is not None:
            p['seq'] = p['_raw_seq']
        else:
            p['seq'] = i + 1
        del p['_raw_seq']
    
    logger.info(f'花名册解析完成：共 {len(persons)} 人')
    return persons
