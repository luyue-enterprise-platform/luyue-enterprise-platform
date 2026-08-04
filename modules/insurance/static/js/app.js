/* ===== 鲁岳企业服务.重点群体社保批量统计智能核算系统 - 前端交互 ===== */

// 四险标准顺序
var INSURANCE_ORDER = ['养老保险', '医疗保险', '工伤保险', '失业保险'];

// 当前选中的文件
var selectedFiles = [];
var currentTaskId = null;
var pollTimer = null;
// 花名册数据
var rosterData = [];
// 花名册公司名和源文件路径（用于缴费单位验证）
var rosterCompany = '';
var rosterSourcePath = '';

// ===== DOM元素 =====
var dropzone = document.getElementById('dropzone');
var fileInput = document.getElementById('fileInput');
var fileInputSingle = document.getElementById('fileInputSingle');
var selectFilesLink = document.getElementById('selectFilesLink');
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

// 修改密码
var btnRefresh = document.getElementById('btnRefresh');

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
        .then(function(res) { return res.json(); })
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
    fileInput.click();
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

selectFilesLink.addEventListener('click', function(e) {
    e.stopPropagation();
    fileInputSingle.click();
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
    selectedFiles = valid;
    renderFileList();
}

function renderFileList() {
    fileCount.textContent = selectedFiles.length;
    fileItems.innerHTML = '';
    for (var i = 0; i < selectedFiles.length; i++) {
        var li = document.createElement('li');
        var sizeKB = (selectedFiles[i].size / 1024).toFixed(1);
        var path = selectedFiles[i]._relativePath || selectedFiles[i].webkitRelativePath || selectedFiles[i].name;
        li.textContent = path + '  (' + sizeKB + ' KB)';
        fileItems.appendChild(li);
    }
    fileList.style.display = 'block';
}

// ===== 清空 =====
btnClear.addEventListener('click', function() {
    selectedFiles = [];
    fileList.style.display = 'none';
    fileInput.value = '';
    fileInputSingle.value = '';
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

// ===== 批量压缩选中文件 =====
async function compressAllFiles(files) {
    var results = [];
    for (var i = 0; i < files.length; i++) {
        results.push(await compressImage(files[i], 1600, 0.85));
    }
    return results;
}

function uploadFiles() {
    btnUpload.disabled = true;
    btnUpload.textContent = '准备上传...';
    navStatus.querySelector('span:last-child').textContent = '准备中';

    // 先压缩图片
    compressAllFiles(selectedFiles).then(function(compressedFiles) {
        // 统计压缩效果
        var totalOrig = 0, totalComp = 0;
        for (var i = 0; i < compressedFiles.length; i++) {
            totalOrig += selectedFiles[i].size;
            totalComp += compressedFiles[i].size;
        }
        var savedPct = totalOrig > 0 ? Math.round((1 - totalComp / totalOrig) * 100) : 0;

        var formData = new FormData();
        for (var i = 0; i < compressedFiles.length; i++) {
            formData.append('files', compressedFiles[i]);
        }
        formData.append('roster', JSON.stringify(rosterData));
        formData.append('roster_company', rosterCompany);
        formData.append('roster_source_path', rosterSourcePath);

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
                progressDetail.textContent = compressedFiles.length + ' 个文件';
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
        .then(function(res) { return res.json(); })
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
        .then(function(res) { return res.json(); })
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
            .then(function(res) { return res.json(); })
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
        .then(function(res) { return res.json(); })
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
function renderResult(data) {
    progressSection.style.display = 'none';
    progressActions.style.display = 'none';
    resultSection.style.display = 'block';

    // 汇总信���
    summaryBar.innerHTML = '';
    var summaries = [
        { label: '识别图片', value: data.ocr_count },
        { label: '参保人员', value: data.person_count },
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

    // 文件整理结果
    var org = data.organize_result;
    if (org) {
        var orgHtml = '<h3>文件整理结果</h3>';
        orgHtml += '<p class="organize-summary">已整理 ' + org.organized_count + ' 个文件';
        if (org.no_roster) {
            orgHtml += '（未上传花名册，按OCR识别姓名命名）';
        }
        orgHtml += '</p>';

        // ===== 每张图片的识别详情（帮助用户定位"险种未识别"等问题） =====
        if (data.image_details && data.image_details.length > 0) {
            orgHtml += '<details class="image-details-block" open>';
            orgHtml += '<summary>📋 每张图片的识别详情（' + data.image_details.length + ' 张）</summary>';
            orgHtml += '<table class="image-details-table">';
            orgHtml += '<thead><tr><th>文件名</th><th>姓名</th><th>身份证</th><th>险种</th><th>时间段</th><th>缴费单位</th><th>状态</th></tr></thead><tbody>';
            for (var di = 0; di < data.image_details.length; di++) {
                var det = data.image_details[di];
                var ins = det.insurance_type || '';
                var period = det.period || '';
                var insCell = ins
                    ? '<span class="tag-ok">' + esc(ins) + '</span>'
                    : '<span class="tag-bad">⚠ 未识别</span>';
                var periodCell = period
                    ? '<span class="tag-ok">' + esc(period) + '</span>'
                    : '<span class="tag-bad">⚠ 未识别</span>';
                var statusCell = '';
                if (det.error) {
                    statusCell = '<span class="tag-bad">识别失败: ' + esc(det.error) + '</span>';
                } else if (!ins) {
                    statusCell = '<span class="tag-bad">险种缺失（未归类）</span>';
                } else if (!period) {
                    statusCell = '<span class="tag-warn">时间段缺失</span>';
                } else {
                    statusCell = '<span class="tag-ok">✓ 正常</span>';
                }
                orgHtml += '<tr>';
                orgHtml += '<td>' + esc(det.filename) + '</td>';
                orgHtml += '<td>' + esc(det.name || '—') + '</td>';
                orgHtml += '<td>' + esc(det.idcard || '—') + '</td>';
                orgHtml += '<td>' + insCell + '</td>';
                orgHtml += '<td>' + periodCell + '</td>';
                orgHtml += '<td>' + esc(det.company_name || '—') + '</td>';
                orgHtml += '<td>' + statusCell + '</td>';
                orgHtml += '</tr>';
            }
            orgHtml += '</tbody></table>';
            orgHtml += '</details>';
        }

        orgHtml += '<div class="folder-grid">';
        for (var folder in org.folder_structure) {
            var files = org.folder_structure[folder];
            orgHtml += '<div class="folder-card">';
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
        document.getElementById('organizeInfo').innerHTML = orgHtml;
        document.getElementById('organizeInfo').style.display = 'block';
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

    // 表格
    var html = '<thead><tr>';
    var headers = [
        '序号',
        '姓名', '身份证号', '人员身份类型',
        '退役证编号/就业创业证编号', '退役时间/登记失业时间',
        '养老保险参保证明时间段', '医疗保险参保证明时间段',
        '工伤保险参保证明时间段', '失业保险参保证明时间段',
        '参保证明时间段（养老+医疗+工伤+失业）', '申请退税总月数'
    ];
    for (var j = 0; j < data.year_cols.length; j++) {
        headers.push(data.year_cols[j] + '年申请退税月数');
    }
    headers.push('合计申请退税总额');
    for (var h = 0; h < headers.length; h++) {
        html += '<th>' + headers[h] + '</th>';
    }
    html += '</tr></thead><tbody>';

    for (var p = 0; p < data.person_stats.length; p++) {
        var ps = data.person_stats[p];
        html += '<tr>';
        html += '<td>' + (p + 1) + '</td>';
        html += '<td>' + esc(ps.name) + '</td>';
        html += '<td>' + esc(ps.idcard) + '</td>';
        html += '<td>' + esc(ps.identity_type || '') + '</td>'; // 人员身份类型 - 来自花名册
        html += '<td></td>'; // 退役证编号/就业创业证编号 - 待填
        html += '<td></td>'; // 退役时间/登记失业时间 - 待填

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

        for (var y = 0; y < data.year_cols.length; y++) {
            var m = ps.yearly_months[data.year_cols[y]] || 0;
            html += '<td>' + m + '</td>';
        }

        html += '<td></td>'; // 合计申请退税总额 - 待填

        html += '</tr>';
    }
    html += '</tbody>';
    resultTable.innerHTML = html;

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
        .then(function(res) { return res.json(); })
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
    fileInput.value = '';
    fileInputSingle.value = '';
    currentTaskId = null;
    document.getElementById('organizeInfo').style.display = 'none';
    document.getElementById('yearlyLedgerInfo').style.display = 'none';
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
