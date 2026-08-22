/**
 * 劳动合同图片整理系统 — 前端交互逻辑
 */

// ===== 全局状态 =====
var rosterData = [];           // [{seq, name, idcard}, ...]
var selectedFiles = [];        // [{file: File|null, name: String, folder: String|null, size: Number, fromFolder: Boolean}, ...]
var pickIds = [];              // 文件夹选择ID列表（支持多个文件夹累加）
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
    // v1.1.41: 最多穿透五级子文件夹（第0级为拖入的根文件夹，1~5级子文件夹内的文件可读取）
    var MAX_TRAVERSE_DEPTH = 5;
    function processEntry(entry, depth) {
        depth = depth || 0;
        if (entry.isFile) {
            return new Promise(function (resolve) {
                entry.file(function (file) {
                    allFiles.push(file);
                    resolve();
                });
            });
        } else if (entry.isDirectory) {
            // 文件夹本身一律跳过，不读取文件夹名称；到达第五级后不再向下穿透
            if (depth >= MAX_TRAVERSE_DEPTH) {
                return Promise.resolve();
            }
            return new Promise(function (resolve) {
                var dirReader = entry.createReader();
                dirReader.readEntries(function (entries) {
                    var promises = entries.map(function (e) { return processEntry(e, depth + 1); });
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

fileInput.addEventListener('change', function () {
    addFiles(this.files);
    this.value = '';
});

// ===== 原生文件夹选择（累加模式）=====
function pickFolder() {
    showToast('正在打开文件夹选择对话框...', 'info');

    fetch('/contract/api/pick_folder', { method: 'POST' })
        .then(function (r) {
            return r.json();
        })
        .then(function (data) {
            if (data.cancelled) {
                return;
            }
            if (data.error) {
                showToast(data.error, 'error');
                return;
            }
            if (data.ok) {
                // ===== 累加模式：将新选择的文件夹中的文件追加到 selectedFiles =====
                // 不清除之前的文件夹选择，支持多个文件夹累加
                pickIds.push(data.pick_id);
                // 添加新文件夹的文件（去重）
                var added = 0;
                for (var i = 0; i < data.files.length; i++) {
                    var f = data.files[i];
                    var dup = selectedFiles.some(function (sf) {
                        var sfn = sf.file ? sf.file.name : sf.name;
                        var sfs = sf.file ? sf.file.size : sf.size;
                        return sfn === f.name && sfs === (f.size || 0) && (sf.folder || '') === (f.folder || '');
                    });
                    if (dup) continue;
                    selectedFiles.push({
                        file: null,
                        name: f.name,
                        folder: f.folder || null,
                        size: f.size || 0,
                        fromFolder: true
                    });
                    added++;
                }
                renderFileList();
                checkReady();
                showToast('已添加文件夹: ' + data.folder_name + '，新增 ' + added + ' 个文件（共 ' + selectedFiles.length + ' 个）', 'success');
            }
        })
        .catch(function (err) {
            showToast('文件夹选择失败: ' + err.message, 'error');
        });
}

// ===== 添加文件（累加模式，支持对象数组或File数组）=====
function addFiles(fileList) {
    // fileList 可以是 [File, ...] 或 [{file, folder}, ...]
    // 累加模式：不清除之前的选择（文件和文件夹可以混合）
    var added = 0;
    for (var i = 0; i < fileList.length; i++) {
        var item = fileList[i];
        // 兼容两种格式
        var f, folder;
        if (item.file) {
            f = item.file;
            folder = item.folder || null;
        } else {
            f = item;
            folder = null;
        }

        var ext = f.name.split('.').pop().toLowerCase();
        var validExts = ['jpg', 'jpeg', 'png', 'bmp', 'gif', 'tiff', 'tif', 'webp', 'pdf'];
        if (validExts.indexOf(ext) === -1) continue;

        // 避免重复（同名+同大小+同文件夹）
        // 注意：通过文件夹选择添加的文件 sf.file 为 null，需兼容处理
        var dup = selectedFiles.some(function (sf) {
            var sfn = sf.file ? sf.file.name : sf.name;
            var sfs = sf.file ? sf.file.size : sf.size;
            return sfn === f.name && sfs === f.size && (sf.folder || '') === (folder || '');
        });
        if (dup) continue;

        selectedFiles.push({file: f, folder: folder});
        added++;
    }
    if (added > 0) {
        renderFileList();
        checkReady();
    }
}

// ===== 渲染文件列表（含文件夹标签和删除按钮）=====
function renderFileList() {
    document.getElementById('fileList').style.display = 'block';
    document.getElementById('fileCount').textContent = '共 ' + selectedFiles.length + ' 个文件';
    document.getElementById('fileItems').innerHTML = selectedFiles.map(function (item, idx) {
        var fileName = item.file ? item.file.name : (item.name || '未知文件');
        var folderBadge = item.folder
            ? '<span class="folder-badge" title="来自文件夹: ' + esc(item.folder) + '">📁 ' + esc(item.folder) + '</span>'
            : '';
        return '<span class="file-tag">' + folderBadge +
            '<span>' + getFileIcon(fileName) + '</span>' +
            '<span class="file-name" title="' + esc(fileName) + '">' + esc(fileName) + '</span>' +
            '<span class="file-remove" onclick="removeContractFile(' + idx + ')" title="删除此文件">✕</span>' +
            '</span>';
    }).join('');
}

// ===== 单条文件删除 =====
function removeContractFile(idx) {
    selectedFiles.splice(idx, 1);
    if (selectedFiles.length === 0) {
        clearFiles();
    } else {
        renderFileList();
        checkReady();
    }
}

function getFileIcon(name) {
    var ext = name.split('.').pop().toLowerCase();
    if (ext === 'pdf') return '📄';
    return '🖼️';
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

    // 发送文件夹选择ID（支持多个）
    if (pickIds.length > 0) {
        formData.append('pick_ids', pickIds.join(','));
    }

    // 构建文件夹映射：{文件索引: 文件夹名} —— 仅对上传的File对象有效
    var folderMap = {};
    var fileIdx = 0;
    selectedFiles.forEach(function (item) {
        if (item.file) {
            if (item.folder) {
                folderMap[fileIdx] = item.folder;
            }
            fileIdx++;
        }
    });
    formData.append('folder_map', JSON.stringify(folderMap));

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
    document.getElementById('conflictSection').style.display = 'none';
    conflictData = [];

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
                document.getElementById('conflictSection').style.display = 'none';
                fetchResult();
            } else if (data.status === 'conflict') {
                // 冲突暂停：渲染人工确认面板
                clearInterval(pollTimer);
                pollTimer = null;
                document.getElementById('progressBar').style.width = '75%';
                document.getElementById('startBtn').disabled = false;
                document.getElementById('startBtn').textContent = '🚀 开始整理';
                renderConflicts(data.conflicts || []);
            } else if (data.status === 'error') {
                clearInterval(pollTimer);
                pollTimer = null;
                document.getElementById('startBtn').disabled = false;
                document.getElementById('startBtn').textContent = '🚀 开始整理';
                showToast(data.message || '处理失败', 'error');
            } else {
                // 按已处理文件数估算进度
                var pct = 20;
                if (data.total > 0) {
                    pct = Math.min(95, 20 + Math.round((data.current || 0) / data.total * 75));
                }
                document.getElementById('progressBar').style.width = pct + '%';
            }
        })
        .catch(function () {
            // 忽略网络错误，继续轮询
        });
}

// ===== 冲突人工确认 =====
var conflictData = [];   // [{original, guessed, reason, candidates:[{seq,name}]}]

function renderConflicts(conflicts) {
    conflictData = conflicts;
    var section = document.getElementById('conflictSection');
    var tbody = document.getElementById('conflictTableBody');
    section.style.display = 'block';

    tbody.innerHTML = conflicts.map(function (c, idx) {
        var options = (c.candidates || []).map(function (p) {
            return '<option value="' + p.seq + '">' + esc(p.seq + ' - ' + p.name) + '</option>';
        }).join('');
        return '<tr>' +
            '<td class="col-orig" title="' + esc(c.original) + '">' + esc(c.original) + '</td>' +
            '<td>' + esc(c.guessed || '(未识别)') + '</td>' +
            '<td class="col-reason">' + esc(c.reason) + '</td>' +
            '<td><select class="conflict-select" id="conflictSel_' + idx + '">' +
                options +
                '<option value="pending">📂 移入待处理</option>' +
            '</select></td>' +
            '</tr>';
    }).join('');

    section.scrollIntoView({behavior: 'smooth', block: 'start'});
}

function confirmConflicts(btn) {
    if (!currentTaskId || conflictData.length === 0) return;

    var resolutions = [];
    for (var i = 0; i < conflictData.length; i++) {
        var sel = document.getElementById('conflictSel_' + i);
        if (!sel) continue;
        var val = sel.value;
        if (val === 'pending') {
            resolutions.push({original: conflictData[i].original, action: 'pending'});
        } else {
            resolutions.push({original: conflictData[i].original, seq: parseInt(val, 10)});
        }
    }

    if (btn) {
        btn.disabled = true;
        btn.textContent = '正在处理...';
    }
    document.getElementById('conflictSection').style.display = 'none';
    document.getElementById('progressSection').style.display = 'block';
    document.getElementById('progressText').textContent = '正在按确认结果处理冲突文件...';
    document.getElementById('progressBar').style.width = '80%';

    fetch('/contract/api/resolve/' + currentTaskId, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({resolutions: resolutions})
    })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (btn) {
                btn.disabled = false;
                btn.textContent = '✔ 确认并继续处理';
            }
            if (data.error) {
                showToast(data.error, 'error');
                document.getElementById('conflictSection').style.display = 'block';
                document.getElementById('progressSection').style.display = 'none';
                return;
            }
            conflictData = [];
            startPolling();
        })
        .catch(function (err) {
            if (btn) {
                btn.disabled = false;
                btn.textContent = '✔ 确认并继续处理';
            }
            showToast('提交确认结果失败: ' + err.message, 'error');
            document.getElementById('conflictSection').style.display = 'block';
            document.getElementById('progressSection').style.display = 'none';
        });
}

// 全部移入待处理
function resolveAllPending() {
    if (!currentTaskId || conflictData.length === 0) return;
    var selects = document.querySelectorAll('.conflict-select');
    for (var i = 0; i < selects.length; i++) {
        selects[i].value = 'pending';
    }
    confirmConflicts(null);
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
    var resolvedCount = result.conflicts_resolved_count || 0;

    document.getElementById('statsGrid').innerHTML =
        '<div class="stat-card success">' +
            '<div class="stat-value">' + result.matched_count + '</div>' +
            '<div class="stat-label">匹配成功</div>' +
        '</div>' +
        '<div class="stat-card warn">' +
            '<div class="stat-value">' + result.unmatched_count + '</div>' +
            '<div class="stat-label">待处理</div>' +
        '</div>' +
        (resolvedCount > 0
            ? '<div class="stat-card info">' +
                '<div class="stat-value">' + resolvedCount + '</div>' +
                '<div class="stat-label">人工确认冲突</div>' +
              '</div>'
            : '') +
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

    // 待处理文件（含失败原因）
    var unmatchedBlock = document.getElementById('unmatchedBlock');
    var unmatchedList = document.getElementById('unmatchedList');
    if (unmatched.length > 0) {
        unmatchedBlock.style.display = 'block';
        unmatchedList.innerHTML = unmatched.map(function (f) {
            // 兼容旧格式（字符串）与新格式（对象）
            var name = typeof f === 'string' ? f : f.original;
            var reason = (typeof f === 'object' && f.reason) ? f.reason : '';
            return '<span class="unmatched-tag" title="' + esc(reason) + '">' +
                esc(name) +
                (reason ? '<span class="unmatched-reason">' + esc(reason) + '</span>' : '') +
                '</span>';
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
    document.getElementById('conflictSection').style.display = 'none';
    conflictData = [];
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
