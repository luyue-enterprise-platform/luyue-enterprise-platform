# -*- coding: utf-8 -*-
"""文件整理模块 - 按花名册重命名图片，按险种分文件夹归类

异常图片（识别失败/无参保时间段/缴费单位不一致被排除）单独归类到"异常图片"文件夹，
方便用户检查并重新提交识别。
"""
import os
import re
import shutil

from .roster_parser import match_person_to_roster, match_record_to_roster


# 18位身份证号校验码权重表与校验码字符集（GB 11643-1999）
_IDCARD_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_IDCARD_CODES = '10X98765432'


def _validate_idcard(idcard):
    """校验并规范化身份证号，返回规范化的号码字符串；不合法返回 ''

    支持：
    - 18位新版：前17位数字 + 末位校验码（数字或X），校验码按 GB 11643-1999 加权模11算法验证
    - 15位旧版：纯数字（出生年份缺世纪位，无校验码，仅做格式校验）

    入参先做清理：去首尾空格、去内部所有空白字符、字母转大写。
    """
    if not idcard:
        return ''
    # 清理：strip + 去掉所有空白字符 + 转大写（x → X）
    cleaned = re.sub(r'\s+', '', str(idcard)).upper()
    if not cleaned:
        return ''
    # 15位旧版身份证：纯数字即可
    if re.fullmatch(r'\d{15}', cleaned):
        return cleaned
    # 18位：前17位数字 + 末位数字或X，且校验码必须正确
    m = re.fullmatch(r'(\d{17})([0-9X])', cleaned)
    if not m:
        return ''
    digits, check = m.group(1), m.group(2)
    total = sum(int(d) * w for d, w in zip(digits, _IDCARD_WEIGHTS))
    expected = _IDCARD_CODES[total % 11]
    if check != expected:
        return ''
    return cleaned

# 险种对应的文件夹名
INSURANCE_FOLDER_MAP = {
    '养老保险': '养老保险参保证明',
    '医疗保险': '医疗保险参保证明',
    '工伤保险': '工伤保险参保证明',
    '失业保险': '失业保险参保证明',
}

# 异常图片文件夹名
ABNORMAL_FOLDER = '异常图片'


def _is_abnormal(rec):
    """判断该OCR记录是否为异常图片（需要单独归类）

    异常条件：
    1. 有 _excluded 标记（缴费单位不一致，被排除不计入统计）
    2. 有 error 字段（OCR识别失败）
    3. 无姓名且无身份证号（未识别出有效信息）
    4. 无参保时间段 period（period为None或空）
    """
    # 条件1：缴费单位不一致，被排除
    if rec.get('_excluded'):
        return True
    # 条件2：OCR错误
    if rec.get('error'):
        return True
    # 条件3：无姓名且无身份证号
    if not rec.get('name') and not rec.get('idcard'):
        return True
    # 条件4：无参保时间段
    period = rec.get('period')
    if not period:
        return True
    return False


def organize_files(ocr_results, roster, output_dir):
    """
    将识别后的图片按花名册重命名，并按险种分文件夹归类

    异常图片（识别失败/无参保时间段/缴费单位不一致）单独放入"异常图片"文件夹。

    Args:
        ocr_results: list of parse_ocr_result()返回的dict，每条包含:
            - filename: 原始文件名
            - name: OCR识别的姓名
            - insurance_type: 险种名称
            - period: (start_ym, end_ym) 参保时间段 或 None
            - _source_path: 图片的实际路径（由调用方添加）
            - _source_origin: 原始源文件名（PDF多页时用于去重，可选）
            - _excluded: bool  缴费单位不一致被排除（可选，由调用方添加）
        roster: list of {'seq': int, 'name': str}  花名册
        output_dir: 整理后的文件输出根目录

    Returns:
        dict: {
            'organized_count': 成功整理的文件数,
            'folder_structure': {文件夹名: [文件名列表]},
            'unmatched': [未匹配到花名册的文件信息],
            'no_roster': bool  是否没有花名册,
            'abnormal_count': int  异常图片数量,
        }
    """

    result = {
        'organized_count': 0,
        'folder_structure': {},
        'unmatched': [],
        'no_roster': len(roster) == 0,
        'abnormal_count': 0,
    }

    # 创建险种文件夹
    for folder_name in INSURANCE_FOLDER_MAP.values():
        folder_path = os.path.join(output_dir, folder_name)
        os.makedirs(folder_path, exist_ok=True)
        result['folder_structure'][folder_name] = []

    # 创建异常图片文件夹
    abnormal_dir = os.path.join(output_dir, ABNORMAL_FOLDER)
    os.makedirs(abnormal_dir, exist_ok=True)
    result['folder_structure'][ABNORMAL_FOLDER] = []

    # 避免重名（同一文件夹内同一人可能有多张图片，如多页PDF）
    # 计数器按文件夹独立，这样不同险种文件夹内的"01-张三.jpg"不会有多余后缀
    name_counter = {}  # key: "文件夹名/01-张三", count

    # PDF多页去重：同一源文件（如同一个PDF）只整理一次，避免"01-张三_2"等多余文件
    organized_origins = set()

    for rec in ocr_results:
        name = rec.get('name', '')
        ins_type = rec.get('insurance_type')
        src_path = rec.get('_source_path', '')
        source_origin = rec.get('_source_origin') or os.path.basename(src_path)

        if not src_path or not os.path.exists(src_path):
            continue

        # 同一源文件内同一人同险种只整理一次（避免PDF多页产生 02-王宝利_2 等多余文件）
        # 多页PDF若包含多个不同人员，仍会为每个人保留一条记录
        origin_key = (source_origin, name, ins_type)
        if origin_key in organized_origins:
            continue
        organized_origins.add(origin_key)

        # === 异常图片判定：识别失败/无时间段/缴费单位不一致 → 放入异常文件夹 ===
        if _is_abnormal(rec):
            # 保留原文件名（异常图片不做花名册重命名，方便用户定位问题）
            orig_filename = rec.get('filename', os.path.basename(src_path))
            ext = os.path.splitext(src_path)[1]
            if not ext:
                ext = '.jpg'

            # 处理重名
            abnormal_basename = os.path.splitext(orig_filename)[0]
            counter_key = ABNORMAL_FOLDER + '/' + abnormal_basename
            if counter_key in name_counter:
                name_counter[counter_key] += 1
                abnormal_basename = f'{abnormal_basename}_{name_counter[counter_key]}'
            else:
                name_counter[counter_key] = 1

            new_filename = abnormal_basename + ext
            dest_path = os.path.join(abnormal_dir, new_filename)
            try:
                shutil.copy2(src_path, dest_path)
                result['folder_structure'][ABNORMAL_FOLDER].append(new_filename)
                result['abnormal_count'] += 1
            except Exception as e:
                result['unmatched'].append({
                    'filename': orig_filename,
                    'reason': f'异常图片复制失败: {e}'
                })
            continue

        if not ins_type:
            result['unmatched'].append({
                'filename': rec.get('filename', ''),
                'reason': '未识别险种'
            })
            continue

        folder_name = INSURANCE_FOLDER_MAP.get(ins_type)
        if not folder_name:
            result['unmatched'].append({
                'filename': rec.get('filename', ''),
                'reason': f'未知险种: {ins_type}'
            })
            continue

        # 匹配花名册（v1.1.34: 身份证号优先匹配，姓名匹配兜底）
        # 身份证号一致 → 图片命名为"花名册中序号+姓名"（格式保持 序号-姓名）
        matched = None
        if roster:
            matched = match_record_to_roster(rec, roster)

        if matched:
            seq_str = f'{matched["seq"]:02d}'
            # v1.1.43: 重命名为"序号-姓名-身份证号"组合格式
            # 身份证号取花名册优先，校验合法才拼接，避免拼进错误号码；
            # 花名册号缺失/不合法时回退用OCR识别的号码，仍不合法则回退"序号-姓名"
            valid_id = _validate_idcard(matched.get('idcard', ''))
            if not valid_id:
                valid_id = _validate_idcard(rec.get('idcard', ''))
            if valid_id:
                new_basename = f'{seq_str}-{matched["name"]}-{valid_id}'
            else:
                new_basename = f'{seq_str}-{matched["name"]}'
        elif name:
            new_basename = name
        else:
            new_basename = os.path.splitext(rec.get('filename', 'unknown'))[0]

        # 处理重名：同一文件夹内同名加序号后缀
        counter_key = folder_name + '/' + new_basename
        if counter_key in name_counter:
            name_counter[counter_key] += 1
            new_basename = f'{new_basename}_{name_counter[counter_key]}'
        else:
            name_counter[counter_key] = 1

        # 保留原扩展名（统一用.jpg，因为PDF已转为图片）
        ext = os.path.splitext(src_path)[1]
        if not ext:
            ext = '.jpg'
        new_filename = new_basename + ext

        # 复制文件到险种文件夹
        dest_path = os.path.join(output_dir, folder_name, new_filename)
        try:
            shutil.copy2(src_path, dest_path)
            result['folder_structure'][folder_name].append(new_filename)
            result['organized_count'] += 1
        except Exception as e:
            result['unmatched'].append({
                'filename': rec.get('filename', ''),
                'reason': f'文件复制失败: {e}'
            })

    return result
