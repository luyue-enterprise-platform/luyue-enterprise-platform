/**
 * 批量PDF转WORD系统 — 前端交互逻辑
 */

// ===== 全局状态 =====
var selectedFiles = [];     // [{file: File|null, name: String, size: Number, fromFolder: Boolean}, ...]
var pickIds = [];           // 文件夹选择ID列表（支持多个文件夹累加）
var currentTaskId = null;
var pollTimer = null;

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
    // 真正的 drop 处理在底部"支持文件夹拖拽"区域统一处理
});

fileInput.addEventListener('change', function () {
    addFiles(this.files);
    this.value = '';
});

function addFiles(fileList) {
    var added = 0;
    var skipped = 0;
    for (var i = 0; i < fileList.length; i++) {
        var f = fileList[i];
        var ext = f.name.split('.').pop().toLowerCase();
        if (ext !== 'pdf') {
            skipped++;
            continue;
        }
        // 避免重复
        var dup = selectedFiles.some(function (sf) {
            var sfn = sf.file ? sf.file.name : sf.name;
            var sfs = sf.file ? sf.file.size : sf.size;
            return sfn === f.name && sfs === f.size;
        });
        if (dup) continue;

        selectedFiles.push({file: f, name: f.name, size: f.size, fromFolder: false});
        added++;
    }
    if (skipped > 0) {
        showToast('跳过了 ' + skipped + ' 个非PDF文件', 'info');
    }
    if (added > 0) {
        renderFileList();
        checkReady();
    }
}

// ===== 通过系统原生对话框选择文件夹（累加模式） =====
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
            if (data.ok) {
                pickIds.push(data.pick_id);
                var added = 0;
                for (var i = 0; i < data.files.length; i++) {
                    var f = data.files[i];
                    var dup = selectedFiles.some(function (sf) {
                        var sfn = sf.file ? sf.file.name : sf.name;
                        var sfs = sf.file ? sf.file.size : sf.size;
                        return sfn === f.name && sfs === (f.size || 0);
                    });
                    if (dup) continue;
                    selectedFiles.push({
                        file: null,
                        name: f.name,
                        size: f.size || 0,
                        fromFolder: true
                    });
                    added++;
                }
                renderFileList();
                checkReady();
                showToast('已添加文件夹: ' + data.folder_name + '，新增 ' + added + ' 个PDF（共 ' + selectedFiles.length + ' 个）', 'success');
            }
        })
        .catch(function (err) {
            showToast('文件夹选择失败: ' + err.message, 'error');
        });
}

function renderFileList() {
    document.getElementById('fileList').style.display = 'block';
    document.getElementById('fileCount').textContent = '共 ' + selectedFiles.length + ' 个 PDF 文件';
    document.getElementById('fileItems').innerHTML = selectedFiles.map(function (item, idx) {
        var folderBadge = item.fromFolder
            ? '<span class="folder-badge" title="来自文件夹选择">📁</span>'
            : '';
        return '<span class="file-tag">' + folderBadge +
            '<span>📄</span>' +
            '<span class="file-name" title="' + esc(item.name) + '">' + esc(item.name) + '</span>' +
            '<span style="color:#999;font-size:11px;">(' + formatSize(item.size) + ')</span>' +
            '<span class="file-remove" onclick="removeFile(' + idx + ')">✕</span>' +
            '</span>';
    }).join('');
}

function formatSize(bytes) {
    if (bytes < 1024) return bytes + 'B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + 'KB';
    return (bytes / 1024 / 1024).toFixed(1) + 'MB';
}

function removeFile(idx) {
    selectedFiles.splice(idx, 1);
    renderFileList();
    checkReady();
}

function clearFiles() {
    if (selectedFiles.length > 0 && !confirm('确定要清空所有已选文件吗？共 ' + selectedFiles.length + ' 个')) {
        return;
    }
    selectedFiles = [];
    pickIds = [];
    document.getElementById('fileList').style.display = 'none';
    hideActionAndResult();
}

// ===== 检查是否可开始 =====
function checkReady() {
    var actionSection = document.getElementById('actionSection');
    if (selectedFiles.length > 0) {
        actionSection.style.display = 'block';
    } else {
        actionSection.style.display = 'none';
    }
}

// ===== 开始转换 =====
function startConvert() {
    if (selectedFiles.length === 0) {
        showToast('请先上传 PDF 文件', 'error');
        return;
    }

    var startBtn = document.getElementById('startBtn');
    startBtn.disabled = true;
    startBtn.textContent = '转换中...';

    var formData = new FormData();

    // 发送文件夹选择ID（支持多个）
    if (pickIds.length > 0) {
        formData.append('pick_ids', pickIds.join(','));
    }

    // 添加上传的File对象
    selectedFiles.forEach(function (item) {
        if (item.file) {
            formData.append('files', item.file);
        }
    });

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
            renderResult(data.results, data.skipped || []);
        })
        .catch(function (err) {
            showToast('获取结果失败: ' + err.message, 'error');
        });
}

// ===== 渲染结果 =====
function renderResult(results, skipped) {
    document.getElementById('resultSection').style.display = 'block';

    var successCount = 0;
    var failCount = 0;
    var totalPages = 0;

    for (var i = 0; i < results.length; i++) {
        if (results[i].ok) {
            successCount++;
            totalPages += results[i].pages || 0;
        } else {
            failCount++;
        }
    }

    // 统计卡片
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
            '<div class="stat-value">' + results.length + '</div>' +
            '<div class="stat-label">文件总数</div>' +
        '</div>';

    // 转换详情表格
    var tbody = document.getElementById('resultTableBody');
    if (results.length > 0) {
        tbody.innerHTML = results.map(function (item, idx) {
            var statusHtml = item.ok
                ? '<span class="col-status-ok">✓ 成功</span>'
                : '<span class="col-status-fail">✗ 失败</span>';
            var actionHtml = '';
            if (item.ok) {
                actionHtml = '<button class="btn-download" onclick="downloadSingle(this,\'' + esc(item.docx_name) + '\')">下载</button>';
            } else if (item.error) {
                actionHtml = '<span style="color:#E74C3C;font-size:12px;" title="' + esc(item.error) + '">' + esc(item.error.substring(0, 20)) + (item.error.length > 20 ? '...' : '') + '</span>';
            }
            return '<tr>' +
                '<td class="col-seq">' + (idx + 1) + '</td>' +
                '<td class="col-name" title="' + esc(item.pdf_name) + '">' + esc(item.pdf_name) + '</td>' +
                '<td class="col-name">' + (item.ok ? esc(item.docx_name) : '-') + '</td>' +
                '<td>' + (item.ok ? item.pages : '-') + '</td>' +
                '<td>' + statusHtml + '</td>' +
                '<td>' + actionHtml + '</td>' +
                '</tr>';
        }).join('');
    } else {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#999;">无转换结果</td></tr>';
    }

    document.getElementById('progressSection').style.display = 'none';
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
}

// ===== 全部重置 =====
function resetAll() {
    clearFiles();
    pickIds = [];
    hideActionAndResult();
    document.getElementById('startBtn').disabled = false;
    document.getElementById('startBtn').textContent = '🚀 开始转换';
}

// ===== 支持文件夹拖拽 =====
dropzone.addEventListener('drop', function (e) {
    e.preventDefault();
    dropzone.classList.remove('dragover');

    var items = e.dataTransfer.items;
    if (!items || !items[0] || typeof items[0].webkitGetAsEntry !== 'function') {
        addFiles(e.dataTransfer.files);
        return;
    }

    var entries = [];
    for (var i = 0; i < items.length; i++) {
        var entry = items[i].webkitGetAsEntry();
        if (entry) entries.push(entry);
    }

    if (entries.length === 0) {
        addFiles(e.dataTransfer.files);
        return;
    }

    // 递归读取文件夹
    var allFiles = [];
    function processEntry(entry) {
        if (entry.isFile) {
            return new Promise(function (resolve) {
                entry.file(function (file) {
                    allFiles.push(file);
                    resolve();
                }, function () { resolve(); });
            });
        } else if (entry.isDirectory) {
            return new Promise(function (resolve) {
                var dirReader = entry.createReader();
                function readBatch() {
                    dirReader.readEntries(function (entries) {
                        if (entries.length === 0) {
                            resolve();
                            return;
                        }
                        var promises = entries.map(function (e) { return processEntry(e); });
                        Promise.all(promises).then(readBatch);
                    }, function () { resolve(); });
                }
                readBatch();
            });
        }
        return Promise.resolve();
    }

    Promise.all(entries.map(function (entry) { return processEntry(entry); }))
        .then(function () {
            if (allFiles.length > 0) {
                addFiles(allFiles);
            } else {
                addFiles(e.dataTransfer.files);
            }
        });
});
