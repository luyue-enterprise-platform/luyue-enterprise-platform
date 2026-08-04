// ===== 全局状态 =====
var currentMode = 'refund';
var selectedFiles = [];  // [{name, absPath, size, fromFolder, folderName}, ...]
var task_id = null;

// ===== 初始化 =====
document.addEventListener('DOMContentLoaded', function() {
    checkCapabilities();
});

// ===== 模式选择 =====
function selectMode(mode) {
    currentMode = mode;
    document.getElementById('modeRefund').classList.toggle('active', mode === 'refund');
    document.getElementById('modeDeduction').classList.toggle('active', mode === 'deduction');
    document.getElementById('refundFields').classList.toggle('hidden', mode !== 'refund');
    document.getElementById('deductionFields').classList.toggle('hidden', mode !== 'deduction');

    // 重新匹配
    if (selectedFiles.length > 0) {
        scanMatch();
    }
}

// ===== 检查转换能力 =====
function checkCapabilities() {
    fetch('/pdfmerge/api/capabilities')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.ok) {
                var caps = data.capabilities;
                var html = '系统转换能力：';
                html += '<span class="capability-tag ok">✅ 图片→PDF</span>';
                html += '<span class="capability-tag ok">✅ PDF→PDF</span>';
                if (caps.word) {
                    html += '<span class="capability-tag ok">✅ Word→PDF</span>';
                } else {
                    html += '<span class="capability-tag no">❌ Word→PDF (需安装MS Office或LibreOffice)</span>';
                }
                if (caps.excel) {
                    html += '<span class="capability-tag ok">✅ Excel→PDF</span>';
                } else {
                    html += '<span class="capability-tag no">❌ Excel→PDF (需安装MS Office或LibreOffice)</span>';
                }
                if (caps.method) {
                    html += '<br><small>转换引擎：' + caps.method + '</small>';
                }
                var el = document.getElementById('capabilityInfo');
                el.innerHTML = html;
                el.classList.remove('hidden');
            }
        })
        .catch(function() {});
}

// ===== 选择文件夹（累加模式） =====
function selectFolder() {
    fetch('/pdfmerge/api/select_folder', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({})
    })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.cancelled) return;
            if (data.error) {
                alert(data.error);
                return;
            }
            // 累加模式：添加文件夹中的文件到已选列表
            var folderName = data.folder_path.split(/[\\\/]/).pop();
            for (var i = 0; i < data.files.length; i++) {
                var f = data.files[i];
                // 去重：同名+同大小
                var exists = selectedFiles.some(function(s) {
                    return s.name === f.name && s.size === f.size;
                });
                if (!exists) {
                    selectedFiles.push({
                        name: f.name,
                        absPath: f.abs_path,
                        size: f.size,
                        fromFolder: true,
                        folderName: folderName,
                    });
                }
            }
            renderFileList();
            // 自动扫描匹配
            scanMatch();
        })
        .catch(function(err) {
            alert('选择文件夹失败: ' + err.message);
        });
}

// ===== 选择文件（累加模式） =====
function selectFiles() {
    fetch('/pdfmerge/api/select_files', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({})
    })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.cancelled) return;
            if (data.error) {
                alert(data.error);
                return;
            }
            // 累加模式
            for (var i = 0; i < data.files.length; i++) {
                var f = data.files[i];
                var exists = selectedFiles.some(function(s) {
                    return s.name === f.name && s.size === f.size;
                });
                if (!exists) {
                    selectedFiles.push({
                        name: f.name,
                        absPath: f.abs_path,
                        size: f.size,
                        fromFolder: false,
                        folderName: '',
                    });
                }
            }
            renderFileList();
            scanMatch();
        })
        .catch(function(err) {
            alert('选择文件失败: ' + err.message);
        });
}

// ===== 移除单个文件 =====
function removeFile(index) {
    selectedFiles.splice(index, 1);
    renderFileList();
    if (selectedFiles.length > 0) {
        scanMatch();
    } else {
        document.getElementById('matchCard').classList.add('hidden');
        document.getElementById('btnGenerate').disabled = true;
        document.getElementById('btnGenerate').textContent = '📑 开始生成PDF';
    }
}

// ===== 清除所有文件 =====
function clearAllFiles() {
    if (selectedFiles.length === 0) return;
    if (!confirm('确认清除所有已添加的文件？')) return;
    selectedFiles = [];
    renderFileList();
    document.getElementById('matchCard').classList.add('hidden');
    document.getElementById('btnGenerate').disabled = true;
    document.getElementById('btnGenerate').textContent = '📑 开始生成PDF';
}

// ===== 格式化文件大小 =====
function formatSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// ===== 渲染文件列表 =====
function renderFileList() {
    var container = document.getElementById('fileListContainer');
    var emptyHint = document.getElementById('emptyFileHint');

    if (selectedFiles.length === 0) {
        container.classList.add('hidden');
        emptyHint.classList.remove('hidden');
        return;
    }

    container.classList.remove('hidden');
    emptyHint.classList.add('hidden');

    var totalSize = selectedFiles.reduce(function(sum, f) { return sum + f.size; }, 0);
    document.getElementById('fileListSummary').textContent =
        '共 ' + selectedFiles.length + ' 个文件，总计 ' + formatSize(totalSize);

    var html = '';
    for (var i = 0; i < selectedFiles.length; i++) {
        var f = selectedFiles[i];
        var icon = f.fromFolder ? '📁' : '📄';
        var folderInfo = f.fromFolder && f.folderName ? ' <span class="file-folder">(' + f.folderName + ')</span>' : '';
        html += '<div class="file-list-item">';
        html += '<span class="file-list-icon">' + icon + '</span>';
        html += '<span class="file-list-name">' + f.name + folderInfo + '</span>';
        html += '<span class="file-list-size">' + formatSize(f.size) + '</span>';
        html += '<button class="file-list-remove" onclick="removeFile(' + i + ')" title="移除">✕</button>';
        html += '</div>';
    }
    document.getElementById('fileList').innerHTML = html;
}

// ===== 扫描匹配 =====
function scanMatch() {
    if (selectedFiles.length === 0) return;

    var filePaths = selectedFiles.map(function(f) { return f.absPath; });

    document.getElementById('btnGenerate').disabled = true;

    fetch('/pdfmerge/api/scan_match', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            file_paths: filePaths,
            mode: currentMode,
        })
    })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) {
                alert(data.error);
                return;
            }
            renderMatchResult(data);
        })
        .catch(function(err) {
            alert('扫描失败: ' + err.message);
        });
}

// ===== 渲染匹配结果 =====
function renderMatchResult(data) {
    var card = document.getElementById('matchCard');
    card.classList.remove('hidden');

    document.getElementById('matchSummary').textContent =
        '匹配 ' + data.matched_files + '/' + data.total_files + ' 个文件';

    var html = '';
    for (var i = 0; i < data.sections.length; i++) {
        var sec = data.sections[i];
        var cls = 'section-item';
        var statusCls = '';
        var statusIcon = '';
        var filesText = '';

        if (sec.matched) {
            cls += ' matched';
            statusCls = 'ok';
            statusIcon = '✓';
            filesText = sec.file_count + ' 个文件: ' + sec.files.join(', ').substring(0, 120);
            if (sec.files.join(', ').length > 120) filesText += '...';
        } else if (sec.required) {
            cls += ' unmatched';
            statusCls = 'miss';
            statusIcon = '✗';
            filesText = '未匹配到文件（必需）';
        } else {
            cls += ' optional';
            statusCls = 'skip';
            statusIcon = '–';
            filesText = '未匹配到文件（可选，将跳过）';
        }

        html += '<div class="' + cls + '">';
        html += '<div class="section-status ' + statusCls + '">' + statusIcon + '</div>';
        html += '<div class="section-name">' + sec.name + '</div>';
        html += '<div class="section-files">' + filesText + '</div>';
        html += '</div>';
    }

    document.getElementById('sectionList').innerHTML = html;

    // 显示未匹配文件
    var unmatchedEl = document.getElementById('unmatchedInfo');
    if (data.unmatched_count > 0) {
        unmatchedEl.classList.remove('hidden');
        unmatchedEl.textContent = '⚠️ ' + data.unmatched_count + ' 个文件未匹配到任何章节，将被忽略: ' +
            data.unmatched_files.join(', ').substring(0, 300);
        if (data.unmatched_files.join(', ').length > 300) {
            unmatchedEl.textContent += '...';
        }
    } else {
        unmatchedEl.classList.add('hidden');
    }

    // 检查是否有必需章节缺失
    var missingRequired = false;
    for (var j = 0; j < data.sections.length; j++) {
        if (data.sections[j].required && !data.sections[j].matched) {
            missingRequired = true;
            break;
        }
    }

    var btn = document.getElementById('btnGenerate');
    if (data.matched_files > 0) {
        btn.disabled = false;
        if (missingRequired) {
            btn.textContent = '⚠️ 部分必需资料缺失，仍要生成PDF';
        }
    } else {
        btn.disabled = true;
        btn.textContent = '📑 未匹配到任何文件';
    }
}

// ===== 开始生成 =====
function startGenerate() {
    var coverTitle = document.getElementById('coverTitle').value.trim();
    if (!coverTitle) {
        alert('请输入封面标题');
        document.getElementById('coverTitle').focus();
        return;
    }
    if (selectedFiles.length === 0) {
        alert('请先添加资料文件');
        return;
    }

    var filePaths = selectedFiles.map(function(f) { return f.absPath; });

    var payload = {
        file_paths: filePaths,
        mode: currentMode,
        cover_title: coverTitle,
        company_name: coverTitle, // 封面标题即公司名
    };

    if (currentMode === 'refund') {
        payload.period_start = document.getElementById('periodStart').value.trim();
        payload.period_end = document.getElementById('periodEnd').value.trim();
    } else {
        payload.deduction_period = document.getElementById('deductionPeriod').value.trim();
        payload.proof_period = document.getElementById('proofPeriod').value.trim();
    }

    document.getElementById('btnGenerate').disabled = true;
    document.getElementById('btnGenerate').textContent = '生成中...';

    fetch('/pdfmerge/api/generate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) {
                showError(data.error);
                return;
            }
            task_id = data.task_id;
            document.getElementById('progressCard').classList.remove('hidden');
            pollProgress();
        })
        .catch(function(err) {
            showError('启动生成失败: ' + err.message);
        });
}

// ===== 轮询进度 =====
function pollProgress() {
    if (!task_id) return;

    fetch('/pdfmerge/api/progress/' + task_id)
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) {
                showError(data.error);
                return;
            }

            var pct = 0;
            if (data.total > 0) {
                pct = Math.round((data.current / data.total) * 100);
            }

            document.getElementById('progressBar').style.width = pct + '%';
            document.getElementById('progressText').textContent = data.message || '处理中...';
            document.getElementById('progressDetail').textContent =
                data.total > 0 ? (data.current + ' / ' + data.total) : '';

            if (data.status === 'done') {
                showResult(data);
            } else if (data.status === 'error') {
                showError(data.error || '生成失败');
            } else {
                setTimeout(pollProgress, 1000);
            }
        })
        .catch(function(err) {
            showError('查询进度失败: ' + err.message);
        });
}

// ===== 显示结果 =====
function showResult(data) {
    document.getElementById('progressCard').classList.add('hidden');
    document.getElementById('resultCard').classList.remove('hidden');
    document.getElementById('resultDesc').textContent =
        '文件名: ' + (data.output_filename || 'output.pdf');
}

// ===== 显示错误 =====
function showError(msg) {
    document.getElementById('progressCard').classList.add('hidden');
    document.getElementById('errorCard').classList.remove('hidden');
    document.getElementById('errorMsg').textContent = msg;
    document.getElementById('btnGenerate').disabled = false;
    document.getElementById('btnGenerate').textContent = '📑 开始生成PDF';
}

// ===== 保存到指定位置 =====
function saveTo() {
    if (!task_id) return;

    fetch('/pdfmerge/api/save_to/' + task_id, {
        method: 'POST',
    })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.cancelled) return;
            if (data.error) {
                alert(data.error);
                return;
            }
            alert('已保存到: ' + data.save_dir + '\\' + data.filename);
        })
        .catch(function(err) {
            alert('保存失败: ' + err.message);
        });
}

// ===== 直接下载 =====
function downloadPDF() {
    if (!task_id) return;
    window.location.href = '/pdfmerge/api/download/' + task_id;
}


// ===== 页面编辑器 =====
var pageInfo = null;       // {total_pages, cover_count, toc_count, content_count, sections}
var selectedPages = new Set(); // 选中的页面（0-indexed PDF页码）
var insertPosition = -1;   // 插入位置（-1=内容最前面）

function openPageEditor() {
    document.getElementById('resultCard').classList.add('hidden');
    document.getElementById('pageEditorCard').classList.remove('hidden');
    selectedPages.clear();
    loadPageInfo();
}

function closePageEditor() {
    document.getElementById('pageEditorCard').classList.add('hidden');
    document.getElementById('resultCard').classList.remove('hidden');
}

function loadPageInfo() {
    if (!task_id) return;

    var grid = document.getElementById('pageGrid');
    grid.innerHTML = '<div class="page-loading">加载页面信息中...</div>';

    fetch('/pdfmerge/api/pages/' + task_id)
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) {
                grid.innerHTML = '<div class="page-loading error">' + data.error + '</div>';
                return;
            }
            pageInfo = data;
            renderPageGrid();
        })
        .catch(function(err) {
            grid.innerHTML = '<div class="page-loading error">加载失败: ' + err.message + '</div>';
        });
}

function renderPageGrid() {
    if (!pageInfo) return;

    var grid = document.getElementById('pageGrid');
    grid.innerHTML = '';

    var coverCount = pageInfo.cover_count;
    var tocCount = pageInfo.toc_count;
    var total = pageInfo.total_pages;

    // 更新信息栏
    document.getElementById('editorPageInfo').textContent =
        '共 ' + total + ' 页（封面 ' + coverCount + ' + 目录 ' + tocCount + ' + 内容 ' + pageInfo.content_count + '）';

    // 构建章节映射：page -> section_name
    var sectionMap = {};
    if (pageInfo.sections) {
        for (var i = 0; i < pageInfo.sections.length; i++) {
            var sec = pageInfo.sections[i];
            var startPdfPage = sec.start_page - 1; // 转为0-indexed
            for (var j = 0; j < sec.count; j++) {
                sectionMap[startPdfPage + j] = sec.name;
            }
        }
    }

    for (var p = 0; p < total; p++) {
        var item = document.createElement('div');
        item.className = 'page-item';
        item.dataset.page = p;

        // 判断页面类型
        var isLocked = p < coverCount + tocCount;
        var sectionName = sectionMap[p] || '';

        if (isLocked) {
            item.classList.add('locked');
            var lockLabel = (p < coverCount) ? '封面' : '目录';
            sectionName = lockLabel;
        } else if (!sectionName) {
            item.classList.add('unassigned');
            sectionName = '（附加页）';
        }

        // 缩略图
        var thumb = document.createElement('div');
        thumb.className = 'page-thumb';
        thumb.dataset.page = p;
        thumb.dataset.loaded = '0';

        // 懒加载缩略图
        var img = document.createElement('img');
        img.alt = '第' + (p + 1) + '页';
        img.style.opacity = '0';
        thumb.appendChild(img);

        // 加载缩略图
        (function(theImg, pageNum) {
            var url = '/pdfmerge/api/pages/' + task_id + '/thumbnail/' + pageNum;
            fetch(url)
                .then(function(r) { return r.blob(); })
                .then(function(blob) {
                    var urlObj = URL.createObjectURL(blob);
                    theImg.src = urlObj;
                    theImg.style.opacity = '1';
                })
                .catch(function() {
                    theImg.alt = '加载失败';
                    theImg.style.opacity = '0.3';
                });
        })(img, p);

        item.appendChild(thumb);

        // 页码标签
        var label = document.createElement('div');
        label.className = 'page-label';
        label.innerHTML = '<span class="page-num">第' + (p + 1) + '页</span>' +
                          '<span class="page-section">' + sectionName + '</span>';
        item.appendChild(label);

        // 选中/操作按钮
        if (!isLocked) {
            var actions = document.createElement('div');
            actions.className = 'page-actions';

            var selectCheckbox = document.createElement('div');
            selectCheckbox.className = 'page-checkbox';
            selectCheckbox.innerHTML = '☐';
            selectCheckbox.title = '选中此页';
            selectCheckbox.onclick = function(e) {
                e.stopPropagation();
                togglePageSelection(p, selectCheckbox, item);
            };
            actions.appendChild(selectCheckbox);

            var insertBtn = document.createElement('div');
            insertBtn.className = 'page-insert-btn';
            insertBtn.innerHTML = '📄+';
            insertBtn.title = '在此页后插入文件';
            insertBtn.onclick = function(e) {
                e.stopPropagation();
                insertAfterPage(p);
            };
            actions.appendChild(insertBtn);

            item.appendChild(actions);

            // 点击缩略图也能选中
            thumb.onclick = function() {
                togglePageSelection(p, selectCheckbox, item);
            };
        } else {
            var lockIcon = document.createElement('div');
            lockIcon.className = 'page-lock';
            lockIcon.innerHTML = '🔒';
            item.appendChild(lockIcon);
        }

        grid.appendChild(item);
    }

    updateSelectionUI();
}

function togglePageSelection(pageNum, checkboxEl, itemEl) {
    if (selectedPages.has(pageNum)) {
        selectedPages.delete(pageNum);
        checkboxEl.innerHTML = '☐';
        itemEl.classList.remove('selected');
    } else {
        selectedPages.add(pageNum);
        checkboxEl.innerHTML = '☑';
        itemEl.classList.add('selected');
    }
    updateSelectionUI();
}

function updateSelectionUI() {
    var count = selectedPages.size;
    var infoEl = document.getElementById('selectedInfo');
    var btnDelete = document.getElementById('btnDeletePages');

    if (count === 0) {
        infoEl.textContent = '未选中页面';
        btnDelete.disabled = true;
    } else {
        var pages = Array.from(selectedPages).sort(function(a, b) { return a - b; });
        var pageStrs = pages.map(function(p) { return '第' + (p + 1) + '页'; });
        infoEl.textContent = '已选中 ' + count + ' 页: ' + pageStrs.join(', ');
        btnDelete.disabled = false;
    }
}

function deleteSelectedPages() {
    if (selectedPages.size === 0) return;

    var pages = Array.from(selectedPages).sort(function(a, b) { return a - b; });
    var pageStrs = pages.map(function(p) { return '第' + (p + 1) + '页'; });

    if (!confirm('确认删除以下页面？\n\n' + pageStrs.join('\n') + '\n\n删除后目录将自动更新。')) {
        return;
    }

    var grid = document.getElementById('pageGrid');
    grid.innerHTML = '<div class="page-loading">正在删除页面并更新目录...</div>';

    fetch('/pdfmerge/api/pages/' + task_id + '/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({page_numbers: pages})
    })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.error) {
                alert(data.error);
                loadPageInfo();
                return;
            }
            selectedPages.clear();
            pageInfo = data;
            alert(data.message || '删除成功');
            renderPageGrid();
        })
        .catch(function(err) {
            alert('删除失败: ' + err.message);
            loadPageInfo();
        });
}

function insertAfterPage(pageNum) {
    insertPosition = pageNum;
    doInsert();
}

function insertAfterSelected() {
    if (selectedPages.size === 0) {
        // 没有选中页面，在内容最前面插入
        if (!confirm('未选中页面，将在内容最前面插入文件。继续？')) return;
        insertPosition = -1;
    } else {
        // 取选中页面中最大的页码
        insertPosition = Math.max.apply(null, Array.from(selectedPages));
    }
    doInsert();
}

function doInsert() {
    var grid = document.getElementById('pageGrid');
    var insertPosLabel = insertPosition >= 0 ? '第' + (insertPosition + 1) + '页后' : '内容最前面';
    grid.innerHTML = '<div class="page-loading">请在弹出的对话框中选择要插入的文件（插入位置：' + insertPosLabel + '）...</div>';

    fetch('/pdfmerge/api/pages/' + task_id + '/insert', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({after_page: insertPosition})
    })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (data.cancelled) {
                loadPageInfo();
                return;
            }
            if (data.error) {
                alert(data.error);
                loadPageInfo();
                return;
            }
            selectedPages.clear();
            pageInfo = data;
            alert(data.message || '插入成功');
            renderPageGrid();
        })
        .catch(function(err) {
            alert('插入失败: ' + err.message);
            loadPageInfo();
        });
}
