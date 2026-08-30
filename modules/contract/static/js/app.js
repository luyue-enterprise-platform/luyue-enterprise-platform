/**
 * 劳动合同图片整理系统 — 前端交互逻辑
 */

// ===== 全局状态 =====
var rosterData = [];           // [{seq, name, idcard}, ...]
var selectedFiles = [];        // [{file: File|null, name: String, folder: String|null, size: Number, fromFolder: Boolean}, ...]
var pickIds = [];              // 文件夹选择ID列表（支持多个文件夹累加）
var currentTaskId = null;
var pollTimer = null;
var previewData = null;       // 重命名计划预览数据 {auto, duplicates, unmatched, roster_missing, total}

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
    document.getElementById('previewSection').style.display = 'none';
    previewData = null;

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
                document.getElementById('previewSection').style.display = 'none';
                fetchResult();
            } else if (data.status === 'preview') {
                // 分析完成：获取重命名计划，渲染预览界面
                clearInterval(pollTimer);
                pollTimer = null;
                document.getElementById('progressBar').style.width = '75%';
                document.getElementById('startBtn').disabled = false;
                document.getElementById('startBtn').textContent = '🚀 开始整理';
                fetchPreview();
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

// ===== 重命名计划预览 =====

function fetchPreview() {
    fetch('/contract/api/preview/' + currentTaskId)
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.error) {
                showToast(data.error, 'error');
                return;
            }
            renderPreview(data.plan || {});
        })
        .catch(function (err) {
            showToast('获取预览数据失败: ' + err.message, 'error');
        });
}

// 生成建议新文件名（重名项按所选归属人生成）
function suggestName(person, originalName) {
    var ext = originalName.indexOf('.') >= 0 ? originalName.slice(originalName.lastIndexOf('.')) : '';
    var seqStr = ('0' + person.seq).slice(-2);
    return seqStr + '-' + person.name + (person.idcard_tail ? '-' + person.idcard_tail : '') + ext;
}

function renderPreview(plan) {
    previewData = plan;
    var section = document.getElementById('previewSection');
    section.style.display = 'block';

    var auto = plan.auto || [];
    var duplicates = plan.duplicates || [];
    var unmatched = plan.unmatched || [];
    var missing = plan.roster_missing || [];

    // 统计卡片
    document.getElementById('previewStats').innerHTML =
        '<div class="stat-card success">' +
            '<div class="stat-value">' + auto.length + '</div>' +
            '<div class="stat-label">自动匹配</div>' +
        '</div>' +
        '<div class="stat-card warn">' +
            '<div class="stat-value">' + duplicates.length + '</div>' +
            '<div class="stat-label">重名待确认</div>' +
        '</div>' +
        '<div class="stat-card danger">' +
            '<div class="stat-value">' + unmatched.length + '</div>' +
            '<div class="stat-label">未匹配文件</div>' +
        '</div>' +
        '<div class="stat-card">' +
            '<div class="stat-value">' + missing.length + '</div>' +
            '<div class="stat-label">花名册无合同</div>' +
        '</div>' +
        '<div class="stat-card">' +
            '<div class="stat-value">' + (plan.total || 0) + '</div>' +
            '<div class="stat-label">总文件数</div>' +
        '</div>';

    // 自动匹配表（新文件名可编辑）
    var autoBlock = document.getElementById('previewAutoBlock');
    var autoBody = document.getElementById('previewAutoBody');
    if (auto.length > 0) {
        autoBlock.style.display = 'block';
        autoBody.innerHTML = auto.map(function (item, idx) {
            return '<tr>' +
                '<td class="col-seq">' + item.seq + '</td>' +
                '<td>' + esc(item.name) + '</td>' +
                '<td class="col-orig" title="' + esc(item.original) + '">' + esc(item.original) + '</td>' +
                '<td><input type="text" class="preview-name-input" id="pvAuto_' + idx + '" value="' + esc(item.new_name) + '"></td>' +
                '</tr>';
        }).join('');
    } else {
        autoBlock.style.display = 'none';
    }

    // 重名待确认表（归属人下拉 + 可编辑新文件名）
    var dupBlock = document.getElementById('previewDupBlock');
    var dupBody = document.getElementById('previewDupBody');
    if (duplicates.length > 0) {
        dupBlock.style.display = 'block';
        dupBody.innerHTML = duplicates.map(function (d, idx) {
            var options = (d.candidates || []).map(function (p) {
                var label = p.seq + ' - ' + p.name + (p.idcard_tail ? '（***' + p.idcard_tail + '）' : '');
                return '<option value="' + p.seq + '">' + esc(label) + '</option>';
            }).join('');
            return '<tr>' +
                '<td class="col-orig" title="' + esc(d.original) + '">' + esc(d.original) + '</td>' +
                '<td>' + esc(d.guessed || '(未识别)') + '</td>' +
                '<td class="col-reason">' + esc(d.reason) + '</td>' +
                '<td><select class="conflict-select" id="pvSel_' + idx + '" onchange="onDupSelect(' + idx + ')">' +
                    options +
                    '<option value="pending">📂 移入待处理</option>' +
                '</select></td>' +
                '<td><input type="text" class="preview-name-input" id="pvDupName_' + idx + '" value="" placeholder="选择归属人后自动生成，可修改"></td>' +
                '</tr>';
        }).join('');
        // 初始化各重名项的建议文件名
        for (var i = 0; i < duplicates.length; i++) {
            onDupSelect(i);
        }
    } else {
        dupBlock.style.display = 'none';
    }

    // 未匹配文件
    var unmatchedBlock = document.getElementById('previewUnmatchedBlock');
    var unmatchedList = document.getElementById('previewUnmatchedList');
    if (unmatched.length > 0) {
        unmatchedBlock.style.display = 'block';
        unmatchedList.innerHTML = unmatched.map(function (f) {
            return '<span class="unmatched-tag" title="' + esc(f.reason || '') + '">' +
                esc(f.original) +
                (f.guessed ? '<span class="unmatched-reason">' + esc(f.guessed) + '</span>' : '') +
                '</span>';
        }).join('');
    } else {
        unmatchedBlock.style.display = 'none';
    }

    // 花名册无对应合同的人员
    var missingBlock = document.getElementById('previewMissingBlock');
    var missingList = document.getElementById('previewMissingList');
    if (missing.length > 0) {
        missingBlock.style.display = 'block';
        missingList.innerHTML = missing.map(function (p) {
            return '<span class="roster-tag"><span class="tag-seq">' + p.seq + '</span>' + esc(p.name) + '</span>';
        }).join('');
    } else {
        missingBlock.style.display = 'none';
    }

    section.scrollIntoView({behavior: 'smooth', block: 'start'});
}

// 重名项归属人切换：自动填充建议文件名
function onDupSelect(idx) {
    var sel = document.getElementById('pvSel_' + idx);
    var input = document.getElementById('pvDupName_' + idx);
    if (!sel || !input || !previewData) return;
    var duplicates = previewData.duplicates || [];
    if (idx >= duplicates.length) return;

    if (sel.value === 'pending') {
        input.value = '';
        input.disabled = true;
        input.placeholder = '将移入待处理文件夹';
        return;
    }
    input.disabled = false;
    input.placeholder = '选择归属人后自动生成，可修改';

    var dup = duplicates[idx];
    var person = null;
    for (var i = 0; i < (dup.candidates || []).length; i++) {
        if (String(dup.candidates[i].seq) === String(sel.value)) {
            person = dup.candidates[i];
            break;
        }
    }
    if (person) {
        input.value = suggestName(person, dup.original);
    }
}

// 确认预览并执行重命名
function confirmPreview(btn) {
    if (!currentTaskId || !previewData) return;

    var renames = [];
    var pending = [];

    // 自动匹配项（新文件名可编辑）
    var auto = previewData.auto || [];
    for (var i = 0; i < auto.length; i++) {
        var input = document.getElementById('pvAuto_' + i);
        var newName = input ? input.value.trim() : auto[i].new_name;
        if (newName) {
            renames.push({original: auto[i].original, new_name: newName});
        } else {
            pending.push(auto[i].original);
        }
    }

    // 重名待确认项
    var duplicates = previewData.duplicates || [];
    for (var j = 0; j < duplicates.length; j++) {
        var sel = document.getElementById('pvSel_' + j);
        var dupInput = document.getElementById('pvDupName_' + j);
        if (sel && sel.value === 'pending') {
            pending.push(duplicates[j].original);
            continue;
        }
        var dupName = dupInput ? dupInput.value.trim() : '';
        if (dupName) {
            renames.push({
                original: duplicates[j].original,
                new_name: dupName,
                seq: sel ? parseInt(sel.value, 10) : null,
            });
        } else {
            pending.push(duplicates[j].original);
        }
    }

    if (renames.length === 0 && pending.length === 0) {
        showToast('没有可执行的重命名项', 'error');
        return;
    }

    if (btn) {
        btn.disabled = true;
        btn.textContent = '正在执行...';
    }
    document.getElementById('previewSection').style.display = 'none';
    document.getElementById('progressSection').style.display = 'block';
    document.getElementById('progressText').textContent = '正在执行重命名...';
    document.getElementById('progressBar').style.width = '80%';

    fetch('/contract/api/execute/' + currentTaskId, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({renames: renames, pending: pending})
    })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.error) {
                if (btn) {
                    btn.disabled = false;
                    btn.textContent = '✔ 确认并执行重命名';
                }
                showToast(data.error, 'error');
                document.getElementById('previewSection').style.display = 'block';
                document.getElementById('progressSection').style.display = 'none';
                return;
            }
            startPolling();
        })
        .catch(function (err) {
            if (btn) {
                btn.disabled = false;
                btn.textContent = '✔ 确认并执行重命名';
            }
            showToast('提交执行请求失败: ' + err.message, 'error');
            document.getElementById('previewSection').style.display = 'block';
            document.getElementById('progressSection').style.display = 'none';
        });
}

// 回滚重命名（依据重命名日志恢复原文件名）
function rollbackRenames(btn) {
    if (!currentTaskId) return;
    if (!confirm('确定要回滚本次重命名吗？输出目录中的文件将恢复为原文件名。')) {
        return;
    }
    var originalText = btn ? btn.textContent : '';
    if (btn) {
        btn.disabled = true;
        btn.textContent = '正在回滚...';
    }

    fetch('/contract/api/rollback/' + currentTaskId, { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (btn) {
                btn.disabled = false;
                btn.textContent = originalText;
            }
            if (data.error) {
                showToast(data.error, 'error');
                return;
            }
            showToast('已回滚 ' + data.reverted + ' 个文件' +
                (data.failed > 0 ? '（' + data.failed + ' 个失败）' : ''), 'success');
            fetchResult();
        })
        .catch(function (err) {
            if (btn) {
                btn.disabled = false;
                btn.textContent = originalText;
            }
            showToast('回滚失败: ' + err.message, 'error');
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
    var resolvedCount = result.conflicts_resolved_count || 0;
    var rolledBackCount = result.rolled_back_count || 0;

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
        (rolledBackCount > 0
            ? '<div class="stat-card info">' +
                '<div class="stat-value">' + rolledBackCount + '</div>' +
                '<div class="stat-label">已回滚</div>' +
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
    document.getElementById('previewSection').style.display = 'none';
    previewData = null;
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
