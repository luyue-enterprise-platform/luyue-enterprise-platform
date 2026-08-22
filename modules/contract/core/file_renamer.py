"""文件重命名模块 — 按花名册顺序对劳动合同图片重命名"""

import os
import re
import shutil
import logging

logger = logging.getLogger(__name__)

# 支持的图片格式
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff', '.tif', '.webp'}


def _extract_name(raw_name):
    """从原始名称中提取姓名（只读取姓名部分，忽略其他内容）

    处理格式：
      - "01张三"     -> "张三"
      - "1-张三"     -> "张三"
      - "01.张三"    -> "张三"
      - "1 张三"     -> "张三"
      - "张三"       -> "张三"
      - "张三_劳动合同" -> "张三"
      - "张三劳动合同.pdf_p1" -> "张三"
      - "扫描件_张三"  -> "张三"
      - "王刚虎机械厂" -> "王刚虎"  (剔除单位词"机械厂")

    关键逻辑：忽略序号、数字、字母、符号及其他与姓名无关的内容
    （业务词如"劳动合同/合同/扫描件"、单位词如"机械厂/公司"等先剔除），
    只读取姓名。
    """
    name = raw_name.strip()

    # 去掉扩展名（仅当调用者传入带扩展名时；按 basename 调用一般不会有）
    name = re.sub(r'\.[Pp][Dd][Ff]$', '', name)

    # 去掉PDF分页后缀 (如 xxx.pdf_p1 / xxx_p2)
    name = re.sub(r'_[Pp]\d+$', '', name)

    # 剔除业务词（与姓名无关的内容，出现在任意位置都忽略）
    name = re.sub(r'(劳动合同书|劳动合同|合同书|扫描件|复印件|电子版|合同|协议)', '', name)

    # 剔除单位/机构词（姓名后紧跟单位名的情况，如"王刚虎机械厂"）。
    # 仅当剔除后仍能提取到姓名时才采用，避免误伤含这些字的姓名。
    stripped = re.sub(
        r'(有限责任公司|有限公司|股份公司|机械厂|工厂|门市部|经营部|'
        r'事务所|工作室|服务中心|公司|集团|中心|车间|班组|厂|店|部)',
        '', name,
    )
    if re.search(r'[\u4e00-\u9fa5]{2,4}', stripped):
        name = stripped

    # 去掉常见业务前缀
    name = re.sub(r'^(扫描件|合同|劳动合同|合同书|协议)[_\-\s]*', '', name)

    # 剥离开头的序号部分 (支持 01、1、001 等数字 + 分隔符 . - _ 空格)
    name = re.sub(r'^\d+[\.\-_\s]*', '', name)

    # 取第一段连续汉字作为姓名（中文姓名通常 2-4 字）
    m = re.search(r'[\u4e00-\u9fa5]{2,4}', name)

    if m:
        return m.group()

    # 兜底：提取不到汉字时返回去掉前缀后的字符串（交由精确比对判定，不中则进未匹配）
    return name.strip()


def _match_roster_name(guessed, roster_map):
    """提取的姓名与花名册严格比对，一致才命中

    比对原则：只做"一致"比对，不做模糊/子串/字符重叠/前缀缩短匹配。
    不一致的文件进"未匹配"文件夹，由人工处理。

    Returns:
        str or None: 命中的花名册姓名，未命中返回 None
    """
    if not guessed:
        return None
    g = guessed.strip()
    if not g:
        return None

    # 完全一致才命中
    return g if g in roster_map else None


def rename_contract_images(file_paths, roster, output_dir, folder_hints=None):
    """按花名册顺序对劳动合同图片重命名

    命名规则：
    - 单人单图: {序号:02d}-{姓名}.{ext}
    - 单人多图: {序号:02d}-{姓名}(1).{ext}, {序号:02d}-{姓名}(2).{ext}, ...
    - 未匹配的图片放入"未匹配"子文件夹

    Args:
        file_paths: 图片文件路径列表
        roster: 花名册人员列表 [{'seq': 1, 'name': '张三'}, ...]
        output_dir: 输出目录
        folder_hints: 可选，{file_path: folder_name} 映射
                      当文件来自以姓名命名的文件夹时，用文件夹名直接匹配花名册

    Returns:
        dict: {
            'renamed': [{original, new_name, person_seq, person_name}],
            'unmatched': [original_filename],
            'total': int,
            'matched_count': int,
            'unmatched_count': int,
        }
    """
    os.makedirs(output_dir, exist_ok=True)

    # 建立花名册索引：姓名 -> 序号
    roster_map = {}
    for person in roster:
        name = person['name'].strip()
        if name:
            roster_map[name] = person['seq']

    # 提取文件名中的姓名（去除扩展名和路径）
    file_info = []  # [(original_path, original_name, guessed_name, source), ...]

    for fp in file_paths:
        basename = os.path.basename(fp)
        name_no_ext = os.path.splitext(basename)[0]

        # 优先使用文件夹名作为姓名来源
        folder_name = (folder_hints or {}).get(fp, '')
        if folder_name:
            # 文件夹名可能为"序号+姓名"格式（如"01张三"），剥离序号只取姓名
            guessed_name = _extract_name(folder_name)
            file_info.append((fp, basename, guessed_name, 'folder'))
            continue

        # 尝试从文件名中提取姓名
        guessed_name = _extract_name(name_no_ext)
        file_info.append((fp, basename, guessed_name, 'filename'))

    # 按花名册分组：每个花名册人员对应哪些文件
    person_files = {}  # {seq: [file_info]}
    unmatched = []

    for fp, basename, guessed_name, source in file_info:
        # 严格比对：提取的姓名与花名册一致才命中，不做模糊匹配
        hit = _match_roster_name(guessed_name, roster_map)
        if hit:
            seq = roster_map[hit]
            person_files.setdefault(seq, []).append((fp, basename, hit))
        else:
            unmatched.append(basename)
            logger.warning(f'未匹配: {basename!r} 提取姓名={guessed_name!r}, 花名册含 {len(roster_map)} 人')

    # 按花名册顺序重命名
    renamed = []

    for person in roster:
        seq = person['seq']
        name = person['name']
        files = person_files.get(seq, [])

        if not files:
            continue

        for idx, (fp, basename, _) in enumerate(files):
            ext = os.path.splitext(basename)[1].lower()
            if ext not in IMAGE_EXTENSIONS:
                ext = '.jpg'  # 兜底

            if len(files) == 1:
                new_name = f'{seq:02d}-{name}{ext}'
            else:
                new_name = f'{seq:02d}-{name}({idx + 1}){ext}'

            new_path = os.path.join(output_dir, new_name)

            # 处理重名（多个不同文件可能生成相同名称）
            counter = 1
            while os.path.exists(new_path):
                base = f'{seq:02d}-{name}'
                new_name = f'{base}({len(files) + counter}){ext}'
                new_path = os.path.join(output_dir, new_name)
                counter += 1

            shutil.copy2(fp, new_path)
            renamed.append({
                'original': basename,
                'new_name': new_name,
                'person_seq': seq,
                'person_name': name,
            })

    # 未匹配的文件复制到"未匹配"子文件夹
    unmatched_dir = os.path.join(output_dir, '未匹配')
    if unmatched:
        os.makedirs(unmatched_dir, exist_ok=True)
        for basename in unmatched:
            # 找到原始路径
            for fp, bn, _, _ in file_info:
                if bn == basename:
                    shutil.copy2(fp, os.path.join(unmatched_dir, basename))
                    break

    result = {
        'renamed': renamed,
        'unmatched': unmatched,
        'total': len(file_paths),
        'matched_count': len(renamed),
        'unmatched_count': len(unmatched),
        'output_dir': output_dir,
    }

    logger.info(f'文件重命名完成: 共 {result["total"]} 个文件, '
                f'匹配 {result["matched_count"]} 个, 未匹配 {result["unmatched_count"]} 个')

    return result
