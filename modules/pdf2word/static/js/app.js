/**
 * 批量PDF转WORD系统 — 前端交互逻辑（v2.0.0 可逆转换）
 *
 * 新增：
 * - 转换方向：pdf2word（原有）/ topdf（其他格式转PDF）
 * - 输出模式（仅 topdf）：merge（合并单PDF）/ individual（独立PDF）
 * - 文件夹递归选择（topdf 方向，复用 pick_id 模式）
 */

// ===== 全局状态 =====
// v2.3.8：统一上传队列（文件与文件夹可交错排列，支持手动调整顺序）。
// 每项为 {kind:'file', file:File} 或 {kind:'pick', pick_id, folder, file_count, files}。
// 队列顺序即转换/合并顺序，尤其决定 merge 模式下合并 PDF 的页面排序。
var queueItems = [];
var currentTaskId = null;
var pollTimer = null;
var currentDirection = 'pdf2word';  // 'pdf2word' | 'topdf'
var lastResultData = null;

// topdf 方向支持的扩展名（须与后端 to_pdf.SUPPORTED_EXTS 一致）
var TOPDF_EXTS = ['png', 'jpg', 'jpeg', 'bmp', 'gif', 'tif', 'tiff', 'webp',
                  'doc', 'docx', 'xls', 'xlsx', 'txt', 'md', 'pdf'];

// ===== 页面元素引用 =====
var fileInput = document.getElementById('fileInput');
var dropzone = document.getElementById('dropzone');
var toast = document.getElementById('toast');

// ===== Toast 提示 =====
function showToast(msg, type) {
    type = type || 'info';
    toast.textContent = msg;
    toast.className = 'toast ' + type + ' show';
    clearTimeout(toast._timeout);
    toast._timeout = setTimeout(function () {
        toast.classList.remove('show');
    }, 3000);
}

// ===== HTML 转义 =====
function esc(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// ===== 转换方向切换 =====
function onDirectionChange(dir) {
    currentDirection = dir;
    var isTopdf = (dir === 'topdf');

    // 输出模式选项：仅转PDF方向显示（PDF转Word时隐藏，避免歧义）
    document.getElementById('modeGroup').style.display = isTopdf ? 'block' : 'none';
    // 文件夹选择入口：仅转PDF方向显示
    document.getElementById('pickFolderEntry').style.display = isTopdf ? 'block' : 'none';

    // 上传区文案与过滤
    if (isTopdf) {
        fileInput.accept = TOPDF_EXTS.map(function (e) { return '.' + e; }).join(',');
        document.getElementById('uploadTitle').textContent = '上传待转换文件';
        document.getElementById('uploadTip').textContent = '支持图片/Word/Excel/TXT/PDF，可批量上传或选择文件夹';
        document.getElementById('dropzoneMainText').textContent = '拖拽文件到此处';
        document.getElementById('dropzoneSubText').textContent = '支持图片 / Word / Excel / TXT / PDF，可批量上传';
        document.getElementById('actionHint').textContent = '将选中的文件批量转换为 PDF';
    } else {
        fileInput.accept = '.pdf';
        document.getElementById('uploadTitle').textContent = '上传 PDF 文件';
        document.getElementById('uploadTip').textContent = '仅支持 .pdf 格式，可批量上传';
        document.getElementById('dropzoneMainText').textContent = '拖拽 PDF 文件到此处';
        document.getElementById('dropzoneSubText').textContent = '支持批量上传多个 PDF 文件';
        document.getElementById('actionHint').textContent = '将选中的 PDF 文件批量转换为 Word (.docx) 格式';
    }

    // 切换方向后清掉不符合当前方向的已选文件，避免误传
    if (queueItems.length > 0) {
        clearFiles();
        showToast('已切换转换方向，请重新选择文件', 'info');
    }
}

// ===== 文件上传 =====
dropzone.addEventListener('dragover', function (e) {
    e.preventDefault();
    dropzone.classList.add('dragover');
});

dropzone.addEventListener('dragleave', function (e) {
    e.preventDefault();
    dropzone.classList.remove('dragover');
});

dropzone.addEventListener('drop', function (e) {
    e.preventDefault();
    dropzone.classList.remove('dragover');
    addFiles(e.dataTransfer.files);
});

fileInput.addEventListener('change', function () {
    addFiles(this.files);
    this.value = '';
});

function extOk(name) {
    var ext = name.split('.').pop().toLowerCase();
    if (currentDirection === 'pdf2word') return ext === 'pdf';
    return TOPDF_EXTS.indexOf(ext) >= 0;
}

function addFiles(fileList) {
    var added = 0;
    var skipped = [];
    for (var i = 0; i < fileList.length; i++) {
        var f = fileList[i];
        if (!extOk(f.name)) {
            skipped.push(f.name);
            continue;
        }
        // 避免重复
        var dup = queueItems.some(function (it) {
            return it.kind === 'file' && it.file.name === f.name && it.file.size === f.size;
        });
        if (dup) continue;

        queueItems.push({ kind: 'file', file: f });
        added++;
    }
    if (skipped.length > 0) {
        showToast('跳过 ' + skipped.length + ' 个不支持格式的文件：' + esc(skipped.slice(0, 3).join('、')) + (skipped.length > 3 ? ' 等' : ''), 'info');
    }
    if (added > 0) {
        renderFileList();
        checkReady();
    }
}

// ===== 选择文件夹（转PDF方向，递归解析） =====
function pickFolder() {
    showToast('正在打开文件夹选择对话框...', 'info');
    fetch('/pdf2word/api/pick_folder', { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.cancelled) return;
            if (data.error) {
                showToast(data.error, 'error');
                return;
            }
            queueItems.push({
                kind: 'pick',
                pick_id: data.pick_id,
                folder: data.folder,
                file_count: data.file_count,
                files: data.files || []
            });
            if (data.unsupported_count > 0) {
                showToast('已添加文件夹（' + data.file_count + ' 个文件），另有 ' + data.unsupported_count + ' 个不支持格式的文件将被忽略', 'info');
            } else {
                showToast('已添加文件夹（' + data.file_count + ' 个文件）', 'success');
            }
            renderFileList();
            checkReady();
        })
        .catch(function (err) {
            showToast('选择文件夹失败: ' + err.message, 'error');
        });
}

function removeItem(idx) {
    queueItems.splice(idx, 1);
    renderFileList();
    checkReady();
}

// ===== 手动调整顺序（v2.3.8）：上移/下移，队列顺序即转换与合并顺序 =====
function moveItem(idx, delta) {
    var to = idx + delta;
    if (to < 0 || to >= queueItems.length) return;
    var item = queueItems.splice(idx, 1)[0];
    queueItems.splice(to, 0, item);
    renderFileList();
}

// ===== 文件列表渲染（v2.3.8：统一队列，带序号与 ↑↓ 调序按钮） =====
function renderFileList() {
    var list = document.getElementById('fileList');
    var total = 0;
    queueItems.forEach(function (it) {
        total += (it.kind === 'pick') ? it.file_count : 1;
    });

    if (total === 0) {
        list.style.display = 'none';
        return;
    }
    list.style.display = 'block';

    var label = currentDirection === 'pdf2word' ? 'PDF 文件' : '待转换文件';
    var orderHint = queueItems.length > 1 ? '（顺序即转换/合并顺序，可 ↑↓ 调整）' : '';
    document.getElementById('fileCount').textContent = '共 ' + total + ' 个' + label + orderHint;

    var html = queueItems.map(function (it, idx) {
        var upHide = idx === 0 ? ' style="visibility:hidden;"' : '';
        var downHide = idx === queueItems.length - 1 ? ' style="visibility:hidden;"' : '';
        var btns = '<span class="file-order-btns">' +
            '<button type="button" class="order-btn" title="上移"' + upHide +
            ' onclick="moveItem(' + idx + ',-1)">↑</button>' +
            '<button type="button" class="order-btn" title="下移"' + downHide +
            ' onclick="moveItem(' + idx + ',1)">↓</button>' +
            '</span>';
        var remove = '<span class="file-remove" onclick="removeItem(' + idx + ')">✕</span>';
        if (it.kind === 'file') {
            return '<div class="file-row">' +
                '<span class="file-row-seq">' + (idx + 1) + '</span>' +
                '<span>📄</span>' +
                '<span class="file-row-name" title="' + esc(it.file.name) + '">' + esc(it.file.name) + '</span>' +
                '<span class="file-row-meta">(' + formatSize(it.file.size) + ')</span>' +
                btns + remove +
                '</div>';
        }
        var filesPreview = (it.files || []).slice(0, 5).join('、');
        if (it.file_count > 5) filesPreview += ' 等共 ' + it.file_count + ' 个文件';
        return '<div class="file-row folder-row" title="' + esc(filesPreview) + '">' +
            '<span class="file-row-seq">' + (idx + 1) + '</span>' +
            '<span>📂</span>' +
            '<span class="file-row-name">' + esc(it.folder) + '</span>' +
            '<span class="file-row-meta">(' + it.file_count + ' 个文件，递归)</span>' +
            btns + remove +
            '</div>';
    }).join('');

    document.getElementById('fileItems').innerHTML = html;
}

function formatSize(bytes) {
    if (bytes < 1024) return bytes + 'B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + 'KB';
    return (bytes / 1024 / 1024).toFixed(1) + 'MB';
}

function clearFiles() {
    queueItems = [];
    document.getElementById('fileList').style.display = 'none';
    hideActionAndResult();
}

// ===== 检查是否可开始 =====
function checkReady() {
    var actionSection = document.getElementById('actionSection');
    actionSection.style.display = queueItems.length > 0 ? 'block' : 'none';
}

// ===== 开始转换 =====
function startConvert() {
    if (queueItems.length === 0) {
        showToast('请先上传文件', 'error');
        return;
    }

    var startBtn = document.getElementById('startBtn');
    startBtn.disabled = true;
    startBtn.textContent = '转换中...';

    var formData = new FormData();
    formData.append('direction', currentDirection);
    var outputMode = 'merge';
    if (currentDirection === 'topdf') {
        var modeRadios = document.getElementsByName('output_mode');
        for (var i = 0; i < modeRadios.length; i++) {
            if (modeRadios[i].checked) outputMode = modeRadios[i].value;
        }
        formData.append('output_mode', outputMode);
    }
    // v2.3.8：按队列顺序提交（文件与文件夹可交错），并附 order 序列化顺序，
    // 后端按此顺序处理——merge 模式下合并 PDF 的页面排序即此顺序。
    var fIdx = 0, pIdx = 0;
    var order = [];
    queueItems.forEach(function (it) {
        if (it.kind === 'file') {
            formData.append('files', it.file);
            order.push('f' + fIdx);
            fIdx++;
        } else {
            formData.append('pick_ids', it.pick_id);
            order.push('p' + pIdx);
            pIdx++;
        }
    });
    formData.append('order', JSON.stringify(order));

    // 显示进度区
    document.getElementById('progressSection').style.display = 'block';
    document.getElementById('progressBar').style.width = '5%';
    document.getElementById('progressText').textContent = '正在上传文件...';
    document.getElementById('resultSection').style.display = 'none';

    fetch('/pdf2word/api/upload', { method: 'POST', body: formData })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.error) {
                showToast(data.error, 'error');
                startBtn.disabled = false;
                startBtn.textContent = '🚀 开始转换';
                document.getElementById('progressSection').style.display = 'none';
                return;
            }
            currentTaskId = data.task_id;
            document.getElementById('progressText').textContent = '正在转换，共 ' + data.total_files + ' 个文件...';
            document.getElementById('progressBar').style.width = '15%';
            startPolling();
        })
        .catch(function (err) {
            showToast('上传失败: ' + err.message, 'error');
            startBtn.disabled = false;
            startBtn.textContent = '🚀 开始转换';
            document.getElementById('progressSection').style.display = 'none';
        });
}

// ===== 进度轮询 =====
function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(pollProgress, 800);
}

function pollProgress() {
    if (!currentTaskId) return;

    fetch('/pdf2word/api/progress/' + currentTaskId)
        .then(function (r) { return r.json(); })
        .then(function (data) {
            document.getElementById('progressText').textContent = data.message || '转换中...';

            if (data.status === 'done') {
                clearInterval(pollTimer);
                pollTimer = null;
                document.getElementById('progressBar').style.width = '100%';
                document.getElementById('startBtn').disabled = false;
                document.getElementById('startBtn').textContent = '🚀 开始转换';
                fetchResult();
            } else if (data.status === 'error') {
                clearInterval(pollTimer);
                pollTimer = null;
                document.getElementById('startBtn').disabled = false;
                document.getElementById('startBtn').textContent = '🚀 开始转换';
                showToast(data.message || '转换失败', 'error');
            } else {
                // 根据进度更新进度条
                var pct = data.total > 0 ? 15 + Math.round((data.current / data.total) * 80) : 30;
                document.getElementById('progressBar').style.width = Math.min(95, pct) + '%';
            }
        })
        .catch(function () {
            // 忽略网络错误，继续轮询
        });
}

// ===== 获取结果 =====
function fetchResult() {
    fetch('/pdf2word/api/result/' + currentTaskId)
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.error) {
                showToast(data.error, 'error');
                return;
            }
            lastResultData = data;
            renderResult(data);
        })
        .catch(function (err) {
            showToast('获取结果失败: ' + err.message, 'error');
        });
}

// ===== 渲染结果（方向自适应） =====
function renderResult(data) {
    document.getElementById('resultSection').style.display = 'block';

    var direction = data.direction || 'pdf2word';
    var isTopdf = (direction === 'topdf');
    var rows = [];   // 统一为 {src, dst, pages, ok, err}
    var successCount = 0, failCount = 0, totalPages = 0;

    if (isTopdf) {
        (data.results || []).forEach(function (item) {
            rows.push({
                src: item.name,
                dst: item.out_name,
                pages: item.pages,
                ok: true,
                err: '',
                action: item.action,
                validation: item.validation || null
            });
            successCount++;
            totalPages += item.pages || 0;
        });
        (data.skipped || []).forEach(function (item) {
            rows.push({ src: item.name, dst: '-', pages: '-', ok: false,
                        err: item.reason || '转换失败', action: null, validation: null });
            failCount++;
        });
    } else {
        (data.results || []).forEach(function (item) {
            var ok = !!item.ok;
            rows.push({
                src: item.pdf_name,
                dst: ok ? item.docx_name : '-',
                pages: ok ? item.pages : '-',
                ok: ok,
                err: item.error || '',
                action: null,
                validation: item.validation || null
            });
            if (ok) {
                successCount++;
                totalPages += item.pages || 0;
            } else {
                failCount++;
            }
        });
    }

    // 表头按方向自适应
    document.getElementById('thSrc').textContent = isTopdf ? '原文件名' : '原PDF文件名';
    document.getElementById('thDst').textContent = isTopdf ? 'PDF文件名' : 'Word文件名';

    // 校验统计（v2.1.0：转换报告汇总）
    var valPass = 0, valFixed = 0, valFail = 0;
    rows.forEach(function (r) {
        if (!r.validation) return;
        if (r.validation.overall === 'pass') valPass++;
        else if (r.validation.overall === 'fixed') valFixed++;
        else if (r.validation.overall === 'fail') valFail++;
    });

    // 统计卡片（成功/失败清单及数量统计）
    var modeText = '';
    if (isTopdf) {
        modeText = '<div class="stat-card"><div class="stat-value" style="font-size:16px;line-height:44px;">' +
            (data.output_mode === 'merge' ? '合并模式' : '独立模式') + '</div>' +
            '<div class="stat-label">输出模式</div></div>';
    }
    var valCard = '';
    if (valPass + valFixed + valFail > 0) {
        var valColor = valFail > 0 ? 'danger' : (valFixed > 0 ? 'warn' : 'success');
        var valText = valFail > 0
            ? (valPass + valFixed) + '/' + (valPass + valFixed + valFail)
            : (valPass + valFixed);
        valCard = '<div class="stat-card ' + valColor + '">' +
            '<div class="stat-value">' + valText + '</div>' +
            '<div class="stat-label">校验通过' + (valFixed ? '（含修正' + valFixed + '）' : '') + '</div></div>';
    }
    document.getElementById('statsGrid').innerHTML =
        '<div class="stat-card success">' +
            '<div class="stat-value">' + successCount + '</div>' +
            '<div class="stat-label">转换成功</div>' +
        '</div>' +
        '<div class="stat-card danger">' +
            '<div class="stat-value">' + failCount + '</div>' +
            '<div class="stat-label">转换失败</div>' +
        '</div>' +
        '<div class="stat-card">' +
            '<div class="stat-value">' + totalPages + '</div>' +
            '<div class="stat-label">总页数</div>' +
        '</div>' +
        '<div class="stat-card">' +
            '<div class="stat-value">' + rows.length + '</div>' +
            '<div class="stat-label">文件总数</div>' +
        '</div>' +
        modeText + valCard;

    // 转换详情表格（含校验列 + 可展开逐项明细）
    var tbody = document.getElementById('resultTableBody');
    if (rows.length > 0) {
        var htmlParts = [];
        rows.forEach(function (row, idx) {
            var statusHtml = row.ok
                ? '<span class="col-status-ok">✓ 成功</span>'
                : '<span class="col-status-fail">✗ 失败</span>';
            // 校验徽标
            var valHtml = '<span style="color:#bbb;">—</span>';
            if (row.validation) {
                var ov = row.validation.overall;
                var badge = ov === 'pass' ? '<span class="val-badge val-pass">✓ 通过</span>'
                    : ov === 'fixed' ? '<span class="val-badge val-fixed">⚠ 已修正</span>'
                    : '<span class="val-badge val-fail">✗ 未通过</span>';
                valHtml = '<a href="javascript:void(0)" onclick="toggleValDetail(' + idx + ')">' + badge + '</a>';
            }
            var lastHtml = '';
            if (row.ok) {
                lastHtml = '<button class="btn-download" onclick="downloadSingle(this,\'' + esc(row.dst) + '\')">保存</button>';
                if (isTopdf && row.action === 'merged') {
                    lastHtml += ' <span style="color:#999;font-size:11px;">已并入合并PDF</span>';
                }
            } else if (row.err) {
                lastHtml = '<span style="color:#E74C3C;font-size:12px;" title="' + esc(row.err) + '">' + esc(row.err.substring(0, 30)) + (row.err.length > 30 ? '...' : '') + '</span>';
            }
            htmlParts.push('<tr>' +
                '<td class="col-seq">' + (idx + 1) + '</td>' +
                '<td class="col-name" title="' + esc(row.src) + '">' + esc(row.src) + '</td>' +
                '<td class="col-name">' + esc(row.dst) + '</td>' +
                '<td>' + row.pages + '</td>' +
                '<td>' + statusHtml + '</td>' +
                '<td>' + valHtml + '</td>' +
                '<td>' + lastHtml + '</td>' +
                '</tr>');
            // 校验明细子行（默认隐藏）
            if (row.validation) {
                var items = (row.validation.items || []).map(function (it) {
                    var icon = it.status === 'pass' ? '<span class="val-badge val-pass">✓</span>'
                        : it.status === 'fixed' ? '<span class="val-badge val-fixed">⚠</span>'
                        : '<span class="val-badge val-fail">✗</span>';
                    return '<div class="val-item">' + icon +
                        '<span class="val-item-label">' + esc(it.label) + '</span>' +
                        '<span class="val-item-detail">' + esc(it.detail) + '</span></div>';
                }).join('');
                htmlParts.push('<tr class="val-detail-row" id="valDetail' + idx + '" style="display:none;">' +
                    '<td colspan="7"><div class="val-detail-box">' +
                    '<div class="val-detail-title">校验明细：' + esc(row.src) + ' → ' + esc(row.dst) + '</div>' +
                    items + '</div></td></tr>');
            }
        });
        tbody.innerHTML = htmlParts.join('');
    } else {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#999;">无转换结果</td></tr>';
    }

    document.getElementById('progressSection').style.display = 'none';
}

// ===== 校验明细展开/收起（v2.1.0） =====
function toggleValDetail(idx) {
    var el = document.getElementById('valDetail' + idx);
    if (el) {
        el.style.display = (el.style.display === 'none') ? 'table-row' : 'none';
    }
}

// ===== 导出校验报告（v2.1.0：TXT 逐文件逐项汇总） =====
function exportValidationReport(btn) {
    if (!lastResultData) {
        showToast('暂无校验结果', 'error');
        return;
    }
    var data = lastResultData;
    var direction = data.direction || 'pdf2word';
    var lines = [];
    lines.push('批量' + (direction === 'topdf' ? '转PDF' : 'PDF转Word') + ' 转换校验报告');
    lines.push('生成时间：' + new Date().toLocaleString());
    lines.push('校验项：页面方向 / 页面尺寸(A4) / 布局结构(文本·图片·表格) / 字体与格式');
    lines.push(''.padEnd(60, '='));
    var results = data.results || [];
    var passN = 0, fixedN = 0, failN = 0;
    results.forEach(function (item) {
        var srcName = direction === 'topdf' ? item.name : item.pdf_name;
        var dstName = direction === 'topdf' ? item.out_name : item.docx_name;
        var v = item.validation;
        if (!v) {
            lines.push('【未校验】' + srcName + '（' + (item.error || '转换失败') + '）');
            return;
        }
        var mark = v.overall === 'pass' ? '通过' : v.overall === 'fixed' ? '已修正' : '未通过';
        if (v.overall === 'pass') passN++;
        else if (v.overall === 'fixed') fixedN++;
        else failN++;
        lines.push('【' + mark + '】' + srcName + ' → ' + dstName);
        (v.items || []).forEach(function (it) {
            var icon = it.status === 'pass' ? '✓' : it.status === 'fixed' ? '⚠' : '✗';
            lines.push('    ' + icon + ' ' + it.label + '：' + it.detail);
        });
    });
    lines.push(''.padEnd(60, '='));
    lines.push('汇总：通过 ' + passN + ' 个，已修正 ' + fixedN + ' 个，未通过 ' + failN + ' 个');
    (data.skipped || []).forEach(function (s) {
        lines.push('【失败】' + s.name + '：' + (s.reason || ''));
    });
    var blob = new Blob(['﻿' + lines.join('\r\n')], { type: 'text/plain;charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = '转换校验报告_' + new Date().toISOString().slice(0, 10) + '.txt';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    showToast('校验报告已导出', 'success');
}

// ===== 选择保存位置（弹出系统原生文件夹选择对话框） =====
function saveToLocation(triggerBtn) {
    if (!currentTaskId) return;
    var btn = triggerBtn || document.getElementById('btnSaveToLocation');
    var originalText = btn ? btn.textContent : '';
    if (btn) {
        btn.disabled = true;
        btn.textContent = '正在打开保存对话框...';
    }

    fetch('/pdf2word/api/save_to/' + currentTaskId, { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (btn) {
                btn.disabled = false;
                btn.textContent = originalText;
            }
            if (data.cancelled) {
                return;
            }
            if (data.error) {
                showToast(data.error, 'error');
                return;
            }
            if (data.ok) {
                showToast('保存成功', 'success');
            }
        })
        .catch(function (err) {
            if (btn) {
                btn.disabled = false;
                btn.textContent = originalText;
            }
            showToast('保存失败: ' + err.message, 'error');
        });
}

// ===== 下载全部 =====
function downloadAll(btn) {
    saveToLocation(btn);
}

// ===== 下载单个文件（弹出保存位置选择窗口） =====
function downloadSingle(btn, fileName) {
    if (!currentTaskId) return;
    var originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '正在打开保存对话框...';

    var formData = new FormData();
    formData.append('file_name', fileName);

    fetch('/pdf2word/api/save_to/' + currentTaskId, { method: 'POST', body: formData })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            btn.disabled = false;
            btn.textContent = originalText;
            if (data.cancelled) {
                return;
            }
            if (data.error) {
                showToast(data.error, 'error');
                return;
            }
            if (data.ok) {
                showToast('保存成功', 'success');
            }
        })
        .catch(function (err) {
            btn.disabled = false;
            btn.textContent = originalText;
            showToast('保存失败: ' + err.message, 'error');
        });
}

// ===== 隐藏操作区和结果 =====
function hideActionAndResult() {
    document.getElementById('actionSection').style.display = 'none';
    document.getElementById('resultSection').style.display = 'none';
    document.getElementById('progressSection').style.display = 'none';
    currentTaskId = null;
    lastResultData = null;
}

// ===== 全部重置 =====
function resetAll() {
    clearFiles();
    hideActionAndResult();
    document.getElementById('startBtn').disabled = false;
    document.getElementById('startBtn').textContent = '🚀 开始转换';
}

// ===== 支持文件夹拖拽 =====
dropzone.addEventListener('drop', function (e) {
    e.preventDefault();
    dropzone.classList.remove('dragover');

    var items = e.dataTransfer.items;
    if (!items) {
        addFiles(e.dataTransfer.files);
        return;
    }

    var pending = [];
    for (var i = 0; i < items.length; i++) {
        var entry = items[i].webkitGetAsEntry ? items[i].webkitGetAsEntry() : null;
        if (entry) {
            pending.push(entry);
        }
    }

    if (pending.length === 0) {
        addFiles(e.dataTransfer.files);
        return;
    }

    var allFiles = [];
    function processEntry(entry) {
        if (entry.isFile) {
            return new Promise(function (resolve) {
                entry.file(function (file) {
                    allFiles.push(file);
                    resolve();
                });
            });
        } else if (entry.isDirectory) {
            return new Promise(function (resolve) {
                var dirReader = entry.createReader();
                dirReader.readEntries(function (entries) {
                    var promises = entries.map(function (e) { return processEntry(e); });
                    Promise.all(promises).then(resolve);
                });
            });
        }
        return Promise.resolve();
    }

    Promise.all(pending.map(function (entry) { return processEntry(entry); }))
        .then(function () {
            if (allFiles.length > 0) {
                addFiles(allFiles);
            }
        });
});
