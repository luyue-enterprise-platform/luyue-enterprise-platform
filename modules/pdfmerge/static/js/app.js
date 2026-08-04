// ===== 全局状态 =====
var currentMode = 'refund';
var folderPath = '';
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
    if (folderPath) {
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

// ===== 选择文件夹 =====
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
            folderPath = data.folder_path;
            document.getElementById('folderSelectBox').classList.add('hidden');
            var sel = document.getElementById('folderSelected');
            sel.classList.remove('hidden');
            document.getElementById('folderPath').textContent = data.folder_path;
            document.getElementById('folderCount').textContent = '共 ' + data.file_count + ' 个文件';

            // 自动扫描匹配
            scanMatch();
        })
        .catch(function(err) {
            alert('选择文件夹失败: ' + err.message);
        });
}

// ===== 扫描匹配 =====
function scanMatch() {
    if (!folderPath) return;

    document.getElementById('btnGenerate').disabled = true;

    fetch('/pdfmerge/api/scan_match', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            folder_path: folderPath,
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
            filesText = sec.file_count + ' 个文件: ' + sec.files.join(', ').substring(0, 80);
            if (sec.files.join(', ').length > 80) filesText += '...';
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
            data.unmatched_files.join(', ').substring(0, 200);
        if (data.unmatched_files.join(', ').length > 200) {
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
    if (!folderPath) {
        alert('请先选择资料文件夹');
        return;
    }

    var payload = {
        folder_path: folderPath,
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
