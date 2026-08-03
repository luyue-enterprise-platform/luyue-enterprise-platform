/**
 * 劳动合同图片整理系统 — 前端交互逻辑
 */

// ===== 全局状态 =====
var rosterData = [];           // [{seq, name, idcard}, ...]
var selectedFiles = [];        // [File, ...]
var currentTaskId = null;
var pollTimer = null;

// ===== 页面元素引用 =====
var rosterFileInput = document.getElementById('rosterFileInput');
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

// ===== 第一步：花名册上传 =====
// 点击上传区域
document.getElementById('rosterUploadBox').addEventListener('dragover', function (e) {
    e.preventDefault();
    this.style.borderColor = '#4A90D9';
});

document.getElementById('rosterUploadBox').addEventListener('dragleave', function (e) {
    e.preventDefault();
    this.style.borderColor = '';
});

document.getElementById('rosterUploadBox').addEventListener('drop', function (e) {
    e.preventDefault();
    this.style.borderColor = '';
    var files = e.dataTransfer.files;
    if (files.length > 0) {
        handleRosterFile(files[0]);
    }
});

rosterFileInput.addEventListener('change', function () {
    if (this.files.length > 0) {
        handleRosterFile(this.files[0]);
    }
});

function handleRosterFile(file) {
    var ext = file.name.split('.').pop().toLowerCase();
    if (['xlsx', 'xls', 'csv'].indexOf(ext) === -1) {
        showToast('不支持的文件格式，请选择 .xlsx / .xls / .csv 文件', 'error');
        return;
    }

    var formData = new FormData();
    formData.append('file', file);

    fetch('/contract/api/roster', { method: 'POST', body: formData })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.error) {
                showToast(data.error, 'error');
                return;
            }
            if (data.ok) {
                rosterData = data.persons;
                renderRoster();
                showToast('花名册解析成功，共 ' + data.count + ' 人', 'success');
                checkReady();
            }
        })
        .catch(function (err) {
            showToast('花名册上传失败: ' + err.message, 'error');
        });
}

function renderRoster() {
    var countEl = document.getElementById('rosterCount');
    var tagsEl = document.getElementById('rosterTags');
    var preview = document.getElementById('rosterPreview');
    var uploadBox = document.getElementById('rosterUploadBox');

    countEl.textContent = '共 ' + rosterData.length + ' 人';
    tagsEl.innerHTML = rosterData.map(function (p) {
        return '<span class="roster-tag"><span class="tag-seq">' + p.seq + '</span>' + esc(p.name) + '</span>';
    }).join('');

    preview.style.display = 'block';
    uploadBox.style.display = 'none';
}

function clearRoster() {
    rosterData = [];
    document.getElementById('rosterPreview').style.display = 'none';
    document.getElementById('rosterUploadBox').style.display = 'block';
    rosterFileInput.value = '';
    hideActionAndResult();
}

// ===== 第二步：图片文件上传 =====
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

function addFiles(fileList) {
    var added = 0;
    for (var i = 0; i < fileList.length; i++) {
        var f = fileList[i];
        var ext = f.name.split('.').pop().toLowerCase();
        var validExts = ['jpg', 'jpeg', 'png', 'bmp', 'gif', 'tiff', 'tif', 'webp', 'pdf'];
        if (validExts.indexOf(ext) === -1) continue;

        // 避免重复
        var dup = selectedFiles.some(function (sf) {
            return sf.name === f.name && sf.size === f.size;
        });
        if (dup) continue;

        selectedFiles.push(f);
        added++;
    }
    if (added > 0) {
        renderFileList();
        checkReady();
    }
}

function renderFileList() {
    document.getElementById('fileList').style.display = 'block';
    document.getElementById('fileCount').textContent = '共 ' + selectedFiles.length + ' 个文件';
    document.getElementById('fileItems').innerHTML = selectedFiles.map(function (f) {
        return '<span class="file-tag"><span>' + getFileIcon(f.name) + '</span><span class="file-name" title="' + esc(f.name) + '">' + esc(f.name) + '</span></span>';
    }).join('');
}

function getFileIcon(name) {
    var ext = name.split('.').pop().toLowerCase();
    if (ext === 'pdf') return '📄';
    return '🖼️';
}

function clearFiles() {
    selectedFiles = [];
    document.getElementById('fileList').style.display = 'none';
    hideActionAndResult();
}

// ===== 检查是否可开始 =====
function checkReady() {
    var actionSection = document.getElementById('actionSection');
    if (rosterData.length > 0 && selectedFiles.length > 0) {
        actionSection.style.display = 'block';
    } else {
        actionSection.style.display = 'none';
    }
}

// ===== 第三步：开始处理 =====
function startProcess() {
    if (rosterData.length === 0) {
        showToast('请先上传花名册', 'error');
        return;
    }
    if (selectedFiles.length === 0) {
        showToast('请先上传劳动合同文件', 'error');
        return;
    }

    var startBtn = document.getElementById('startBtn');
    startBtn.disabled = true;
    startBtn.textContent = '处理中...';

    var formData = new FormData();
    formData.append('roster', JSON.stringify(rosterData));
    selectedFiles.forEach(function (f) {
        formData.append('files', f);
    });

    // 显示进度区
    document.getElementById('progressSection').style.display = 'block';
    document.getElementById('progressBar').style.width = '5%';
    document.getElementById('progressText').textContent = '正在上传文件...';
    document.getElementById('resultSection').style.display = 'none';

    fetch('/contract/api/upload', { method: 'POST', body: formData })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.error) {
                showToast(data.error, 'error');
                startBtn.disabled = false;
                startBtn.textContent = '🚀 开始整理';
                document.getElementById('progressSection').style.display = 'none';
                return;
            }
            currentTaskId = data.task_id;
            document.getElementById('progressText').textContent = '处理中，共 ' + data.total_files + ' 个文件...';
            document.getElementById('progressBar').style.width = '20%';
            startPolling();
        })
        .catch(function (err) {
            showToast('上传失败: ' + err.message, 'error');
            startBtn.disabled = false;
            startBtn.textContent = '🚀 开始整理';
            document.getElementById('progressSection').style.display = 'none';
        });
}

// ===== 进度轮询 =====
function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(pollProgress, 600);
}

function pollProgress() {
    if (!currentTaskId) return;

    fetch('/contract/api/progress/' + currentTaskId)
        .then(function (r) { return r.json(); })
        .then(function (data) {
            document.getElementById('progressText').textContent = data.message || '处理中...';

            if (data.status === 'done') {
                clearInterval(pollTimer);
                pollTimer = null;
                document.getElementById('progressBar').style.width = '100%';
                document.getElementById('startBtn').disabled = false;
                document.getElementById('startBtn').textContent = '🚀 开始整理';
                fetchResult();
            } else if (data.status === 'error') {
                clearInterval(pollTimer);
                pollTimer = null;
                document.getElementById('startBtn').disabled = false;
                document.getElementById('startBtn').textContent = '🚀 开始整理';
                showToast(data.message || '处理失败', 'error');
            } else {
                var pct = Math.min(95, 20 + Math.random() * 40);
                document.getElementById('progressBar').style.width = pct + '%';
            }
        })
        .catch(function () {
            // 忽略网络错误，继续轮询
        });
}

// ===== 获取结果 =====
function fetchResult() {
    fetch('/contract/api/result/' + currentTaskId)
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.error) {
                showToast(data.error, 'error');
                return;
            }
            renderResult(data.result);
        })
        .catch(function (err) {
            showToast('获取结果失败: ' + err.message, 'error');
        });
}

// ===== 渲染结果 =====
function renderResult(result) {
    document.getElementById('resultSection').style.display = 'block';

    // 统计卡片
    var renamed = result.renamed || [];
    var unmatched = result.unmatched || [];

    document.getElementById('statsGrid').innerHTML =
        '<div class="stat-card success">' +
            '<div class="stat-value">' + result.matched_count + '</div>' +
            '<div class="stat-label">匹配成功</div>' +
        '</div>' +
        '<div class="stat-card warn">' +
            '<div class="stat-value">' + result.unmatched_count + '</div>' +
            '<div class="stat-label">未匹配</div>' +
        '</div>' +
        '<div class="stat-card">' +
            '<div class="stat-value">' + result.total + '</div>' +
            '<div class="stat-label">总图片数</div>' +
        '</div>' +
        '<div class="stat-card">' +
            '<div class="stat-value">' + result.roster_count + '</div>' +
            '<div class="stat-label">花名册人数</div>' +
        '</div>';

    // 匹配详情表格
    var tbody = document.getElementById('resultTableBody');
    if (renamed.length > 0) {
        tbody.innerHTML = renamed.map(function (item) {
            return '<tr>' +
                '<td class="col-seq">' + item.person_seq + '</td>' +
                '<td>' + esc(item.person_name) + '</td>' +
                '<td class="col-new">' + esc(item.new_name) + '</td>' +
                '<td class="col-orig" title="' + esc(item.original) + '">' + esc(item.original) + '</td>' +
                '</tr>';
        }).join('');
    } else {
        tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#999;">无匹配结果</td></tr>';
    }

    // 未匹配文件
    var unmatchedBlock = document.getElementById('unmatchedBlock');
    var unmatchedList = document.getElementById('unmatchedList');
    if (unmatched.length > 0) {
        unmatchedBlock.style.display = 'block';
        unmatchedList.innerHTML = unmatched.map(function (f) {
            return '<span class="unmatched-tag">' + esc(f) + '</span>';
        }).join('');
    } else {
        unmatchedBlock.style.display = 'none';
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

    fetch('/contract/api/save_to/' + currentTaskId, { method: 'POST' })
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

// ===== 下载结果 =====
function downloadResult(btn) {
    saveToLocation(btn);
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
    clearRoster();
    clearFiles();
    hideActionAndResult();
    document.getElementById('startBtn').disabled = false;
    document.getElementById('startBtn').textContent = '🚀 开始整理';
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

    // 使用 DataTransferItem API 递归读取文件夹
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
