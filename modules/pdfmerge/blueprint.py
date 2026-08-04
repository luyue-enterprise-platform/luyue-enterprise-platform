# -*- coding: utf-8 -*-
"""
留存备查资料汇总PDF智能生成系统 — Flask Blueprint
"""

import os
import sys
import uuid
import shutil
import logging
import traceback
import threading
from datetime import datetime

from flask import (
    Blueprint, render_template, request, jsonify,
    send_file, Response
)

from core.auth import login_required

# ===== 路径设置 =====
IS_FROZEN = getattr(sys, 'frozen', False)
if IS_FROZEN:
    RESOURCE_DIR = os.path.join(sys._MEIPASS, 'modules', 'pdfmerge')
    DATA_DIR = os.path.dirname(sys.executable)
else:
    RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ===== Blueprint 创建 =====
pdfmerge_bp = Blueprint(
    'pdfmerge',
    __name__,
    url_prefix='/pdfmerge',
    template_folder=os.path.join(RESOURCE_DIR, 'templates'),
    static_folder=os.path.join(RESOURCE_DIR, 'static')
)

UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
OUTPUT_DIR = os.path.join(DATA_DIR, 'outputs')
LOG_DIR = os.path.join(DATA_DIR, 'logs')

for d in [UPLOAD_DIR, OUTPUT_DIR, LOG_DIR]:
    os.makedirs(d, exist_ok=True)

logger = logging.getLogger('pdfmerge')

# ===== 任务状态存储 =====
tasks = {}
tasks_lock = threading.Lock()


# ===== 路由 =====

@pdfmerge_bp.route('/')
@login_required
def index():
    """主页面"""
    return render_template('pdfmerge_index.html')


@pdfmerge_bp.route('/api/select_folder', methods=['POST'])
@login_required
def api_select_folder():
    """弹出系统原生文件夹选择对话框，扫描文件夹内文件"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        folder_path = filedialog.askdirectory(
            title='选择留存备查资料文件夹',
            initialdir=os.path.expanduser('~')
        )
        root.destroy()
    except Exception as e:
        logger.error(f'文件夹选择对话框异常: {e}')
        return jsonify({'error': f'无法打开文件夹选择对话框: {e}'}), 500

    if not folder_path:
        return jsonify({'cancelled': True})

    if not os.path.isdir(folder_path):
        return jsonify({'error': '选择的路径不是文件夹'}), 400

    # 扫描文件夹内的所有支持文件（递归包含子文件夹）
    from .core.sections import SUPPORTED_EXTENSIONS
    file_list = []
    walk_errors = []

    def _on_walk_error(err):
        walk_errors.append(str(err))
        logger.warning(f'文件夹扫描错误: {err}')

    for root_dir, dirs, files in os.walk(folder_path, onerror=_on_walk_error):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                full_path = os.path.join(root_dir, f)
                rel_path = os.path.relpath(full_path, folder_path)
                try:
                    file_size = os.path.getsize(full_path)
                except Exception as e:
                    logger.warning(f'无法获取文件大小，跳过: {full_path} ({e})')
                    continue
                file_list.append({
                    'name': f,
                    'path': rel_path,
                    'abs_path': full_path,
                    'ext': ext,
                    'size': file_size,
                })

    if walk_errors:
        logger.warning(f'文件夹扫描完成，但有 {len(walk_errors)} 个目录访问错误: {walk_errors[:3]}')
    logger.info(f'文件夹扫描完成: {folder_path} -> {len(file_list)} 个支持文件')

    return jsonify({
        'ok': True,
        'folder_path': folder_path,
        'file_count': len(file_list),
        'files': file_list,
    })


@pdfmerge_bp.route('/api/select_files', methods=['POST'])
@login_required
def api_select_files():
    """弹出系统原生文件选择对话框（支持多选）"""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        file_paths = filedialog.askopenfilenames(
            title='选择留存备查资料文件（可多选）',
            filetypes=[
                ('所有支持的文件', '*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff *.doc *.docx *.xls *.xlsx'),
                ('PDF文件', '*.pdf'),
                ('图片文件', '*.png *.jpg *.jpeg *.bmp *.tif *.tiff'),
                ('Word文件', '*.doc *.docx'),
                ('Excel文件', '*.xls *.xlsx'),
            ],
            initialdir=os.path.expanduser('~')
        )
        root.destroy()
    except Exception as e:
        logger.error(f'文件选择对话框异常: {e}')
        return jsonify({'error': f'无法打开文件选择对话框: {e}'}), 500

    if not file_paths:
        return jsonify({'cancelled': True})

    # 过滤支持的文件格式
    from .core.sections import SUPPORTED_EXTENSIONS
    file_list = []
    for fp in file_paths:
        ext = os.path.splitext(fp)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue
        try:
            file_size = os.path.getsize(fp)
        except Exception as e:
            logger.warning(f'无法获取文件大小，跳过: {fp} ({e})')
            continue
        file_list.append({
            'name': os.path.basename(fp),
            'abs_path': fp,
            'ext': ext,
            'size': file_size,
        })

    logger.info(f'文件选择完成: {len(file_list)} 个文件')

    return jsonify({
        'ok': True,
        'file_count': len(file_list),
        'files': file_list,
    })


@pdfmerge_bp.route('/api/scan_match', methods=['POST'])
@login_required
def api_scan_match():
    """扫描文件列表并匹配文件到各章节（不生成PDF）"""
    data = request.get_json(silent=True) or {}
    file_paths = data.get('file_paths', [])
    mode = data.get('mode', 'refund')  # refund / deduction

    if not file_paths:
        return jsonify({'error': '请先选择文件'}), 400

    if mode not in ('refund', 'deduction'):
        return jsonify({'error': '模式必须为 refund 或 deduction'}), 400

    from .core.sections import match_files_from_paths

    try:
        result = match_files_from_paths(file_paths, mode)

        # 转换为前端友好的格式
        sections_data = []
        for sec in result['sections']:
            # 显示文件名（basename），方便用户识别
            file_display = [os.path.basename(f) for f in sec['files']]

            sections_data.append({
                'id': sec['id'],
                'name': sec['name'],
                'file_count': len(sec['files']),
                'files': file_display,
                'matched': sec['matched'],
                'required': sec['required'],
                'sort_by_roster': sec['sort_by_roster'],
            })

        return jsonify({
            'ok': True,
            'sections': sections_data,
            'unmatched_count': len(result['unmatched']),
            'unmatched_files': [os.path.basename(f) for f in result['unmatched']],
            'total_files': result['total_files'],
            'matched_files': result['matched_files'],
        })
    except Exception as e:
        logger.error(f'文件匹配失败: {e}\n{traceback.format_exc()}')
        return jsonify({'error': f'文件匹配失败: {e}'}), 500


@pdfmerge_bp.route('/api/generate', methods=['POST'])
@login_required
def api_generate():
    """启动PDF生成任务"""
    data = request.get_json(silent=True) or {}
    file_paths = data.get('file_paths', [])
    mode = data.get('mode', 'refund')
    cover_title = data.get('cover_title', '').strip()
    company_name = data.get('company_name', '').strip()
    period_start = data.get('period_start', '')
    period_end = data.get('period_end', '')
    deduction_period = data.get('deduction_period', '')  # 抵税模式的所属期
    proof_period = data.get('proof_period', '')  # 抵税模式的证明时间段

    if not file_paths:
        return jsonify({'error': '请先选择文件'}), 400

    if not cover_title:
        return jsonify({'error': '请输入封面名称'}), 400

    if mode not in ('refund', 'deduction'):
        return jsonify({'error': '模式必须为 refund 或 deduction'}), 400

    # 构建时间段文本
    if mode == 'refund':
        period_text = f'（证明时间段：{period_start}-{period_end}）' if period_start and period_end else ''
    else:
        # 抵税模式：封面同时显示所属期和证明时间段
        parts = []
        if deduction_period:
            parts.append(f'所属期{deduction_period}')
        if proof_period:
            parts.append(f'（证明时间段：{proof_period}）')
        period_text = ' '.join(parts)

    # 构建输出文件名
    if mode == 'refund':
        period_str = ''
        if period_start and period_end:
            period_str = f'{period_start}-{period_end}'
        output_filename = f'{company_name or cover_title}{period_str}申请重点群体备查材料（退税）.pdf'
    else:
        period_str = deduction_period or ''
        output_filename = f'{company_name or cover_title}所属期{period_str}申请重点群体备查材料（抵税）.pdf'

    # 清理文件名中的非法字符
    output_filename = output_filename.replace('/', '-').replace('\\', '-').replace(':', '-').replace('*', '').replace('?', '').replace('"', '').replace('<', '').replace('>', '').replace('|', '')

    task_id = uuid.uuid4().hex[:8]
    task_dir = os.path.join(OUTPUT_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    with tasks_lock:
        tasks[task_id] = {
            'status': 'processing',
            'current': 0,
            'total': 0,
            'message': '正在初始化...',
            'output_path': None,
            'output_filename': output_filename,
            'error': None,
        }

    # 启动后台生成线程
    thread = threading.Thread(
        target=_process_generate_task,
        args=(task_id, file_paths, mode, cover_title, company_name,
              period_text, output_filename, task_dir),
        daemon=True
    )
    thread.start()

    return jsonify({
        'ok': True,
        'task_id': task_id,
        'output_filename': output_filename,
    })


def _process_generate_task(task_id, file_paths, mode, cover_title, company_name,
                            period_text, output_filename, task_dir):
    """后台PDF生成任务"""
    try:
        from .core.sections import match_files_from_paths
        from .core.format_converter import convert_to_pdf
        from .core.pdf_builder import generate_cover, generate_toc, merge_pdfs, get_pdf_page_count

        def update_progress(current, total, message):
            with tasks_lock:
                tasks[task_id]['current'] = current
                tasks[task_id]['total'] = total
                tasks[task_id]['message'] = message

        # Step 1: 匹配文件
        update_progress(0, 100, '正在匹配文件...')
        match_result = match_files_from_paths(file_paths, mode)

        matched_sections = [s for s in match_result['sections'] if s['matched']]
        total_files = sum(len(s['files']) for s in matched_sections)

        if total_files == 0:
            with tasks_lock:
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['error'] = '未匹配到任何文件，请检查已添加的文件'
            return

        logger.info(f'[task:{task_id}] 匹配到 {total_files} 个文件, {len(matched_sections)} 个章节')

        # Step 2: 转换所有文件为PDF
        update_progress(0, total_files, '正在转换文件为PDF...')

        pdf_convert_dir = os.path.join(task_dir, 'converted_pdfs')
        os.makedirs(pdf_convert_dir, exist_ok=True)

        # 按章节顺序收集PDF路径
        section_pdfs = []  # [(section_name, [pdf_path, ...]), ...]
        converted_count = 0

        for section in match_result['sections']:
            if not section['matched']:
                continue

            section_pdf_paths = []
            for file_path in section['files']:
                converted_count += 1
                rel_name = os.path.basename(file_path)
                update_progress(converted_count, total_files,
                                f'正在转换 ({converted_count}/{total_files}): {rel_name}')

                logger.info(f'[task:{task_id}] 转换文件: {file_path}')
                pdf_path = convert_to_pdf(file_path, pdf_convert_dir)
                if pdf_path:
                    section_pdf_paths.append(pdf_path)
                else:
                    logger.warning(f'[task:{task_id}] 转换失败，跳过: {file_path}')

            if section_pdf_paths:
                section_pdfs.append((section['name'], section_pdf_paths))

        # Step 3: 生成封面
        update_progress(converted_count, total_files, '正在生成封面...')
        cover_pdf = generate_cover(cover_title, mode, company_name, period_text)

        # Step 4: 计算各章节页码偏移（封面占1页，目录占1-2页）
        # 先合并所有内容PDF，统计页数
        update_progress(converted_count, total_files, '正在合并PDF...')

        # 记录各章节在内容PDF中的位置（0-indexed，相对于内容部分）
        content_sections = []  # [{name, start, count}] 相对于内容部分起始
        content_current = 0

        # 合并所有章节内容
        content_pdfs = []
        for section_name, pdf_list in section_pdfs:
            section_page_count = sum(get_pdf_page_count(p) for p in pdf_list)
            content_sections.append({
                'name': section_name,
                'start': content_current,
                'count': section_page_count,
            })
            content_current += section_page_count
            content_pdfs.extend(pdf_list)

        # 先合并内容部分到一个临时文件
        content_merged_path = os.path.join(task_dir, '_content_merged.pdf')
        merge_pdfs(content_pdfs, content_merged_path)

        content_page_count = get_pdf_page_count(content_merged_path)

        # 估算目录页数（每页约25条目录）
        toc_entry_count = len(section_pdfs)
        toc_page_count = max(1, (toc_entry_count + 24) // 25)

        # 封面1页 + 目录toc_page_count页
        page_offset = 1 + toc_page_count

        # 计算各章节起始页码（绝对页码，含封面和目录）
        section_page_info = []  # [(section_name, start_page), ...]
        current_page = page_offset

        for section_name, pdf_list in section_pdfs:
            section_page_info.append((section_name, current_page))
            for pdf_path in pdf_list:
                current_page += get_pdf_page_count(pdf_path)

        # Step 5: 生成目录
        update_progress(converted_count, total_files, '正在生成目录...')
        toc_pdf = generate_toc(section_page_info, page_offset)

        # Step 6: 最终合并 封面 + 目录 + 内容
        update_progress(converted_count, total_files, '正在生成最终PDF...')
        final_pdfs = [cover_pdf, toc_pdf, content_merged_path]

        output_path = os.path.join(task_dir, output_filename)
        merge_pdfs(final_pdfs, output_path)

        # 清理临时文件（保留 content_merged_path 供页面编辑使用）
        for tmp in [cover_pdf, toc_pdf]:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass

        # 清理转换的PDF文件
        try:
            shutil.rmtree(pdf_convert_dir)
        except Exception:
            pass

        final_page_count = get_pdf_page_count(output_path)
        logger.info(f'[task:{task_id}] PDF生成完成: {output_filename} ({final_page_count}页)')

        with tasks_lock:
            tasks[task_id]['status'] = 'done'
            tasks[task_id]['message'] = f'PDF生成完成！共 {final_page_count} 页'
            tasks[task_id]['output_path'] = output_path
            # 存储页面编辑所需的状态
            tasks[task_id]['content_pdf_path'] = content_merged_path
            tasks[task_id]['content_sections'] = content_sections
            tasks[task_id]['cover_page_count'] = 1
            tasks[task_id]['toc_page_count'] = toc_page_count
            tasks[task_id]['cover_params'] = {
                'cover_title': cover_title,
                'mode': mode,
                'company_name': company_name,
                'period_text': period_text,
            }

    except Exception as e:
        logger.error(f'[task:{task_id}] 生成失败: {e}\n{traceback.format_exc()}')
        with tasks_lock:
            tasks[task_id]['status'] = 'error'
            tasks[task_id]['error'] = str(e)


@pdfmerge_bp.route('/api/progress/<task_id>')
@login_required
def api_progress(task_id):
    """查询任务进度"""
    with tasks_lock:
        task = tasks.get(task_id)

    if not task:
        return jsonify({'error': '任务不存在'}), 404

    return jsonify({
        'status': task['status'],
        'current': task['current'],
        'total': task['total'],
        'message': task['message'],
        'error': task.get('error'),
        'output_filename': task.get('output_filename'),
    })


@pdfmerge_bp.route('/api/download/<task_id>')
@login_required
def api_download(task_id):
    """下载生成的PDF文件"""
    with tasks_lock:
        task = tasks.get(task_id)

    if not task or task['status'] != 'done':
        return jsonify({'error': '文件不可用'}), 404

    output_path = task.get('output_path')
    if not output_path or not os.path.isfile(output_path):
        return jsonify({'error': 'PDF文件不存在'}), 404

    return send_file(
        output_path,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=task.get('output_filename', 'output.pdf')
    )


@pdfmerge_bp.route('/api/save_to/<task_id>', methods=['POST'])
@login_required
def api_save_to(task_id):
    """弹出系统原生文件夹选择对话框，将PDF保存到用户选择的位置"""
    with tasks_lock:
        task = tasks.get(task_id)

    if not task or task['status'] != 'done':
        return jsonify({'error': '文件不可用'}), 404

    output_path = task.get('output_path')
    if not output_path or not os.path.isfile(output_path):
        return jsonify({'error': 'PDF文件不存在'}), 404

    # 使用 tkinter 弹出系统原生文件夹选择对话框
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        save_dir = filedialog.askdirectory(
            title='选择保存位置（PDF文件将保存到此文件夹）',
            initialdir=os.path.expanduser('~')
        )
        root.destroy()
    except Exception as e:
        logger.error(f'保存对话框异常: {e}')
        return jsonify({'error': f'无法打开保存对话框: {e}'}), 500

    if not save_dir:
        return jsonify({'cancelled': True})

    # 复制文件到用户选择的目录
    filename = task.get('output_filename', 'output.pdf')
    dest_path = os.path.join(save_dir, filename)
    shutil.copy2(output_path, dest_path)

    logger.info(f'[task:{task_id}] PDF已保存到: {dest_path}')

    return jsonify({
        'ok': True,
        'save_dir': save_dir,
        'filename': filename,
    })


@pdfmerge_bp.route('/api/capabilities')
@login_required
def api_capabilities():
    """查询系统的文件转换能力"""
    from .core.format_converter import check_conversion_capability
    caps = check_conversion_capability()
    return jsonify({'ok': True, 'capabilities': caps})


# ===== 页面编辑功能 =====

@pdfmerge_bp.route('/api/pages/<task_id>')
@login_required
def api_get_pages(task_id):
    """获取PDF页面信息（总页数、章节分布、封面/目录页数）"""
    with tasks_lock:
        task = tasks.get(task_id)

    if not task or task['status'] != 'done':
        return jsonify({'error': '任务不存在或未完成'}), 404

    content_pdf_path = task.get('content_pdf_path')
    if not content_pdf_path or not os.path.isfile(content_pdf_path):
        return jsonify({'error': '内容PDF文件不存在'}), 404

    from .core.pdf_builder import get_pdf_page_count

    cover_count = task.get('cover_page_count', 1)
    toc_count = task.get('toc_page_count', 1)
    content_count = get_pdf_page_count(content_pdf_path)
    total_pages = cover_count + toc_count + content_count

    # 构建章节信息（转换为绝对页码）
    sections = []
    for sec in task.get('content_sections', []):
        sections.append({
            'name': sec['name'],
            'start_page': sec['start'] + cover_count + toc_count + 1,  # 1-indexed PDF page
            'content_start': sec['start'],  # 0-indexed relative to content
            'count': sec['count'],
        })

    return jsonify({
        'ok': True,
        'total_pages': total_pages,
        'cover_count': cover_count,
        'toc_count': toc_count,
        'content_count': content_count,
        'sections': sections,
    })


@pdfmerge_bp.route('/api/pages/<task_id>/thumbnail/<int:page>')
@login_required
def api_page_thumbnail(task_id, page):
    """获取指定页面的缩略图（page为0-indexed）"""
    with tasks_lock:
        task = tasks.get(task_id)

    if not task or task['status'] != 'done':
        return jsonify({'error': '任务不存在'}), 404

    content_pdf_path = task.get('content_pdf_path')
    output_path = task.get('output_path')
    if not content_pdf_path or not output_path:
        return jsonify({'error': 'PDF文件不存在'}), 404

    cover_count = task.get('cover_page_count', 1)
    toc_count = task.get('toc_page_count', 1)
    content_start = cover_count + toc_count

    from .core.pdf_builder import render_page_thumbnail

    if page < content_start:
        # 封面或目录页 - 从完整PDF渲染
        png_bytes = render_page_thumbnail(output_path, page)
    else:
        # 内容页 - 从内容PDF渲染
        content_page = page - content_start
        png_bytes = render_page_thumbnail(content_pdf_path, content_page)

    if png_bytes is None:
        return jsonify({'error': '无法渲染页面'}), 500

    return Response(png_bytes, mimetype='image/png')


@pdfmerge_bp.route('/api/pages/<task_id>/delete', methods=['POST'])
@login_required
def api_delete_pages(task_id):
    """删除指定页面（page_numbers为0-indexed的PDF页码列表）"""
    with tasks_lock:
        task = tasks.get(task_id)

    if not task or task['status'] != 'done':
        return jsonify({'error': '任务不存在或未完成'}), 404

    data = request.get_json(silent=True) or {}
    page_numbers = data.get('page_numbers', [])

    if not page_numbers:
        return jsonify({'error': '请选择要删除的页面'}), 400

    cover_count = task.get('cover_page_count', 1)
    toc_count = task.get('toc_page_count', 1)
    content_start = cover_count + toc_count

    # 过滤：不允许删除封面和目录页
    content_pages_to_delete = []
    for p in page_numbers:
        if p < content_start:
            return jsonify({'error': f'不能删除封面或目录页（第{p+1}页）'}), 400
        content_pages_to_delete.append(p - content_start)

    content_pdf_path = task.get('content_pdf_path')
    if not content_pdf_path or not os.path.isfile(content_pdf_path):
        return jsonify({'error': '内容PDF文件不存在'}), 404

    from .core.pdf_builder import delete_pages, get_pdf_page_count

    # 删除页面
    delete_pages(content_pdf_path, content_pages_to_delete)

    # 更新章节信息
    content_sections = task.get('content_sections', [])
    new_sections = _recalculate_sections(content_sections, content_pages_to_delete, [])

    # 重新生成目录和合并
    result = _rebuild_pdf(task_id, task, new_sections)
    if 'error' in result:
        return jsonify(result), 500

    return jsonify({
        'ok': True,
        'total_pages': result['total_pages'],
        'content_count': result['content_count'],
        'sections': result['sections'],
        'message': f'已删除 {len(content_pages_to_delete)} 页，目录已更新',
    })


@pdfmerge_bp.route('/api/pages/<task_id>/insert', methods=['POST'])
@login_required
def api_insert_pages(task_id):
    """在指定页面后插入文件（弹出文件选择对话框）"""
    with tasks_lock:
        task = tasks.get(task_id)

    if not task or task['status'] != 'done':
        return jsonify({'error': '任务不存在或未完成'}), 404

    data = request.get_json(silent=True) or {}
    after_page = data.get('after_page', -1)  # 0-indexed PDF page number, -1 = insert at beginning of content

    cover_count = task.get('cover_page_count', 1)
    toc_count = task.get('toc_page_count', 1)
    content_start = cover_count + toc_count

    # 弹出文件选择对话框
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)

        file_path = filedialog.askopenfilename(
            title='选择要插入的文件（PDF/图片/Word/Excel）',
            filetypes=[
                ('所有支持的文件', '*.pdf *.png *.jpg *.jpeg *.bmp *.doc *.docx *.xls *.xlsx'),
                ('PDF文件', '*.pdf'),
                ('图片文件', '*.png *.jpg *.jpeg *.bmp'),
                ('Word文件', '*.doc *.docx'),
                ('Excel文件', '*.xls *.xlsx'),
            ],
            initialdir=os.path.expanduser('~')
        )
        root.destroy()
    except Exception as e:
        logger.error(f'文件选择对话框异常: {e}')
        return jsonify({'error': f'无法打开文件选择对话框: {e}'}), 500

    if not file_path:
        return jsonify({'cancelled': True})

    if not os.path.isfile(file_path):
        return jsonify({'error': '文件不存在'}), 400

    # 转换为PDF
    from .core.format_converter import convert_to_pdf
    from .core.pdf_builder import insert_pages, get_pdf_page_count

    task_dir = os.path.dirname(task.get('output_path', ''))
    convert_dir = os.path.join(task_dir, 'insert_converted')
    os.makedirs(convert_dir, exist_ok=True)

    source_pdf = convert_to_pdf(file_path, convert_dir)
    if not source_pdf:
        return jsonify({'error': f'文件转换PDF失败: {os.path.basename(file_path)}'}), 500

    # 计算在内容PDF中的插入位置
    if after_page < 0:
        content_insert_pos = -1  # 在内容最前面插入
    else:
        content_insert_pos = after_page - content_start

    content_pdf_path = task.get('content_pdf_path')
    if not content_pdf_path or not os.path.isfile(content_pdf_path):
        return jsonify({'error': '内容PDF文件不存在'}), 404

    # 插入页面
    inserted_count = insert_pages(content_pdf_path, source_pdf, content_insert_pos)

    # 清理转换的临时PDF
    try:
        os.remove(source_pdf)
        shutil.rmtree(convert_dir)
    except Exception:
        pass

    # 更新章节信息
    content_sections = task.get('content_sections', [])
    new_sections = _recalculate_sections(content_sections, [], 
                                          [(content_insert_pos, inserted_count)])

    # 重新生成目录和合并
    result = _rebuild_pdf(task_id, task, new_sections)
    if 'error' in result:
        return jsonify(result), 500

    return jsonify({
        'ok': True,
        'total_pages': result['total_pages'],
        'content_count': result['content_count'],
        'sections': result['sections'],
        'inserted_count': inserted_count,
        'message': f'已插入 {inserted_count} 页，目录已更新',
    })


def _recalculate_sections(content_sections, deleted_pages, inserted_ranges):
    """
    重新计算章节页码

    Args:
        content_sections: 原章节列表 [{name, start, count}]
        deleted_pages: 已删除的内容页（0-indexed，相对于内容）
        inserted_ranges: 已插入的范围列表 [(after_pos, count)]

    Returns:
        更新后的章节列表
    """
    deleted_set = set(deleted_pages)
    new_sections = []

    for sec in content_sections:
        old_start = sec['start']
        old_end = old_start + sec['count']  # exclusive

        # 计算该章节中未被删除的页面数
        remaining = 0
        for p in range(old_start, old_end):
            if p not in deleted_set:
                remaining += 1

        # 计算新的起始位置（需要考虑删除和插入的影响）
        new_start = old_start
        for dp in deleted_pages:
            if dp < old_start:
                new_start -= 1

        for ins_pos, ins_count in inserted_ranges:
            if ins_pos < old_start:  # 插入点在此章节之前
                new_start += ins_count

        if remaining > 0:
            new_sections.append({
                'name': sec['name'],
                'start': new_start,
                'count': remaining,
            })

    return new_sections


def _rebuild_pdf(task_id, task, new_sections):
    """
    重新生成目录并重建完整PDF

    Args:
        task_id: 任务ID
        task: 任务状态字典
        new_sections: 更新后的章节列表

    Returns:
        dict: 包含更新后的页面信息，或错误信息
    """
    try:
        from .core.pdf_builder import generate_cover, generate_toc, merge_pdfs, get_pdf_page_count

        content_pdf_path = task['content_pdf_path']
        cover_params = task.get('cover_params', {})
        cover_count = task.get('cover_page_count', 1)

        content_count = get_pdf_page_count(content_pdf_path)

        # 重新计算目录页数
        toc_entry_count = len(new_sections)
        toc_count = max(1, (toc_entry_count + 24) // 25)
        page_offset = cover_count + toc_count

        # 计算各章节绝对起始页码
        section_page_info = []
        current_page = page_offset
        for sec in new_sections:
            section_page_info.append((sec['name'], current_page))
            current_page += sec['count']

        # 生成新封面和目录
        cover_pdf = generate_cover(
            cover_params.get('cover_title', ''),
            cover_params.get('mode', 'refund'),
            cover_params.get('company_name', ''),
            cover_params.get('period_text', ''),
        )
        toc_pdf = generate_toc(section_page_info, page_offset)

        # 合并：封面 + 目录 + 编辑后的内容
        output_path = task['output_path']
        final_pdfs = [cover_pdf, toc_pdf, content_pdf_path]
        merge_pdfs(final_pdfs, output_path)

        # 清理临时文件
        for tmp in [cover_pdf, toc_pdf]:
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except Exception:
                pass

        total_pages = get_pdf_page_count(output_path)

        # 更新任务状态
        with tasks_lock:
            tasks[task_id]['content_sections'] = new_sections
            tasks[task_id]['toc_page_count'] = toc_count

        # 构建返回的章节信息
        sections_data = []
        for sec in new_sections:
            sections_data.append({
                'name': sec['name'],
                'start_page': sec['start'] + cover_count + toc_count + 1,
                'content_start': sec['start'],
                'count': sec['count'],
            })

        return {
            'total_pages': total_pages,
            'content_count': content_count,
            'sections': sections_data,
        }
    except Exception as e:
        logger.error(f'重建PDF失败: {e}\n{traceback.format_exc()}')
        return {'error': f'重建PDF失败: {e}'}
