/* ===== 汇总智能系统 - 前端逻辑 ===== */

// ===== 状态管理 =====
let state = {
    mode: 'refund',
    selectedFiles: [],      // [{name, abs_path, ext, size}]
    processTaskId: null,
    matchResult: null,      // {sections: [...], unmatched: [...]}
    generateTaskId: null,
    pdfResult: null,
    capabilities: null,
};

// ===== API 调用 =====
async function api(url, options = {}) {
    const resp = await fetch(url, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
    });
    return resp.json();
}

// ===== 模式选择 =====
function selectMode(mode) {
    state.mode = mode;
    document.getElementById('modeRefund').classList.toggle('active', mode === 'refund');
    document.getElementById('modeDeduction').classList.toggle('active', mode === 'deduction');

    // 如果已有匹配结果，提示重新匹配
    if (state.matchResult) {
        if (confirm('切换模式后需要重新匹配，是否立即重新匹配？')) {
            reMatch();
        }
    }
}

// ===== 文件选择 =====
async function selectFolder() {
    try {
        const data = await api('/pdfmerge/api/select_folder', { method: 'POST' });
        if (data.cancelled) return;
        if (data.error) { alert(data.error); return; }

        addFiles(data.files);
    } catch (e) {
        alert('选择文件夹失败: ' + e.message);
    }
}

async function selectFiles() {
    try {
        const data = await api('/pdfmerge/api/select_files', { method: 'POST' });
        if (data.cancelled) return;
        if (data.error) { alert(data.error); return; }

        addFiles(data.files);
    } catch (e) {
        alert('选择文件失败: ' + e.message);
    }
}

function addFiles(newFiles) {
    // 去重添加
    const existingPaths = new Set(state.selectedFiles.map(f => f.abs_path));
    for (const f of newFiles) {
        if (!existingPaths.has(f.abs_path)) {
            state.selectedFiles.push(f);
            existingPaths.add(f.abs_path);
        }
    }
    renderFileList();
}

function removeFile(index) {
    state.selectedFiles.splice(index, 1);
    renderFileList();
}

function clearFiles() {
    state.selectedFiles = [];
    renderFileList();
}

// ===== 文件列表渲染 =====
function renderFileList() {
    const container = document.getElementById('fileList');
    const stats = document.getElementById('fileStats');

    if (state.selectedFiles.length === 0) {
        container.innerHTML = '<div style="padding:20px;text-align:center;color:#999;font-size:13px;">尚未选择文件</div>';
        stats.textContent = '';
        return;
    }

    let html = '';
    state.selectedFiles.forEach((f, i) => {
        const icon = getFileIcon(f.ext);
        const sizeStr = formatSize(f.size);
        html += `<div class="file-item">
            <span class="file-icon">${icon}</span>
            <span class="file-name" title="${f.abs_path}">${f.name}</span>
            <span class="file-size">${sizeStr}</span>
            <span class="file-remove" onclick="removeFile(${i})">✕</span>
        </div>`;
    });
    container.innerHTML = html;

    const totalSize = state.selectedFiles.reduce((s, f) => s + f.size, 0);
    stats.textContent = `共 ${state.selectedFiles.length} 个文件，${formatSize(totalSize)}`;
}

function getFileIcon(ext) {
    if (['.pdf'].includes(ext)) return '📕';
    if (['.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff'].includes(ext)) return '🖼';
    if (['.doc', '.docx'].includes(ext)) return '📘';
    if (['.xls', '.xlsx'].includes(ext)) return '📗';
    return '📄';
}

function formatSize(bytes) {
    if (bytes < 1024) return bytes + 'B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + 'KB';
    return (bytes / 1024 / 1024).toFixed(1) + 'MB';
}

// ===== 处理（转换 + OCR + 匹配）=====
async function startProcess() {
    if (state.selectedFiles.length === 0) {
        alert('请先选择文件');
        return;
    }

    const btn = document.getElementById('btnProcess');
    btn.disabled = true;
    document.getElementById('processProgress').style.display = 'block';
    updateProgress('process', 0, '正在提交...');

    try {
        const filePaths = state.selectedFiles.map(f => f.abs_path);
        const data = await api('/pdfmerge/api/process', {
            method: 'POST',
            body: JSON.stringify({ file_paths: filePaths, mode: state.mode }),
        });

        if (data.error) {
            alert(data.error);
            btn.disabled = false;
            document.getElementById('processProgress').style.display = 'none';
            return;
        }

        state.processTaskId = data.task_id;
        pollProgress(data.task_id, 'process', onProcessDone);
    } catch (e) {
        alert('启动处理失败: ' + e.message);
        btn.disabled = false;
        document.getElementById('processProgress').style.display = 'none';
    }
}

async function pollProgress(taskId, type, onDone) {
    const interval = setInterval(async () => {
        try {
            const data = await api(`/pdfmerge/api/progress/${taskId}`);
            updateProgress(type, data.progress, data.message);

            if (data.status === 'done') {
                clearInterval(interval);
                document.getElementById(type === 'process' ? 'btnProcess' : '').disabled = false;
                onDone(data);
            } else if (data.status === 'error') {
                clearInterval(interval);
                alert('处理失败: ' + data.message);
                document.getElementById('btnProcess').disabled = false;
                document.getElementById('processProgress').style.display = 'none';
            }
        } catch (e) {
            console.error('轮询失败:', e);
        }
    }, 1000);
}

function onProcessDone(data) {
    document.getElementById('btnProcess').disabled = false;
    if (data.result) {
        state.matchResult = data.result;
        renderMatchResult(data.result);
        document.getElementById('cardMatch').style.display = 'block';
        document.getElementById('cardGenerate').style.display = 'block';
        // 滚动到匹配结果
        document.getElementById('cardMatch').scrollIntoView({ behavior: 'smooth' });
    }
}

// ===== 重新匹配 =====
async function reMatch() {
    try {
        const data = await api('/pdfmerge/api/rematch', {
            method: 'POST',
            body: JSON.stringify({ mode: state.mode }),
        });

        if (data.error) {
            alert(data.error);
            return;
        }

        state.matchResult = data;
        renderMatchResult(data);
    } catch (e) {
        alert('重新匹配失败: ' + e.message);
    }
}

// ===== 补充材料 =====
async function addMoreFiles() {
    // 让用户选择是添加文件夹还是文件
    const choice = confirm('点击"确定"添加文件夹，点击"取消"添加文件');
    try {
        let data;
        if (choice) {
            data = await api('/pdfmerge/api/select_folder', { method: 'POST' });
        } else {
            data = await api('/pdfmerge/api/select_files', { method: 'POST' });
        }

        if (data.cancelled) return;
        if (data.error) { alert(data.error); return; }

        // 添加新文件
        addFiles(data.files);

        // 自动处理新文件
        const filePaths = data.files.map(f => f.abs_path);
        if (filePaths.length === 0) return;

        document.getElementById('processProgress').style.display = 'block';
        document.getElementById('btnProcess').disabled = true;
        updateProgress('process', 0, '正在处理新文件...');

        const processData = await api('/pdfmerge/api/process', {
            method: 'POST',
            body: JSON.stringify({ file_paths: filePaths, mode: state.mode }),
        });

        if (processData.task_id) {
            state.processTaskId = processData.task_id;
            pollProgress(processData.task_id, 'process', onProcessDone);
        }
    } catch (e) {
        alert('补充材料失败: ' + e.message);
    }
}

// ===== 匹配结果渲染 =====
function renderMatchResult(result) {
    const summary = document.getElementById('matchSummary');
    const sectionsList = document.getElementById('sectionsList');
    const unmatchedSection = document.getElementById('unmatchedSection');

    // 统计
    const totalImages = result.sections.reduce((s, sec) => s + sec.images.length, 0) + result.unmatched.length;
    const matchedImages = result.sections.reduce((s, sec) => s + sec.images.length, 0);
    const matchedSections = result.sections.filter(s => s.matched).length;

    summary.innerHTML = `
        <div class="summary-item"><div class="summary-num">${matchedImages}</div><div class="summary-label">已匹配图片</div></div>
        <div class="summary-item"><div class="summary-num">${result.unmatched.length}</div><div class="summary-label">未匹配图片</div></div>
        <div class="summary-item"><div class="summary-num">${matchedSections}/${result.sections.length}</div><div class="summary-label">已匹配章节</div></div>
    `;

    // 渲染章节
    let html = '';
    result.sections.forEach((section, idx) => {
        const badgeClass = section.matched ? 'badge-ok' : (section.required ? 'badge-missing' : 'badge-optional');
        const badgeText = section.matched ? `${section.images.length}张` : (section.required ? '缺失' : '可选');

        html += `<div class="section-item ${section.matched ? '' : 'missing'}" id="section-${idx}"
                     ondragover="dragOver(event)" ondrop="drop(event, ${idx})">
            <div class="section-header">
                <span class="section-name">${section.name}</span>
                <span class="section-badge ${badgeClass}">${badgeText}</span>
            </div>
            <div class="section-images" id="images-${idx}">`;

        section.images.forEach(img => {
            html += renderImageCard(img, idx);
        });

        html += `</div></div>`;
    });
    sectionsList.innerHTML = html;

    // 未匹配
    if (result.unmatched.length > 0) {
        unmatchedSection.style.display = 'block';
        let unmatchedHtml = '';
        result.unmatched.forEach(img => {
            unmatchedHtml += renderImageCard(img, -1);
        });
        document.getElementById('unmatchedList').innerHTML = unmatchedHtml;
        document.getElementById('unmatchedList').parentElement.ondragover = (e) => e.preventDefault();
        document.getElementById('unmatchedList').parentElement.ondrop = (e) => drop(e, -1);
    } else {
        unmatchedSection.style.display = 'none';
    }
}

function renderImageCard(img, sectionIdx) {
    const name = img.original_filename || '';
    const shortName = name.length > 12 ? name.substring(0, 10) + '..' : name;
    return `<div class="image-card" draggable="true"
                ondragstart="dragStart(event, '${img.id}', ${sectionIdx})"
                ondragend="dragEnd(event)">
        <img src="/pdfmerge/api/thumbnail/${img.id}" alt="${name}" loading="lazy">
        <div class="img-name" title="${name}">${shortName}</div>
        <div class="img-remove" onclick="removeImage('${img.id}', ${sectionIdx})">✕</div>
    </div>`;
}

// ===== 拖拽 =====
function dragStart(event, imageId, fromSection) {
    event.dataTransfer.setData('text/plain', JSON.stringify({ imageId, fromSection }));
    event.target.classList.add('dragging');
}

function dragEnd(event) {
    event.target.classList.remove('dragging');
    // 清除所有 drag-over 样式
    document.querySelectorAll('.section-item.drag-over').forEach(el => el.classList.remove('drag-over'));
}

function dragOver(event) {
    event.preventDefault();
    event.currentTarget.classList.add('drag-over');
}

function drop(event, toSection) {
    event.preventDefault();
    event.currentTarget.classList.remove('drag-over');

    const data = JSON.parse(event.dataTransfer.getData('text/plain'));
    const { imageId, fromSection } = data;

    if (fromSection === toSection) return; // 同一区域，不处理

    moveImage(imageId, fromSection, toSection);
}

function moveImage(imageId, fromSection, toSection) {
    const result = state.matchResult;
    let img = null;

    // 从源位置移除
    if (fromSection >= 0) {
        const section = result.sections[fromSection];
        if (section) {
            const idx = section.images.findIndex(i => i.id === imageId);
            if (idx >= 0) {
                img = section.images.splice(idx, 1)[0];
                section.matched = section.images.length > 0;
            }
        }
    } else {
        // 从未匹配列表移除
        const idx = result.unmatched.findIndex(i => i.id === imageId);
        if (idx >= 0) {
            img = result.unmatched.splice(idx, 1)[0];
        }
    }

    if (!img) return;

    // 添加到目标位置
    if (toSection >= 0) {
        const section = result.sections[toSection];
        if (section) {
            section.images.push(img);
            section.matched = true;
        }
    } else {
        // 添加到未匹配列表
        result.unmatched.push(img);
    }

    // 重新渲染
    renderMatchResult(result);
}

function removeImage(imageId, sectionIdx) {
    if (sectionIdx >= 0) {
        moveImage(imageId, sectionIdx, -1);
    }
}

// ===== PDF 生成 =====
async function startGenerate() {
    if (!state.matchResult) {
        alert('请先进行匹配');
        return;
    }

    // 只包含有图片的章节
    const activeSections = state.matchResult.sections.filter(s => s.images.length > 0);
    if (activeSections.length === 0) {
        alert('没有匹配到任何图片，无法生成PDF');
        return;
    }

    const coverInfo = {
        title: document.getElementById('coverTitle').value || '',
        company_name: document.getElementById('companyName').value || '',
        period_text: document.getElementById('periodText').value || '',
    };

    document.getElementById('generateProgress').style.display = 'block';
    updateProgress('generate', 0, '正在提交...');

    try {
        const data = await api('/pdfmerge/api/generate', {
            method: 'POST',
            body: JSON.stringify({
                sections: activeSections,
                cover_info: coverInfo,
                mode: state.mode,
            }),
        });

        if (data.error) {
            alert(data.error);
            document.getElementById('generateProgress').style.display = 'none';
            return;
        }

        state.generateTaskId = data.task_id;
        pollGenerateProgress(data.task_id);
    } catch (e) {
        alert('启动生成失败: ' + e.message);
        document.getElementById('generateProgress').style.display = 'none';
    }
}

function pollGenerateProgress(taskId) {
    const interval = setInterval(async () => {
        try {
            const data = await api(`/pdfmerge/api/progress/${taskId}`);
            updateProgress('generate', data.progress, data.message);

            if (data.status === 'done') {
                clearInterval(interval);
                state.pdfResult = data.result;
                document.getElementById('resultActions').style.display = 'flex';
                alert(`PDF生成完成！共 ${data.result.page_count} 页`);
            } else if (data.status === 'error') {
                clearInterval(interval);
                alert('生成失败: ' + data.message);
            }
        } catch (e) {
            console.error('轮询失败:', e);
        }
    }, 1000);
}

// ===== PDF 预览/下载/另存为 =====
function previewPDF() {
    if (!state.generateTaskId) return;
    window.open(`/pdfmerge/api/preview/${state.generateTaskId}`, '_blank');
}

function downloadPDF() {
    if (!state.generateTaskId) return;
    window.location.href = `/pdfmerge/api/download/${state.generateTaskId}`;
}

async function saveTo() {
    if (!state.generateTaskId) return;
    try {
        const data = await api(`/pdfmerge/api/save_to/${state.generateTaskId}`, { method: 'POST' });
        if (data.ok) {
            alert('文件已保存到: ' + data.saved_path);
        }
    } catch (e) {
        alert('另存为失败: ' + e.message);
    }
}

// ===== 进度更新 =====
function updateProgress(type, progress, message) {
    const prefix = type === 'process' ? 'process' : 'generate';
    document.getElementById(`${prefix}Bar`).style.width = progress + '%';
    document.getElementById(`${prefix}Text`).textContent = message;
}

// ===== 系统能力检测 =====
async function checkCapabilities() {
    try {
        const data = await api('/pdfmerge/api/capabilities');
        state.capabilities = data;

        const info = document.getElementById('capabilityInfo');
        let html = '支持的文件格式: 图片';
        if (data.pdf) html += ' / PDF';
        if (data.word) html += ' / Word';
        if (data.excel) html += ' / Excel';
        if (data.method) html += ` (转换引擎: ${data.method})`;
        if (!data.word || !data.excel) {
            html += '<br>⚠️ Word/Excel转换不可用，建议安装 Microsoft Office 或 WPS';
        }
        info.innerHTML = html;
    } catch (e) {
        console.error('能力检测失败:', e);
    }
}

// ===== 初始化 =====
async function init() {
    renderFileList();
    await checkCapabilities();
}

init();
