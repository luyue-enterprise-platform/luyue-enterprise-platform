"""文件重命名模块 — 按花名册顺序对劳动合同图片重命名"""

import os
import re
import shutil
import logging

logger = logging.getLogger(__name__)

# 支持的图片格式
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff', '.tif', '.webp'}


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
            # 文件夹名即为姓名，直接使用
            guessed_name = folder_name.strip()
            file_info.append((fp, basename, guessed_name, 'folder'))
            continue

        # 尝试从文件名中提取姓名
        # 常见格式: "张三.png", "张三.jpg", "张三劳动合同.pdf_p1.png"
        # 去掉PDF转换后缀
        clean_name = re.sub(r'_p\d+$', '', name_no_ext)
        # 去掉可能的前缀（如扫描件_、合同_等）
        clean_name = re.sub(r'^(扫描件|合同|劳动合同|合同书|协议)_*', '', clean_name)
        # 提取中文字符作为姓名
        chinese_part = re.findall(r'[\u4e00-\u9fa5]{2,4}', clean_name)

        guessed_name = chinese_part[0] if chinese_part else clean_name
        file_info.append((fp, basename, guessed_name, 'filename'))

    # 按花名册分组：每个花名册人员对应哪些文件
    person_files = {}  # {seq: [file_info]}
    unmatched = []

    for fp, basename, guessed_name, source in file_info:
        matched = False
        # 精确匹配
        if guessed_name in roster_map:
            seq = roster_map[guessed_name]
            person_files.setdefault(seq, []).append((fp, basename, guessed_name))
            matched = True
        else:
            # 模糊匹配：文件名中包含花名册姓名
            for roster_name, seq in roster_map.items():
                if roster_name in guessed_name or guessed_name in roster_name:
                    person_files.setdefault(seq, []).append((fp, basename, roster_name))
                    matched = True
                    break

        if not matched:
            unmatched.append(basename)

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
