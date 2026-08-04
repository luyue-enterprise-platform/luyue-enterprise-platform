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
    send_file
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

    # 扫描文件夹内的所有支持文件
    from .core.sections import SUPPORTED_EXTENSIONS
    file_list = []
    for root_dir, dirs, files in os.walk(folder_path):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in SUPPORTED_EXTENSIONS:
                rel_path = os.path.relpath(os.path.join(root_dir, f), folder_path)
                file_size = os.path.getsize(os.path.join(root_dir, f))
                file_list.append({
                    'name': f,
                    'path': rel_path,
                    'ext': ext,
                    'size': file_size,
                })

    return jsonify({
        'ok': True,
        'folder_path': folder_path,
        'file_count': len(file_list),
        'files': file_list,
    })


@pdfmerge_bp.route('/api/scan_match', methods=['POST'])
@login_required
def api_scan_match():
    """扫描文件夹并匹配文件到各章节（不生成PDF）"""
    data = request.get_json(silent=True) or {}
    folder_path = data.get('folder_path', '')
    mode = data.get('mode', 'refund')  # refund / deduction

    if not folder_path or not os.path.isdir(folder_path):
        return jsonify({'error': '文件夹路径无效'}), 400

    if mode not in ('refund', 'deduction'):
        return jsonify({'error': '模式必须为 refund 或 deduction'}), 400

    from .core.sections import match_files

    try:
        result = match_files(folder_path, mode)

        # 转换为前端友好的格式
        sections_data = []
        for sec in result['sections']:
            sections_data.append({
                'id': sec['id'],
                'name': sec['name'],
                'file_count': len(sec['files']),
                'files': [os.path.basename(f) for f in sec['files']],
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
    folder_path = data.get('folder_path', '')
    mode = data.get('mode', 'refund')
    cover_title = data.get('cover_title', '').strip()
    company_name = data.get('company_name', '').strip()
    period_start = data.get('period_start', '')
    period_end = data.get('period_end', '')
    deduction_period = data.get('deduction_period', '')  # 抵税模式的所属期
    proof_period = data.get('proof_period', '')  # 抵税模式的证明时间段

    if not folder_path or not os.path.isdir(folder_path):
        return jsonify({'error': '文件夹路径无效'}), 400

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
        args=(task_id, folder_path, mode, cover_title, company_name,
              period_text, output_filename, task_dir),
        daemon=True
    )
    thread.start()

    return jsonify({
        'ok': True,
        'task_id': task_id,
        'output_filename': output_filename,
    })


def _process_generate_task(task_id, folder_path, mode, cover_title, company_name,
                            period_text, output_filename, task_dir):
    """后台PDF生成任务"""
    try:
        from .core.sections import match_files
        from .core.format_converter import convert_to_pdf
        from .core.pdf_builder import generate_cover, generate_toc, merge_pdfs, get_pdf_page_count

        def update_progress(current, total, message):
            with tasks_lock:
                tasks[task_id]['current'] = current
                tasks[task_id]['total'] = total
                tasks[task_id]['message'] = message

        # Step 1: 匹配文件
        update_progress(0, 100, '正在扫描文件夹并匹配文件...')
        match_result = match_files(folder_path, mode)

        matched_sections = [s for s in match_result['sections'] if s['matched']]
        total_files = sum(len(s['files']) for s in matched_sections)

        if total_files == 0:
            with tasks_lock:
                tasks[task_id]['status'] = 'error'
                tasks[task_id]['error'] = '未匹配到任何文件，请检查文件夹内容'
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
                update_progress(converted_count, total_files,
                                f'正在转换 ({converted_count}/{total_files}): {os.path.basename(file_path)}')

                pdf_path = convert_to_pdf(file_path, pdf_convert_dir)
                if pdf_path:
                    section_pdf_paths.append(pdf_path)
                else:
                    logger.warning(f'转换失败，跳过: {file_path}')

            if section_pdf_paths:
                section_pdfs.append((section['name'], section_pdf_paths))

        # Step 3: 生成封面
        update_progress(converted_count, total_files, '正在生成封面...')
        cover_pdf = generate_cover(cover_title, mode, company_name, period_text)

        # Step 4: 计算各章节页码偏移（封面占1页，目录占1-2页）
        # 先合并所有内容PDF，统计页数
        update_progress(converted_count, total_files, '正在合并PDF...')

        # 合并所有章节内容
        content_pdfs = []
        for section_name, pdf_list in section_pdfs:
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

        # 计算各章节起始页码
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

        # 清理临时文件
        for tmp in [cover_pdf, toc_pdf, content_merged_path]:
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
