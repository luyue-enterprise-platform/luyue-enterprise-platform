# -*- coding: utf-8 -*-
"""
Excel 员工名单读取模块
支持 .xlsx / .xls / .csv 格式
"""

import csv
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import openpyxl

from .config import EXCEL_COLUMN_MAP


@dataclass
class Employee:
    """员工信息"""
    name: str = ""
    id_card: str = ""
    phone: str = ""
    remark: str = ""
    row_index: int = 0          # Excel 中的行号（便于报错定位）
    raw: dict = field(default_factory=dict)  # 原始整行数据

    @property
    def display(self) -> str:
        return f"{self.name}({self.id_card[-4:] if len(self.id_card) >= 4 else self.id_card})"


def _normalize_header(header: str) -> str:
    """标准化表头：去空格、换行、统一为小写中文"""
    if header is None:
        return ""
    return str(header).strip().replace("\n", "").replace(" ", "")


def _match_column(header: str, candidates: list[str]) -> bool:
    """检查表头是否匹配候选列名之一"""
    h = _normalize_header(header)
    for c in candidates:
        if _normalize_header(c) == h:
            return True
    return False


def _validate_id_card(id_card: str) -> tuple[bool, str]:
    """
    简单校验身份证号格式
    返回 (是否有效, 错误信息)
    """
    id_card = id_card.strip()
    if not id_card:
        return False, "身份证号为空"
    # 18位或15位
    if not re.match(r'^[0-9]{15}$|^[0-9]{17}[0-9Xx]$', id_card):
        return False, f"身份证号格式不正确: {id_card}"
    return True, ""


def read_excel(file_path: str) -> list[Employee]:
    """
    读取 Excel 员工名单

    支持格式: .xlsx, .xls, .csv
    要求第一行为表头，包含"姓名"和"身份证号"列

    返回: Employee 列表
    抛出: ValueError 当文件格式不对或缺少必要列时
    """
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    suffix = p.suffix.lower()

    if suffix == ".csv":
        rows, headers = _read_csv(p)
    elif suffix in (".xlsx", ".xls"):
        rows, headers = _read_xlsx(p)
    else:
        raise ValueError(f"不支持的文件格式: {suffix}，请使用 .xlsx / .xls / .csv")

    if not rows:
        raise ValueError("文件中没有数据行")

    # 匹配列索引
    col_map = {}  # field_name -> column_index
    for field_name, candidates in EXCEL_COLUMN_MAP.items():
        for col_idx, header in enumerate(headers):
            if _match_column(header, candidates):
                col_map[field_name] = col_idx
                break

    # 必须有姓名和身份证号
    if "name" not in col_map:
        raise ValueError(
            f"未找到姓名列。请确保表头包含以下之一: {', '.join(EXCEL_COLUMN_MAP['name'])}"
        )
    if "id_card" not in col_map:
        raise ValueError(
            f"未找到身份证号列。请确保表头包含以下之一: {', '.join(EXCEL_COLUMN_MAP['id_card'])}"
        )

    # 解析数据行
    employees = []
    errors = []

    for row_idx, row in enumerate(rows, start=2):  # Excel 行号从 2 开始（1 是表头）
        emp = Employee(
            name=str(row[col_map["name"]]).strip() if col_map["name"] < len(row) else "",
            id_card=str(row[col_map["id_card"]]).strip() if col_map["id_card"] < len(row) else "",
            phone=str(row[col_map["phone"]]).strip() if "phone" in col_map and col_map["phone"] < len(row) else "",
            remark=str(row[col_map["remark"]]).strip() if "remark" in col_map and col_map["remark"] < len(row) else "",
            row_index=row_idx,
            raw={headers[i]: row[i] for i in range(min(len(headers), len(row)))},
        )

        # 去掉浮点型身份证号的小数点
        if emp.id_card and emp.id_card.endswith(".0"):
            emp.id_card = emp.id_card[:-2]

        # 校验
        ok, msg = _validate_id_card(emp.id_card)
        if not ok:
            errors.append(f"第{row_idx}行: {msg}")
            continue

        if not emp.name or emp.name.lower() == "none":
            errors.append(f"第{row_idx}行: 姓名为空")
            continue

        employees.append(emp)

    if errors:
        # 有错误但不阻止，返回有效数据和错误列表
        return employees, errors

    return employees, []


def _read_xlsx(file_path: Path) -> tuple[list[list], list[str]]:
    """读取 xlsx 文件"""
    wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
    ws = wb.active

    all_rows = list(ws.iter_rows(values_only=True))
    if not all_rows:
        raise ValueError("Excel 文件为空")

    headers = [str(h) if h is not None else "" for h in all_rows[0]]
    rows = []
    for row in all_rows[1:]:
        # 跳过全空行
        if all(c is None or str(c).strip() == "" for c in row):
            continue
        rows.append([str(c).strip() if c is not None else "" for c in row])

    wb.close()
    return rows, headers


def _read_csv(file_path: Path) -> tuple[list[list], list[str]]:
    """读取 CSV 文件"""
    # 尝试不同编码
    for encoding in ["utf-8-sig", "utf-8", "gbk", "gb2312", "gb18030"]:
        try:
            with open(file_path, "r", encoding=encoding, newline="") as f:
                reader = csv.reader(f)
                all_rows = list(reader)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("无法识别 CSV 文件编码")

    if not all_rows:
        raise ValueError("CSV 文件为空")

    headers = [str(h) if h is not None else "" for h in all_rows[0]]
    rows = []
    for row in all_rows[1:]:
        if all(c is None or str(c).strip() == "" for c in row):
            continue
        rows.append([str(c).strip() if c else "" for c in row])

    return rows, headers
