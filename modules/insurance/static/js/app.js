/* ===== 鲁岳企业服务.重点群体社保批量统计智能核算系统 - 前端交互 ===== */

// 四险标准顺序
var INSURANCE_ORDER = ['养老保险', '医疗保险', '工伤保险', '失业保险'];

// 当前选中的文件（支持混合：File对象 + 文件夹选择的文件）
var selectedFiles = [];  // [{file: File|null, name: String, size: Number, fromFolder: Boolean}, ...]
var pickIds = [];        // 文件夹选择ID列表（支持多个文件夹累加）
var currentTaskId = null;
var pollTimer = null;
// 花名册数据
var rosterData = [];
// 花名册公司名和源文件路径（用于缴费单位验证）
var rosterCompany = '';
var rosterSourcePath = '';
// 当前结果的图片详情与人员统计（供手动补录/时间段编辑弹窗使用）
var currentImageDetails = [];
var currentPersonStats = [];

// ===== DOM元素 =====
var dropzone = document.getElementById('dropzone');
var fileInput = document.getElementById('fileInput');
var fileInputSingle = document.getElementById('fileInputSingle');
var fileList = document.getElementById('fileList');
var fileItems = document.getElementById('fileItems');
var fileCount = document.getElementById('fileCount');
var btnUpload = document.getElementById('btnUpload');
var btnClear = document.getElementById('btnClear');
var progressSection = document.getElementById('progressSection');
var progressBar = document.getElementById('progressBar');
var progressText = document.getElementById('progressText');
var progressDetail = document.getElementById('progressDetail');
var resultSection = document.getElementById('resultSection');
var summaryBar = document.getElementById('summaryBar');
var resultTable = document.getElementById('resultTable');
var btnDownload = document.getElementById('btnDownload');
var btnSaveToLocation = document.getElementById('btnSaveToLocation');
var btnDownloadOrganized = document.getElementById('btnDownloadOrganized');
var btnRestart = document.getElementById('btnRestart');
var btnPause = document.getElementById('btnPause');
var btnCancel = document.getElementById('btnCancel');
var progressActions = document.getElementById('progressActions');
var toast = document.getElementById('toast');
var navStatus = document.getElementById('navStatus');

// 花名册相关元素
var btnUploadRoster = document.getElementById('btnUploadRoster');
var rosterInput = document.getElementById('rosterInput');
var rosterStatus = document.getElementById('rosterStatus');
var rosterPreview = document.getElementById('rosterPreview');
var rosterCount = document.getElementById('rosterCount');
var rosterList = document.getElementById('rosterList');
var btnClearRoster = document.getElementById('btnClearRoster');

// 刷新页面
var btnRefresh = document.getElementById('btnRefresh');

// 年月范围选择
var yearStartSelect = document.getElementById('yearStart');
var monthStartSelect = document.getElementById('monthStart');
var yearEndSelect = document.getElementById('yearEnd');
var monthEndSelect = document.getElementById('monthEnd');

// ===== 初始化年月范围下拉 =====
(function initYearMonthRange() {
    var now = new Date();
    var currentYear = now.getFullYear();
    var currentMonth = now.getMonth() + 1;  // JS月份0-11，+1得到真实月份
    // 默认范围：往前推3年到当前年
    var defaultStartYear = currentYear - 3;
    var defaultEndYear = currentYear;
    // 下拉范围：2015 ~ 2030
    var minYear = 2015;
    var maxYear = 2030;

    // 填充年份选项
    var yearOptions = '';
    for (var y = minYear; y <= maxYear; y++) {
        yearOptions += '<option value="' + y + '">' + y + '年</option>';
    }
    yearStartSelect.innerHTML = yearOptions;
    yearEndSelect.innerHTML = yearOptions;
    yearStartSelect.value = String(defaultStartYear);
    yearEndSelect.value = String(defaultEndYear);

    // 填充月份选项
    var monthOptions = '';
    for (var m = 1; m <= 12; m++) {
        monthOptions += '<option value="' + m + '">' + m + '月</option>';
    }
    monthStartSelect.innerHTML = monthOptions;
    monthEndSelect.innerHTML = monthOptions;
    monthStartSelect.value = String(currentMonth);
    monthEndSelect.value = String(currentMonth);

    // 联动校验：起始年月不能晚于截止年月
    function validateRange() {
        var sy = parseInt(yearStartSelect.value);
        var sm = parseInt(monthStartSelect.value);
        var ey = parseInt(yearEndSelect.value);
        var em = parseInt(monthEndSelect.value);
        // 比较 (sy*12+sm) vs (ey*12+em)
        if (sy * 12 + sm > ey * 12 + em) {
            // 起始晚于截止，把截止调整为起始
            yearEndSelect.value = yearStartSelect.value;
            monthEndSelect.value = monthStartSelect.value;
        }
    }
    yearStartSelect.addEventListener('change', validateRange);
    monthStartSelect.addEventListener('change', validateRange);
    yearEndSelect.addEventListener('change', function() {
        if (parseInt(yearEndSelect.value) < parseInt(yearStartSelect.value)) {
            yearStartSelect.value = yearEndSelect.value;
        }
        validateRange();
    });
    monthEndSelect.addEventListener('change', validateRange);
})();

// ===== 刷新页面 =====
if (btnRefresh) {
    btnRefresh.addEventListener('click', function() {
        window.location.reload();
    });
}

// ===== 花名册上传 =====
btnUploadRoster.addEventListener('click', function() {
    rosterInput.click();
});

rosterInput.addEventListener('change', function(e) {
    var files = e.target.files;
    if (!files || files.length === 0) return;

    rosterStatus.textContent = '正在解析花名册...';
    btnUploadRoster.disabled = true;

    var formData = new FormData();
    for (var i = 0; i < files.length; i++) {
        formData.append('files', files[i]);
    }

    fetch('/insurance/api/roster', { method: 'POST', body: formData })
        .then(apiJson)
        .then(function(data) {
            btnUploadRoster.disabled = false;
            if (data.error) {
                rosterStatus.textContent = '解析失败';
                showToast(data.error);
                return;
            }
            if (data.errors && data.errors.length > 0) {
                showToast(data.errors.join('; '));
            }
            rosterData = data.roster || [];
            rosterCompany = data.company_name || '';
            rosterSourcePath = data.roster_source_path || '';
            if (rosterData.length === 0) {
                rosterStatus.textContent = '未识别到人员信息';
                showToast('花名册中未识别到人员，请检查表格是否包含"姓名"列');
                return;
            }
            rosterStatus.textContent = '已识别 ' + rosterData.length + ' 人';
            renderRoster();
        })
        .catch(function(err) {
            btnUploadRoster.disabled = false;
            rosterStatus.textContent = '解析失败';
            showToast('花名册上传失败: ' + err.message);
        });
});

function renderRoster() {
    rosterCount.textContent = rosterData.length;
    var html = '';
    for (var i = 0; i < rosterData.length; i++) {
        var item = rosterData[i];
        html += '<span class="roster-item">' +
                 '<span class="roster-seq">' + item.seq + '</span>' +
                 '<span class="roster-name">' + esc(item.name) + '</span>' +
                 '</span>';
    }
    rosterList.innerHTML = html;
    rosterPreview.style.display = 'block';
}

btnClearRoster.addEventListener('click', function() {
    rosterData = [];
    rosterCompany = '';
    rosterSourcePath = '';
    rosterPreview.style.display = 'none';
    rosterStatus.textContent = '未上传花名册';
    rosterInput.value = '';
});

// ===== 拖拽上传 =====
dropzone.addEventListener('click', function() {
    fileInputSingle.click();
});

dropzone.addEventListener('dragover', function(e) {
    e.preventDefault();
    dropzone.classList.add('dragover');
});

dropzone.addEventListener('dragleave', function(e) {
    e.preventDefault();
    dropzone.classList.remove('dragover');
});

dropzone.addEventListener('drop', function(e) {
    e.preventDefault();
    dropzone.classList.remove('dragover');

    var items = e.dataTransfer.items;
    if (items && items.length > 0 && typeof items[0].webkitGetAsEntry === 'function') {
        var entries = [];
        for (var i = 0; i < items.length; i++) {
            var entry = items[i].webkitGetAsEntry();
            if (entry) entries.push(entry);
        }
        if (entries.length > 0) {
            traverseEntries(entries, function(files) {
                handleFiles(files);
            });
            return;
        }
    }
    handleFiles(e.dataTransfer.files);
});

fileInput.addEventListener('change', function(e) {
    handleFiles(e.target.files);
});

fileInputSingle.addEventListener('change', function(e) {
    handleFiles(e.target.files);
});

// ===== 递归读取文件夹（拖拽目录时使用） =====
function traverseEntries(entries, callback) {
    var allFiles = [];
    var pending = entries.length;
    if (pending === 0) { callback([]); return; }

    entries.forEach(function(entry) {
        if (entry.isFile) {
            entry.file(function(file) {
                if (entry.fullPath) {
                    file._relativePath = entry.fullPath;
                }
                allFiles.push(file);
                if (--pending === 0) callback(allFiles);
            }, function() {
                if (--pending === 0) callback(allFiles);
            });
        } else if (entry.isDirectory) {
            var reader = entry.createReader();
            var subEntries = [];
            (function readDir() {
                reader.readEntries(function(results) {
                    if (results.length === 0) {
                        traverseEntries(subEntries, function(subFiles) {
                            allFiles = allFiles.concat(subFiles);
                            if (--pending === 0) callback(allFiles);
                        });
                    } else {
                        subEntries = subEntries.concat(Array.prototype.slice.call(results));
                        readDir();
                    }
                }, function() {
                    if (--pending === 0) callback(allFiles);
                });
            })();
        } else {
            if (--pending === 0) callback(allFiles);
        }
    });
}

function handleFiles(files) {
    var valid = [];
    for (var i = 0; i < files.length; i++) {
        var f = files[i];
        if (f.type.startsWith('image/') ||
            /\.(jpg|jpeg|png|bmp|tif|tiff|pdf)$/i.test(f.name)) {
            valid.push(f);
        }
    }
    if (valid.length === 0) {
        showToast('请选择图片或PDF文件（JPG/PNG/BMP/TIF/PDF）');
        return;
    }
    // ===== 追加模式：避免重复（同名+同大小） =====
    var added = 0;
    for (var j = 0; j < valid.length; j++) {
        var f = valid[j];
        var dup = selectedFiles.some(function (sf) {
            var sfn = sf.file ? sf.file.name : sf.name;
            var sfs = sf.file ? sf.file.size : sf.size;
            return sfn === f.name && sfs === f.size;
        });
        if (dup) continue;
        // 保留相对路径信息
        var entry = {file: f, name: f.name, size: f.size, fromFolder: false};
        if (f._relativePath) entry._relativePath = f._relativePath;
        if (f.webkitRelativePath) entry._relativePath = f.webkitRelativePath;
        selectedFiles.push(entry);
        added++;
    }
    if (added > 0) {
        renderFileList();
        if (added < valid.length) {
            showToast('已添加 ' + added + ' 个文件，跳过 ' + (valid.length - added) + ' 个重复文件');
        }
    } else if (valid.length > 0) {
        showToast('所选文件已全部存在（重复）');
    }
}

// ===== 通过系统原生对话框选择文件夹（累加模式） =====
function pickFolder() {
    showToast('正在打开文件夹选择对话框...');

    fetch('/insurance/api/pick_folder', { method: 'POST' })
        .then(apiJson)
        .then(function(data) {
            if (data.cancelled) return;
            if (data.error) {
                showToast(data.error);
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
                showToast('已添加文件夹: ' + data.folder_name + '，新增 ' + added + ' 个文件（共 ' + selectedFiles.length + ' 个）');
            }
        })
        .catch(function(err) {
            showToast('文件夹选择失败: ' + err.message);
        });
}

function renderFileList() {
    fileCount.textContent = selectedFiles.length;
    fileItems.innerHTML = '';
    for (var i = 0; i < selectedFiles.length; i++) {
        var item = selectedFiles[i];
        var li = document.createElement('li');
        li.className = 'file-list-item';
        var sizeKB = (item.size / 1024).toFixed(1);
        var path = item._relativePath || item.name;
        var folderBadge = item.fromFolder ? '<span class="folder-badge" title="来自文件夹">📁</span> ' : '';
        li.innerHTML = folderBadge + '<span class="file-path">' + esc(path) + '</span>' +
            '<span class="file-size">(' + sizeKB + ' KB)</span>' +
            '<button class="btn-remove" onclick="removeInsuranceFile(' + i + ')" title="删除此文件">✕</button>';
        fileItems.appendChild(li);
    }
    fileList.style.display = 'block';
}

// ===== 单条删除 =====
function removeInsuranceFile(idx) {
    selectedFiles.splice(idx, 1);
    if (selectedFiles.length === 0) {
        fileList.style.display = 'none';
    } else {
        renderFileList();
    }
    fileInput.value = '';
    fileInputSingle.value = '';
}

// ===== 清空全部 =====
btnClear.addEventListener('click', function() {
    if (selectedFiles.length === 0) {
        showToast('当前没有选中文件');
        return;
    }
    if (!confirm('确定要清空所有已选文件吗？共 ' + selectedFiles.length + ' 个')) {
        return;
    }
    selectedFiles = [];
    pickIds = [];
    fileList.style.display = 'none';
    fileInput.value = '';
    fileInputSingle.value = '';
    showToast('已清空所有文件');
});

// ===== 上传并处理 =====
btnUpload.addEventListener('click', function() {
    if (selectedFiles.length === 0) {
        showToast('请先选择图片文件');
        return;
    }
    uploadFiles();
});

// ===== 图片压缩（上传前自动压缩，减少传输量） =====
function compressImage(file, maxWidth, quality) {
    return new Promise(function(resolve) {
        // PDF和小图片不压缩
        if (file.type === 'application/pdf' || file.size < 300 * 1024) {
            resolve(file);
            return;
        }
        var reader = new FileReader();
        reader.onload = function(e) {
            var img = new Image();
            img.onload = function() {
                var w = img.width, h = img.height;
                if (w <= maxWidth && h <= maxWidth) {
                    resolve(file); // 图片不大，不压缩
                    return;
                }
                var scale = maxWidth / Math.max(w, h);
                var canvas = document.createElement('canvas');
                canvas.width = Math.round(w * scale);
                canvas.height = Math.round(h * scale);
                var ctx = canvas.getContext('2d');
                ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
                canvas.toBlob(function(blob) {
                    // 如果压缩后反而更大，用原图
                    if (blob && blob.size < file.size) {
                        var compressed = new File([blob], file.name, { type: file.type });
                        compressed._relativePath = file._relativePath;
                        compressed._originalSize = file.size;
                        resolve(compressed);
                    } else {
                        resolve(file);
                    }
                }, file.type === 'image/png' ? 'image/png' : 'image/jpeg', quality);
            };
            img.onerror = function() { resolve(file); };
            img.src = e.target.result;
        };
        reader.onerror = function() { resolve(file); };
        reader.readAsDataURL(file);
    });
}

// ===== 批量压缩选中文件（仅压缩上传的File对象，文件夹选择的文件已在服务端） =====
async function compressAllFiles(entries) {
    var results = [];
    for (var i = 0; i < entries.length; i++) {
        if (entries[i].file) {
            results.push(await compressImage(entries[i].file, 1600, 0.85));
        } else {
            results.push(null); // 文件夹选择的文件不需要压缩
        }
    }
    return results;
}

function uploadFiles() {
    btnUpload.disabled = true;
    btnUpload.textContent = '准备上传...';
    navStatus.querySelector('span:last-child').textContent = '准备中';

    // 分离File对象和文件夹选择条目
    var fileEntries = selectedFiles.filter(function(e) { return e.file; });
    var folderEntries = selectedFiles.filter(function(e) { return !e.file; });

    // 先压缩图片（仅File对象）
    compressAllFiles(fileEntries).then(function(compressedFiles) {
        // 统计压缩效果
        var totalOrig = 0, totalComp = 0;
        for (var i = 0; i < compressedFiles.length; i++) {
            if (compressedFiles[i]) {
                totalOrig += fileEntries[i].file.size;
                totalComp += compressedFiles[i].size;
            }
        }
        var savedPct = totalOrig > 0 ? Math.round((1 - totalComp / totalOrig) * 100) : 0;

        var formData = new FormData();

        // 添加压缩后的File对象
        for (var i = 0; i < compressedFiles.length; i++) {
            if (compressedFiles[i]) {
                formData.append('files', compressedFiles[i]);
            }
        }

        // 添加文件夹选择ID
        if (pickIds.length > 0) {
            formData.append('pick_ids', pickIds.join(','));
        }

        formData.append('roster', JSON.stringify(rosterData));
        formData.append('roster_company', rosterCompany);
        formData.append('roster_source_path', rosterSourcePath);

        // 添加用户选择的统计年月范围
        formData.append('year_start', yearStartSelect.value);
        formData.append('month_start', monthStartSelect.value);
        formData.append('year_end', yearEndSelect.value);
        formData.append('month_end', monthEndSelect.value);

        // 用 XMLHttpRequest 获取上传进度
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/insurance/api/upload');

        // 上传进度
        xhr.upload.onprogress = function(e) {
            if (e.lengthComputable) {
                var pct = Math.round(e.loaded / e.total * 100);
                btnUpload.textContent = '上传中 ' + pct + '%';
                navStatus.querySelector('span:last-child').textContent = '上传 ' + pct + '%';
                // 显示进度条
                if (progressSection.style.display === 'none' || progressSection.style.display === '') {
                    progressSection.style.display = 'block';
                }
                progressBar.style.width = pct + '%';
                progressBar.textContent = pct + '%';
                var speedText = '';
                if (e.loaded > 0 && pct < 100) {
                    var loadedMB = (e.loaded / 1024 / 1024).toFixed(1);
                    var totalMB = (e.total / 1024 / 1024).toFixed(1);
                    speedText = '上传中 ' + loadedMB + ' / ' + totalMB + ' MB';
                    if (savedPct > 0) {
                        speedText += '（已压缩 ' + savedPct + '%）';
                    }
                }
                progressText.textContent = speedText;
                progressDetail.textContent = selectedFiles.length + ' 个文件';
            }
        };

        xhr.upload.onload = function() {
            btnUpload.textContent = '处理中...';
            navStatus.querySelector('span:last-child').textContent = '识别中';
            progressText.textContent = '上传完成，等待服务器处理...';
        };

        xhr.onload = function() {
            if (xhr.status === 401) {
                window.location.href = '/login';
                return;
            }
            var data;
            try { data = JSON.parse(xhr.responseText); } catch(e) {
                showToast('服务器响应异常');
                btnUpload.disabled = false;
                btnUpload.textContent = '开始识别统计';
                return;
            }
            if (data.error) {
                showToast(data.error);
                btnUpload.disabled = false;
                btnUpload.textContent = '开始识别统计';
                progressSection.style.display = 'none';
                return;
            }
            currentTaskId = data.task_id;
            fileList.style.display = 'none';
            navStatus.querySelector('span:last-child').textContent = '识别中';
            progressActions.style.display = 'flex';
            btnPause.disabled = false;
            btnPause.textContent = '暂停';
            btnCancel.disabled = false;
            startPolling();
        };

        xhr.onerror = function() {
            showToast('上传失败，请检查网络后重试');
            btnUpload.disabled = false;
            btnUpload.textContent = '开始识别统计';
            progressSection.style.display = 'none';
        };

        xhr.send(formData);
    }).catch(function(err) {
        showToast('文件处理失败: ' + err.message);
        btnUpload.disabled = false;
        btnUpload.textContent = '开始识别统计';
    });
}

// ===== 暂停/恢复/取消 =====
btnPause.addEventListener('click', function() {
    if (!currentTaskId) return;
    var isPaused = btnPause.textContent === '恢复';
    var url = '/insurance/api/task/' + currentTaskId + '/' + (isPaused ? 'resume' : 'pause');
    btnPause.disabled = true;
    fetch(url, { method: 'POST' })
        .then(apiJson)
        .then(function(data) {
            btnPause.disabled = false;
            if (data.error) {
                showToast(data.error);
                return;
            }
            if (isPaused) {
                btnPause.textContent = '暂停';
                navStatus.querySelector('span:last-child').textContent = '识别中';
                progressBar.classList.remove('paused');
                showToast('任务已恢复');
            } else {
                btnPause.textContent = '恢复';
                navStatus.querySelector('span:last-child').textContent = '已暂停';
                progressBar.classList.add('paused');
                showToast('任务已暂停');
            }
        })
        .catch(function(err) {
            btnPause.disabled = false;
            showToast('操作失败: ' + err.message);
        });
});

btnCancel.addEventListener('click', function() {
    if (!currentTaskId) return;
    if (!confirm('确定取消当前任务吗？已处理的进度将不会保存。')) return;
    btnCancel.disabled = true;
    btnPause.disabled = true;
    fetch('/insurance/api/task/' + currentTaskId + '/cancel', { method: 'POST' })
        .then(apiJson)
        .then(function(data) {
            if (data.error) {
                showToast(data.error);
                btnCancel.disabled = false;
                btnPause.disabled = false;
                return;
            }
            showToast('任务正在取消...');
        })
        .catch(function(err) {
            btnCancel.disabled = false;
            btnPause.disabled = false;
            showToast('取消失败: ' + err.message);
        });
});

// ===== 轮询进度 =====
function startPolling() {
    pollTimer = setInterval(function() {
        fetch('/insurance/api/progress/' + currentTaskId)
            .then(apiJson)
            .then(function(data) {
                if (data.error) return;
                updateProgress(data);
                if (data.status === 'done') {
                    stopPolling();
                    fetchResult();
                } else if (data.status === 'error') {
                    stopPolling();
                    progressBar.style.width = '100%';
                    progressBar.textContent = '失败';
                    progressBar.style.background = '#E74C3C';
                    progressText.textContent = '处理失败: ' + (data.message || '服务器异常');
                    progressDetail.textContent = '';
                    progressActions.style.display = 'none';
                    btnUpload.disabled = false;
                    btnUpload.textContent = '开始识别统计';
                    navStatus.querySelector('span:last-child').textContent = '失败';
                    showToast('处理失败: ' + (data.message || '服务器异常'));
                } else if (data.status === 'cancelled') {
                    stopPolling();
                    progressBar.style.width = '100%';
                    progressBar.textContent = '已取消';
                    progressBar.classList.add('cancelled');
                    progressText.textContent = '任务已取消';
                    progressDetail.textContent = '';
                    progressActions.style.display = 'none';
                    btnUpload.disabled = false;
                    btnUpload.textContent = '开始识别统计';
                    navStatus.querySelector('span:last-child').textContent = '已取消';
                    showToast('任务已取消');
                }
            })
            .catch(function(err) {});
    }, 800);
}

function stopPolling() {
    if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}

function updateProgress(data) {
    var pct = data.total > 0 ? Math.round(data.current / data.total * 100) : 0;
    
    // 暂停状态特殊显示
    if (data.paused) {
        progressBar.style.width = pct + '%';
        progressBar.textContent = pct + '% (暂停)';
        progressBar.classList.add('paused');
        progressText.textContent = '⏸ 已暂停 — 点击"恢复"继续处理';
        progressDetail.textContent = data.current + ' / ' + data.total + ' 张图片已识别';
        btnPause.textContent = '恢复';
        return;
    }
    
    // 正常处理中
    progressBar.style.width = pct + '%';
    progressBar.textContent = pct + '%';
    progressBar.classList.remove('paused');
    progressText.textContent = data.message || '';
    progressDetail.textContent = data.current + ' / ' + data.total + ' 张图片已识别';
    btnPause.textContent = '暂停';
}

// ===== 获取结果 =====
function fetchResult() {
    fetch('/insurance/api/result/' + currentTaskId)
        .then(apiJson)
        .then(function(data) {
            if (data.error) {
                showToast(data.error);
                return;
            }
            renderResult(data);
            navStatus.querySelector('span:last-child').textContent = '完成';
            navStatus.querySelector('.status-dot').style.background = '#27AE60';
        })
        .catch(function(err) {
            showToast('获取结果失败: ' + err.message);
        });
}

// ===== 渲染结果 =====
function renderResult(data, opts) {
    opts = opts || {};
    progressSection.style.display = 'none';
    progressActions.style.display = 'none';
    resultSection.style.display = 'block';
    currentImageDetails = [];
    currentPersonStats = data.person_stats || [];

    // 汇总信息
    summaryBar.innerHTML = '';
    var summaries = [
        { label: '识别图片', value: data.ocr_count },
        { label: '参保人员', value: data.person_count },
        { label: '统计区间', value: yearStartSelect.value + '年' + monthStartSelect.value + '月<br>至' + yearEndSelect.value + '年' + monthEndSelect.value + '月' },
        { label: '年度列数', value: data.year_cols.length },
        { label: '年度台账', value: data.year_cols.length + '张' },
    ];
    for (var i = 0; i < summaries.length; i++) {
        var div = document.createElement('div');
        div.className = 'summary-item';
        div.innerHTML = '<div class="label">' + summaries[i].label +
            '</div><div class="value">' + summaries[i].value + '</div>';
        summaryBar.appendChild(div);
    }

    // 缴费单位信息
    var companyInfo = document.getElementById('companyInfo');
    if (companyInfo && data.company_name) {
        var companyHtml = '<h3>缴费单位验证</h3>';
        companyHtml += '<p class="company-name-display"><strong>缴费单位：</strong>' + esc(data.company_name) + '</p>';
        if (data.roster_company && data.roster_company !== data.company_name) {
            companyHtml += '<p class="company-warn">⚠ 花名册公司名（' + esc(data.roster_company) +
                '）与参保证明缴费单位（' + esc(data.company_name) + '）不一致，请核实！</p>';
        }
        if (data.company_mismatch_files && data.company_mismatch_files.length > 0) {
            companyHtml += '<p class="company-warn">⚠ 以下 <strong>' + data.company_mismatch_files.length + '</strong> 个文件缴费单位不一致，<strong>已排除，不计入统计</strong>：</p>';
            companyHtml += '<ul class="mismatch-list">';
            for (var mi = 0; mi < data.company_mismatch_files.length; mi++) {
                var mf = data.company_mismatch_files[mi];
                companyHtml += '<li>' + esc(mf.filename) + ' — 识别为 "' + esc(mf.ocr_company) + '"（期望: ' + esc(mf.expected_company) + '）</li>';
            }
            companyHtml += '</ul>';
        }
        if (data.excluded_count && data.excluded_count > 0) {
            companyHtml += '<p class="company-ok">✓ 有效文件 ' + data.success_count + ' 个，排除 ' + data.excluded_count + ' 个（缴费单位不一致）</p>';
        } else {
            companyHtml += '<p class="company-ok">✓ 所有参保证明缴费单位一致，共 ' + data.success_count + ' 个有效文件</p>';
        }
        companyInfo.innerHTML = companyHtml;
        companyInfo.style.display = 'block';
    } else if (companyInfo) {
        companyInfo.style.display = 'none';
    }

    // 文件整理结果（v1.1.45: 折叠面板，默认收起）
    var org = data.organize_result;
    if (org) {
        var abnormalCnt = org.abnormal_count || 0;
        var orgHtml = '<div class="panel-header" onclick="togglePanel(this)" title="点击展开或收起">';
        orgHtml += '<h3>文件整理结果' +
            (abnormalCnt > 0 ? ' <span class="abnormal-badge">⚠ 异常 ' + abnormalCnt + '</span>' : '') +
            '</h3>';
        orgHtml += '<span class="panel-toggle">展开 ▸</span>';
        orgHtml += '</div>';
        orgHtml += '<div class="panel-body">';
        orgHtml += '<p class="organize-summary">已整理 ' + org.organized_count + ' 个文件';
        if (abnormalCnt > 0) {
            orgHtml += '，异常图片 ' + abnormalCnt + ' 个（识别失败/无时间段/缴费单位不一致，可双击行预览或点击"处理"按钮手动处理）';
        }
        if (org.no_roster) {
            orgHtml += '（未上传花名册，按OCR识别姓名命名）';
        }
        orgHtml += '</p>';

        // ===== 每张图片的识别详情（帮助用户定位"险种未识别"等问题） =====
        if (data.image_details && data.image_details.length > 0) {
            orgHtml += '<details class="image-details-block" open>';
            orgHtml += '<summary>📋 每张图片的识别详情（' + data.image_details.length + ' 张，双击行可预览图片）</summary>';
            orgHtml += '<table class="image-details-table">';
            orgHtml += '<thead><tr><th>文件名</th><th>姓名</th><th>身份证</th><th>险种</th><th>时间段</th><th>缴费单位</th><th>状态</th><th>操作</th></tr></thead><tbody>';
            for (var di = 0; di < data.image_details.length; di++) {
                var det = data.image_details[di];
                var ins = det.insurance_type || '';
                var period = det.period || '';
                var insCell = ins
                    ? '<span class="tag-ok">' + esc(ins) + '</span>'
                    : '<span class="tag-bad">⚠ 未识别</span>';
                var periodCell = period
                    ? '<span class="tag-ok">' + esc(period) + (det.is_manual ? '（补录）' : '') + '</span>'
                    : '<span class="tag-bad">⚠ 未识别</span>';
                var statusCell = '';
                var isFailed = false;
                if (det.error && det.error.indexOf('缴费单位不一致') >= 0) {
                    statusCell = '<span class="tag-warn">⚠ ' + esc(det.error) + '</span>';
                } else if (det.error) {
                    statusCell = '<span class="tag-bad">识别失败: ' + esc(det.error) + '</span>';
                    isFailed = true;
                } else if (!ins) {
                    statusCell = '<span class="tag-bad">险种缺失（未归类）</span>';
                } else if (!period) {
                    statusCell = '<span class="tag-warn">时间段缺失</span>';
                } else if (det.is_manual) {
                    statusCell = '<span class="tag-ok">✓ 正常（手动处理）</span>';
                } else {
                    statusCell = '<span class="tag-ok">✓ 正常</span>';
                }
                // v1.1.45: 异常信息备注（编辑/补充后回显）
                if (det.remark) {
                    statusCell += '<div class="det-remark" title="' + esc(det.remark) + '">备注: ' + esc(det.remark) + '</div>';
                }
                // v1.1.45: 操作列——识别失败保留手动补录；所有异常行提供"处理"（命名/编辑信息）
                var isMissing = !det.error && (!ins || !period);
                var actions = [];
                if (isFailed) {
                    actions.push('<button class="btn btn-ghost btn-sm mf-btn" data-idx="' + di + '">手动补录</button>');
                }
                if (det.error || isMissing) {
                    actions.push('<button class="btn btn-ghost btn-sm eh-btn" data-idx="' + di + '">处理</button>');
                }
                // v1.1.45: 手动命名后显示新文件名（title 提示原名），双击行预览原图
                var displayName = det.manual_name || det.filename;
                orgHtml += '<tr data-filename="' + esc(det.filename) + '" title="双击预览图片">';
                orgHtml += '<td title="原文件名: ' + esc(det.filename) + '">' + esc(displayName) + '</td>';
                orgHtml += '<td>' + esc(det.name || '—') + '</td>';
                orgHtml += '<td>' + esc(det.idcard || '—') + '</td>';
                orgHtml += '<td>' + insCell + '</td>';
                orgHtml += '<td>' + periodCell + '</td>';
                orgHtml += '<td>' + esc(det.company_name || '—') + '</td>';
                orgHtml += '<td>' + statusCell + '</td>';
                orgHtml += '<td>' + (actions.length ? actions.join(' ') : '—') + '</td>';
                orgHtml += '</tr>';
            }
            orgHtml += '</tbody></table>';
            orgHtml += '</details>';
            currentImageDetails = data.image_details;
        }

        orgHtml += '<div class="folder-grid">';
        for (var folder in org.folder_structure) {
            var files = org.folder_structure[folder];
            orgHtml += '<div class="folder-card' + (folder === '异常图片' && files.length === 0 ? ' folder-empty' : '') + '">';
            orgHtml += '<div class="folder-name">' + esc(folder) + '</div>';
            orgHtml += '<div class="folder-count">' + files.length + ' 个文件</div>';
            if (files.length > 0) {
                orgHtml += '<ul class="folder-files">';
                for (var fi = 0; fi < Math.min(files.length, 5); fi++) {
                    orgHtml += '<li>' + esc(files[fi]) + '</li>';
                }
                if (files.length > 5) {
                    orgHtml += '<li class="more">...共 ' + files.length + ' 个</li>';
                }
                orgHtml += '</ul>';
            }
            orgHtml += '</div>';
        }
        orgHtml += '</div>';
        if (org.unmatched && org.unmatched.length > 0) {
            orgHtml += '<p class="organize-warn">未归类文件: ' + org.unmatched.length + ' 个</p>';
        }
        orgHtml += '</div>';
        var orgBox = document.getElementById('organizeInfo');
        // v1.1.51: 重渲染时保留面板展开/收起状态（修改异常图片/手动补录后
        // 不得自动收起用户已展开的面板，也不得让页面因布局突变而跳走）
        var keepExpanded = orgBox.style.display !== 'none' && orgBox.innerHTML &&
            !orgBox.classList.contains('collapsed');
        var oldDetails = orgBox.querySelector ? orgBox.querySelector('details.image-details-block') : null;
        var detailsWasOpen = oldDetails ? oldDetails.open : true;
        orgBox.innerHTML = orgHtml;
        orgBox.className = 'organize-info collapsible-panel' + (keepExpanded ? '' : ' collapsed');
        if (keepExpanded) {
            var tog = orgBox.querySelector('.panel-toggle');
            if (tog) tog.textContent = '收起 ▾';
            // 同步保留"每张图片的识别详情"<details>的开合状态
            var newDetails = orgBox.querySelector('details.image-details-block');
            if (newDetails && !detailsWasOpen) newDetails.open = false;
        }
        orgBox.style.display = 'block';
        btnDownloadOrganized.style.display = 'inline-flex';
    }

    // 年度台账文件列表
    var yearlyInfo = document.getElementById('yearlyLedgerInfo');
    if (yearlyInfo && data.yearly_ledger_files && data.yearly_ledger_files.length > 0) {
        var yHtml = '<h3>年度台账文件（独立Excel）</h3>';
        yHtml += '<p class="organize-summary">共生成 ' + data.yearly_ledger_files.length + ' 张年度台账</p>';
        yHtml += '<div class="yearly-file-list">';
        for (var yi = 0; yi < data.yearly_ledger_files.length; yi++) {
            var yfn = data.yearly_ledger_files[yi];
            yHtml += '<div class="yearly-file-item">';
            yHtml += '<span class="yearly-file-icon">📊</span>';
            yHtml += '<span class="yearly-file-name">' + esc(yfn) + '</span>';
            yHtml += '<a href="/insurance/api/download_yearly/' + currentTaskId + '/' + encodeURIComponent(yfn) + '" class="btn btn-ghost btn-sm">下载</a>';
            yHtml += '</div>';
        }
        yHtml += '</div>';
        yearlyInfo.innerHTML = yHtml;
        yearlyInfo.style.display = 'block';
    } else if (yearlyInfo) {
        yearlyInfo.style.display = 'none';
    }

    // 表格（v1.1.45: 抽为独立函数支持按姓名搜索过滤）
    currentYearCols = data.year_cols || [];
    var searchKeyword = (document.getElementById('resultSearch').value || '').trim();
    if (searchKeyword) {
        // 已有搜索关键字：新结果仍沿用过滤
        renderPersonTable();
    } else {
        document.getElementById('resultSearch').value = '';
        renderPersonTable();
    }
    document.getElementById('tableSearchBar').style.display = 'flex';

    // 绑定下载按钮
    btnDownload.onclick = function() {
        saveToLocation('table', btnDownload);
    };
    btnSaveToLocation.onclick = function() {
        saveToLocation('all', btnSaveToLocation);
    };
    btnDownloadOrganized.onclick = function() {
        saveToLocation('organized', btnDownloadOrganized);
    };

    // 绑定"手动补录"按钮（每张图片识别详情中的失败行）
    var mfBtns = document.querySelectorAll('.mf-btn');
    for (var mb = 0; mb < mfBtns.length; mb++) {
        (function(btn) {
            btn.onclick = function() {
                var idx = parseInt(btn.getAttribute('data-idx'), 10);
                if (currentImageDetails[idx]) {
                    openManualFill(currentImageDetails[idx]);
                }
            };
        })(mfBtns[mb]);
    }

    // v1.1.45: 绑定"处理"按钮（异常图片：双击预览/手动命名/编辑异常信息）
    var ehBtns = document.querySelectorAll('.eh-btn');
    for (var eb = 0; eb < ehBtns.length; eb++) {
        (function(btn) {
            btn.onclick = function() {
                var idx = parseInt(btn.getAttribute('data-idx'), 10);
                if (currentImageDetails[idx]) {
                    openExcludedHandle(currentImageDetails[idx]);
                }
            };
        })(ehBtns[eb]);
    }

    // 绑定"修改时间段"按钮由 renderPersonTable() 统一处理（支持搜索过滤后重建）

    // 渲染手动操作记录
    renderOperationLog(data.operation_log);

    // v1.1.51: 修改异常图片/手动补录后，页面停留在被处理图片所在行
    if (opts.scrollToFilename) {
        scrollToImageRow(opts.scrollToFilename);
    }
}

// v1.1.51: 滚动定位到"每张图片的识别详情"中指定文件所在行，并短暂高亮
function scrollToImageRow(filename) {
    var rows = document.querySelectorAll('#organizeInfo tr[data-filename]');
    for (var i = 0; i < rows.length; i++) {
        if (rows[i].getAttribute('data-filename') === filename) {
            rows[i].scrollIntoView({ block: 'center', behavior: 'smooth' });
            rows[i].classList.add('row-just-saved');
            (function(row) {
                setTimeout(function() { row.classList.remove('row-just-saved'); }, 2400);
            })(rows[i]);
            return;
        }
    }
}

// ===== v1.1.45: 结果表渲染（支持按姓名搜索过滤，实时刷新） =====
var currentYearCols = [];
function renderPersonTable() {
    var searchInp = document.getElementById('resultSearch');
    var keyword = (searchInp.value || '').trim().toLowerCase();
    var all = currentPersonStats || [];
    var matched = [];
    for (var i = 0; i < all.length; i++) {
        if (!keyword || (all[i].name || '').toLowerCase().indexOf(keyword) >= 0) {
            matched.push(all[i]);
        }
    }

    // 搜索计数与空状态提示
    var cntEl = document.getElementById('resultSearchCount');
    if (keyword) {
        cntEl.textContent = '匹配 ' + matched.length + ' / ' + all.length + ' 人';
    } else {
        cntEl.textContent = all.length ? '共 ' + all.length + ' 人' : '';
    }

    var html = '<thead><tr>';
    var headers = [
        '序号',
        '姓名', '身份证号', '人员身份类型',
        '退役证编号/就业创业证编号', '退役时间/登记失业时间',
        '养老保险参保证明时间段', '医疗保险参保证明时间段',
        '工伤保险参保证明时间段', '失业保险参保证明时间段',
        '参保证明时间段（养老+医疗+工伤+失业）', '申请退税总月数'
    ];
    for (var j = 0; j < currentYearCols.length; j++) {
        headers.push(currentYearCols[j] + '年申请退税月数');
    }
    headers.push('合计申请退税总额');
    headers.push('操作');
    for (var h = 0; h < headers.length; h++) {
        html += '<th>' + headers[h] + '</th>';
    }
    html += '</tr></thead><tbody>';

    if (keyword && matched.length === 0) {
        // 空状态提示
        html += '<tr class="table-empty-row"><td colspan="' + headers.length + '">未找到姓名包含"' +
            esc(keyword) + '"的人员，请更换关键字</td></tr>';
    }

    for (var p = 0; p < matched.length; p++) {
        var ps = matched[p];
        var srcIdx = all.indexOf(ps);
        html += '<tr>';
        html += '<td>' + (srcIdx + 1) + '</td>';
        html += '<td>' + esc(ps.name) + '</td>';
        html += '<td>' + esc(ps.idcard) + '</td>';
        html += '<td>' + esc(ps.identity_type || '') + '</td>';
        html += '<td></td>';
        html += '<td></td>';

        for (var t = 0; t < INSURANCE_ORDER.length; t++) {
            var ins = ps.insurances[INSURANCE_ORDER[t]];
            if (ins) {
                html += '<td>' + ins.start + '~' + ins.end + '</td>';
            } else {
                html += '<td>-</td>';
            }
        }

        if (ps.has_overlap) {
            html += '<td>' + ps.overlap_start + '~' + ps.overlap_end + '</td>';
            html += '<td>' + ps.overlap_months + '</td>';
        } else {
            html += '<td>-</td><td>0</td>';
        }

        for (var y = 0; y < currentYearCols.length; y++) {
            var m = ps.yearly_months[currentYearCols[y]] || 0;
            html += '<td>' + m + '</td>';
        }

        html += '<td></td>';

        html += '<td><button class="btn btn-ghost btn-sm pe-btn" data-idx="' + srcIdx + '">修改时间段</button></td>';

        html += '</tr>';
    }
    html += '</tbody>';
    resultTable.innerHTML = html;

    // 重新绑定"修改时间段"按钮（过滤后行重建）
    var peBtns = document.querySelectorAll('.pe-btn');
    for (var pb = 0; pb < peBtns.length; pb++) {
        (function(btn) {
            btn.onclick = function() {
                var idx = parseInt(btn.getAttribute('data-idx'), 10);
                if (currentPersonStats[idx]) {
                    openPeriodEdit(currentPersonStats[idx]);
                }
            };
        })(peBtns[pb]);
    }
}

// v1.1.45: 搜索框实时过滤绑定
(function bindResultSearch() {
    var inp = document.getElementById('resultSearch');
    if (inp) {
        inp.addEventListener('input', function() { renderPersonTable(); });
    }
})();

// v1.1.45: 识别详情表格行双击预览（事件委托到 organizeInfo 常驻容器）
(function bindImageDetailsDblclick() {
    var box = document.getElementById('organizeInfo');
    if (!box) return;
    box.addEventListener('dblclick', function(e) {
        var tr = e.target.closest ? e.target.closest('tr[data-filename]') : null;
        if (tr) {
            var fn = tr.getAttribute('data-filename');
            if (fn) openImagePreview(fn);
        }
    });
})();

// ===== 渲染操作记录（手动补录/时间段修改的追溯日志） =====
function renderOperationLog(opLog) {
    var box = document.getElementById('operationLogInfo');
    if (!box) return;
    if (!opLog || opLog.length === 0) {
        box.style.display = 'none';
        box.innerHTML = '';
        return;
    }
    var html = '<details class="operation-log-block">';
    html += '<summary>🕐 手动操作记录（' + opLog.length + ' 条，点击展开/收起）</summary>';
    html += '<table class="image-details-table op-log-table">';
    html += '<thead><tr><th>时间</th><th>操作</th><th>姓名</th><th>身份证</th><th>险种</th><th>变更内容</th></tr></thead><tbody>';
    for (var i = 0; i < opLog.length; i++) {
        var lg = opLog[i];
        var changeText = '';
        if (lg.old && lg.new) {
            changeText = esc(lg.old) + ' → ' + esc(lg.new);
        } else if (lg.new) {
            changeText = '→ ' + esc(lg.new);
        } else if (lg.old) {
            changeText = esc(lg.old) + '（清除）';
        }
        html += '<tr>';
        html += '<td>' + esc(lg.time || '') + '</td>';
        html += '<td>' + esc(lg.action || '') + '</td>';
        html += '<td>' + esc(lg.name || '') + '</td>';
        html += '<td>' + esc(lg.idcard || '—') + '</td>';
        html += '<td>' + esc(lg.insurance_type || '—') + '</td>';
        html += '<td>' + changeText + '</td>';
        html += '</tr>';
    }
    html += '</tbody></table>';
    html += '</details>';
    box.innerHTML = html;
    box.style.display = 'block';
}

// ===== 选择保存位置（弹出系统原生文件夹选择对话框） =====
function saveToLocation(fileType, btn) {
    if (!currentTaskId) return;
    fileType = fileType || 'all';
    var originalText = btn ? btn.textContent : '';
    if (btn) {
        btn.disabled = true;
        btn.textContent = '正在打开保存对话框...';
    }

    fetch('/insurance/api/save_to/' + currentTaskId, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_type: fileType })
    })
        .then(apiJson)
        .then(function(data) {
            if (btn) {
                btn.disabled = false;
                btn.textContent = originalText;
            }
            if (data.cancelled) {
                return;
            }
            if (data.error) {
                showToast(data.error);
                return;
            }
            if (data.ok) {
                showToast('保存成功');
            }
        })
        .catch(function(err) {
            if (btn) {
                btn.disabled = false;
                btn.textContent = originalText;
            }
            showToast('保存失败: ' + err.message);
        });
}

// ===== 重新上传 =====
btnRestart.addEventListener('click', function() {
    resultSection.style.display = 'none';
    fileList.style.display = 'none';
    progressActions.style.display = 'none';
    selectedFiles = [];
    pickIds = [];
    fileInput.value = '';
    fileInputSingle.value = '';
    currentTaskId = null;
    document.getElementById('organizeInfo').style.display = 'none';
    document.getElementById('yearlyLedgerInfo').style.display = 'none';
    // v1.1.45: 重置搜索框与计数
    document.getElementById('tableSearchBar').style.display = 'none';
    document.getElementById('resultSearch').value = '';
    document.getElementById('resultSearchCount').textContent = '';
    document.getElementById('imagePreviewModal').style.display = 'none';
    document.getElementById('excludedHandleModal').style.display = 'none';
    currentImageDetails = [];
    currentPersonStats = [];
    btnDownloadOrganized.style.display = 'none';
    navStatus.querySelector('span:last-child').textContent = '系统就绪';
    navStatus.querySelector('.status-dot').style.background = '';
    btnUpload.disabled = false;
    btnUpload.textContent = '开始识别统计';
    btnPause.textContent = '暂停';
    btnPause.disabled = false;
    btnCancel.disabled = false;
    progressBar.classList.remove('paused', 'cancelled');
    progressBar.style.background = '';
    window.scrollTo(0, 0);
});

// ===== v1.1.43: 手动补录弹窗（识别失败的图片） =====
var currentManualFillFilename = '';

function openManualFill(det) {
    currentManualFillFilename = det.filename;
    document.getElementById('manualFillFileTip').textContent = '文件：' + det.filename +
        (det.error ? '（失败原因: ' + det.error + '）' : '');
    document.getElementById('mfName').value = det.name || '';
    document.getElementById('mfIdcard').value = det.idcard || '';
    var sel = document.getElementById('mfInsurance');
    sel.value = INSURANCE_ORDER.indexOf(det.insurance_type) >= 0 ? det.insurance_type : '养老保险';
    document.getElementById('mfStart').value = '';
    document.getElementById('mfEnd').value = '';
    document.getElementById('btnMfSubmit').disabled = false;
    document.getElementById('manualFillModal').style.display = 'flex';
}

function closeManualFill() {
    document.getElementById('manualFillModal').style.display = 'none';
    currentManualFillFilename = '';
}

function submitManualFill() {
    if (!currentTaskId || !currentManualFillFilename) return;
    var name = document.getElementById('mfName').value.trim();
    var idcard = document.getElementById('mfIdcard').value.trim();
    var insuranceType = document.getElementById('mfInsurance').value;
    var start = document.getElementById('mfStart').value.trim();
    var end = document.getElementById('mfEnd').value.trim();

    if (!name) { showToast('请输入姓名'); return; }
    if (idcard && !/^\d{15}$|^\d{17}[\dXx]$/.test(idcard)) {
        showToast('身份证号格式不正确（15位或18位）');
        return;
    }
    var ymRe = /^\d{4}-(0[1-9]|1[0-2])$/;
    if (!ymRe.test(start) || !ymRe.test(end)) {
        showToast('起始/截止年月格式应为 YYYY-MM，如 2023-01');
        return;
    }
    if (start > end) {
        showToast('起始年月不能晚于截止年月');
        return;
    }

    var btn = document.getElementById('btnMfSubmit');
    btn.disabled = true;
    btn.textContent = '提交中...';
    fetch('/insurance/api/manual_fill/' + currentTaskId, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            filename: currentManualFillFilename,
            name: name,
            idcard: idcard,
            insurance_type: insuranceType,
            start: start,
            end: end
        })
    })
        .then(apiJson)
        .then(function(data) {
            btn.disabled = false;
            btn.textContent = '确认补录';
            if (data.error) {
                showToast(data.error);
                return;
            }
            // v1.1.51: 先记住当前文件名（close 会清空），补录后页面停留在该行
            var savedFilename = currentManualFillFilename;
            closeManualFill();
            showToast('补录成功，统计结果已更新');
            renderResult(data, { scrollToFilename: savedFilename });
        })
        .catch(function(err) {
            btn.disabled = false;
            btn.textContent = '确认补录';
            showToast('补录失败: ' + err.message);
        });
}

// ===== v1.1.43: 时间段编辑弹窗（修改/新增/清除） =====
var currentPeriodEditPerson = null;

function openPeriodEdit(ps) {
    currentPeriodEditPerson = { name: ps.name, idcard: ps.idcard || '' };
    document.getElementById('pePersonTip').textContent = '人员：' + ps.name +
        (ps.idcard ? '（' + ps.idcard + '）' : '');

    // 四险各一行，预填当前生效值
    var rowsHtml = '';
    for (var t = 0; t < INSURANCE_ORDER.length; t++) {
        var insName = INSURANCE_ORDER[t];
        var cur = ps.insurances && ps.insurances[insName];
        var sVal = cur ? cur.start : '';
        var eVal = cur ? cur.end : '';
        rowsHtml += '<div class="form-row form-row-period">';
        rowsHtml += '<label class="form-label form-label-fixed">' + insName + '</label>';
        rowsHtml += '<input type="text" class="form-input form-input-ym" id="peStart_' + t + '" value="' + esc(sVal) + '" placeholder="起始 YYYY-MM" maxlength="7">';
        rowsHtml += '<span class="form-sep">至</span>';
        rowsHtml += '<input type="text" class="form-input form-input-ym" id="peEnd_' + t + '" value="' + esc(eVal) + '" placeholder="截止 YYYY-MM" maxlength="7">';
        rowsHtml += '</div>';
    }
    document.getElementById('periodEditRows').innerHTML = rowsHtml;
    document.getElementById('btnPeSubmit').disabled = false;
    document.getElementById('periodEditModal').style.display = 'flex';
}

function closePeriodEdit() {
    document.getElementById('periodEditModal').style.display = 'none';
    currentPeriodEditPerson = null;
}

function submitPeriodEdit() {
    if (!currentTaskId || !currentPeriodEditPerson) return;
    var ymRe = /^\d{4}-(0[1-9]|1[0-2])$/;
    var periods = [];
    for (var t = 0; t < INSURANCE_ORDER.length; t++) {
        var insName = INSURANCE_ORDER[t];
        var s = document.getElementById('peStart_' + t).value.trim();
        var e = document.getElementById('peEnd_' + t).value.trim();
        if (s || e) {
            // 有填写：必须同时合法
            if (!ymRe.test(s) || !ymRe.test(e)) {
                showToast(insName + ': 起止年月格式应为 YYYY-MM，如 2023-01');
                return;
            }
            if (s > e) {
                showToast(insName + ': 起始年月不能晚于截止年月');
                return;
            }
            periods.push({ insurance_type: insName, start: s, end: e });
        } else {
            // 双空：清除覆盖，恢复OCR识别值（无覆盖时为无变化）
            periods.push({ insurance_type: insName, start: null, end: null });
        }
    }

    var btn = document.getElementById('btnPeSubmit');
    btn.disabled = true;
    btn.textContent = '保存中...';
    fetch('/insurance/api/update_period/' + currentTaskId, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            name: currentPeriodEditPerson.name,
            idcard: currentPeriodEditPerson.idcard,
            periods: periods
        })
    })
        .then(apiJson)
        .then(function(data) {
            btn.disabled = false;
            btn.textContent = '保存修改';
            if (data.error) {
                showToast(data.error);
                return;
            }
            closePeriodEdit();
            showToast('时间段已保存，统计结果已更新');
            renderResult(data);
        })
        .catch(function(err) {
            btn.disabled = false;
            btn.textContent = '保存修改';
            showToast('保存失败: ' + err.message);
        });
}

// ===== Toast提示 =====
function showToast(msg) {
    toast.textContent = msg;
    toast.style.display = 'block';
    setTimeout(function() {
        toast.style.display = 'none';
    }, 3500);
}

// ===== HTML转义 =====
function esc(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// ===== v1.1.45: fetch响应统一防御（登录失效返回JSON 401 / 意外HTML响应兜底） =====
var _loginRedirecting = false;
function apiJson(res) {
    if (res.status === 401) {
        if (!_loginRedirecting) {
            _loginRedirecting = true;
            showToast('登录已失效，即将跳转登录页...');
            setTimeout(function() { window.location.href = '/login'; }, 1500);
        }
        return new Promise(function(resolve) { resolve({error: '登录已失效，请重新登录', need_login: true}); });
    }
    var ct = res.headers.get('content-type') || '';
    if (ct.indexOf('json') < 0) {
        // 意外收到HTML（如会话过期被重定向），避免 JSON 解析报错
        showToast('登录状态异常，请刷新页面重新登录');
        return new Promise(function(resolve) { resolve({error: '响应格式异常，请重新登录后重试'}); });
    }
    return res.json();
}

// ===== v1.1.45: 可折叠面板（花名册/文件整理结果） =====
function togglePanel(headerEl) {
    var panel = headerEl.closest('.collapsible-panel');
    if (!panel) return;
    var collapsed = panel.classList.toggle('collapsed');
    var toggle = headerEl.querySelector('.panel-toggle');
    if (toggle) toggle.textContent = collapsed ? '展开 ▸' : '收起 ▾';
}
(function bindRosterPanel() {
    var header = document.getElementById('rosterPreviewHeader');
    if (header) {
        header.addEventListener('click', function() { togglePanel(header); });
    }
})();

// ===== v1.1.45: 图片预览弹窗（双击识别详情行打开） =====
var currentImagePreviewFilename = '';
function imagePreviewUrl(filename) {
    return '/insurance/api/image_preview/' + currentTaskId + '?filename=' + encodeURIComponent(filename);
}

function openImagePreview(filename) {
    if (!filename) return;
    currentImagePreviewFilename = filename;
    var img = document.getElementById('imagePreviewImg');
    var frame = document.getElementById('imagePreviewFrame');
    var tip = document.getElementById('imagePreviewTip');
    document.getElementById('imagePreviewTitle').textContent = filename;
    img.style.display = 'none';
    frame.style.display = 'none';
    tip.style.display = 'none';
    tip.textContent = '';
    document.getElementById('imagePreviewModal').style.display = 'flex';
    img.onload = function() { img.style.display = 'block'; };
    img.onerror = function() {
        // 非图片（如PDF）时尝试iframe嵌入展示
        img.style.display = 'none';
        frame.src = imagePreviewUrl(filename);
        frame.style.display = 'block';
        // iframe也无法展示时给出提示
        setTimeout(function() {
            try {
                if (frame.contentDocument && frame.contentDocument.body &&
                    frame.contentDocument.body.innerHTML.indexOf('error') >= 0) {
                    frame.style.display = 'none';
                    tip.textContent = '该文件暂不支持在线预览';
                    tip.style.display = 'block';
                }
            } catch (e) { /* 跨域忽略 */ }
        }, 800);
    };
    img.src = imagePreviewUrl(filename);
}

function closeImagePreview() {
    var img = document.getElementById('imagePreviewImg');
    var frame = document.getElementById('imagePreviewFrame');
    img.src = '';
    frame.src = 'about:blank';
    document.getElementById('imagePreviewModal').style.display = 'none';
    currentImagePreviewFilename = '';
}

(function bindImagePreviewOverlay() {
    var overlay = document.getElementById('imagePreviewModal');
    if (overlay) {
        overlay.addEventListener('click', function(e) {
            if (e.target === overlay) closeImagePreview();
        });
    }
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            if (document.getElementById('imagePreviewModal').style.display !== 'none') closeImagePreview();
        }
    });
})();

// ===== v1.1.45: 异常图片处理弹窗（手动命名 + 编辑异常信息 + 归入正常列表） =====
var currentEhFilename = '';

function openExcludedHandle(det) {
    currentEhFilename = det.filename;
    document.getElementById('ehFileTip').textContent = '文件：' + det.filename +
        (det.error ? '（异常原因: ' + det.error + '）' : '');
    document.getElementById('ehNewName').value = det.manual_name || '';
    document.getElementById('ehRemark').value = det.remark ||
        (det.error ? det.error : '');
    document.getElementById('ehName').value = det.name || '';
    document.getElementById('ehIdcard').value = det.idcard || '';
    var sel = document.getElementById('ehInsurance');
    sel.value = INSURANCE_ORDER.indexOf(det.insurance_type) >= 0 ? det.insurance_type : '养老保险';
    // 识别到的时间段预填（兼容数组格式与字符串格式，统一为 YYYY-MM）
    var periodStr = '';
    if (Array.isArray(det.period) && det.period.length >= 2) {
        periodStr = det.period[0] + ' ~ ' + det.period[1];
    } else if (det.period) {
        periodStr = String(det.period);
    }
    var pm = periodStr.match(/(\d{4}-\d{2})\s*[~至\-～]\s*(\d{4}-\d{2})/);
    document.getElementById('ehStart').value = pm ? pm[1] : '';
    document.getElementById('ehEnd').value = pm ? pm[2] : '';
    document.getElementById('btnEhSubmit').disabled = false;
    document.getElementById('btnEhSubmit').textContent = '保存并归入正常列表';
    document.getElementById('excludedHandleModal').style.display = 'flex';
    // 加载预览图
    var pimg = document.getElementById('ehPreviewImg');
    var phint = document.getElementById('ehPreviewHint');
    pimg.style.display = 'none';
    phint.style.display = 'block';
    phint.textContent = '（预览加载中…）';
    pimg.onload = function() {
        pimg.style.display = 'block';
        phint.style.display = 'none';
    };
    pimg.onerror = function() {
        pimg.style.display = 'none';
        phint.textContent = '（该文件暂不支持预览，可双击列表行尝试）';
    };
    pimg.src = imagePreviewUrl(det.filename);
    pimg.onclick = function() { openImagePreview(currentEhFilename); };
}

function closeExcludedHandle() {
    document.getElementById('excludedHandleModal').style.display = 'none';
    document.getElementById('ehPreviewImg').src = '';
    currentEhFilename = '';
}

function submitExcludedHandle() {
    if (!currentTaskId || !currentEhFilename) return;
    var name = document.getElementById('ehName').value.trim();
    var idcard = document.getElementById('ehIdcard').value.trim();
    var insuranceType = document.getElementById('ehInsurance').value;
    var start = document.getElementById('ehStart').value.trim();
    var end = document.getElementById('ehEnd').value.trim();
    var newName = document.getElementById('ehNewName').value.trim();
    var remark = document.getElementById('ehRemark').value.trim();

    if (!name) { showToast('请输入姓名'); return; }
    if (idcard && !/^\d{15}$|^\d{17}[\dXx]$/.test(idcard)) {
        showToast('身份证号格式不正确（15位或18位）');
        return;
    }
    var ymRe = /^\d{4}-(0[1-9]|1[0-2])$/;
    if (start || end) {
        if (!ymRe.test(start) || !ymRe.test(end)) {
            showToast('起止年月格式应为 YYYY-MM，如 2023-01（也可留空沿用识别值）');
            return;
        }
        if (start > end) {
            showToast('起始年月不能晚于截止年月');
            return;
        }
    }

    var btn = document.getElementById('btnEhSubmit');
    btn.disabled = true;
    btn.textContent = '保存中...';
    fetch('/insurance/api/update_excluded_image/' + currentTaskId, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            filename: currentEhFilename,
            name: name,
            idcard: idcard,
            insurance_type: insuranceType,
            start: start,
            end: end,
            new_name: newName,
            remark: remark
        })
    })
        .then(apiJson)
        .then(function(data) {
            btn.disabled = false;
            btn.textContent = '保存并归入正常列表';
            if (data.error) {
                showToast(data.error);
                return;
            }
            // v1.1.51: 先记住当前文件名（close 会清空），保存后页面停留在该行
            var savedFilename = currentEhFilename;
            closeExcludedHandle();
            showToast('已保存：图片已归入正常列表并从异常列表移除');
            renderResult(data, { scrollToFilename: savedFilename });
        })
        .catch(function(err) {
            btn.disabled = false;
            btn.textContent = '保存并归入正常列表';
            showToast('保存失败: ' + err.message);
        });
}
