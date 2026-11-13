"""Validate page — JSON Schema validation for any project pipeline."""

from __future__ import annotations

from aqm.web.templates import layout


def render_validate() -> str:
    body = """\
<h1>校验流水线</h1>

<div class="card">
  <p style="font-size:14px;color:var(--text-dim);margin-bottom:12px;">
    根据 JSON Schema 规范校验流水线 YAML；可切换任意流水线，默认选中当前默认流水线。
  </p>
  <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;">
    <div class="form-group" style="flex:1;min-width:220px;margin-bottom:0;">
      <label for="pipelineSelect">流水线</label>
      <select id="pipelineSelect" style="width:100%;" onchange="runValidation()">
        <option value="">加载中...</option>
      </select>
    </div>
    <button class="btn" onclick="loadPipelines()" style="height:38px;">刷新列表</button>
    <label style="font-size:14px;cursor:pointer;height:38px;display:flex;align-items:center;">
      <input type="checkbox" id="strictMode" style="margin-right:4px;"> 严格模式 (警告视为错误)
    </label>
    <button class="btn btn-primary" onclick="runValidation()" style="height:38px;">立即校验</button>
  </div>
  <div id="pipelineHint" style="margin-top:8px;font-size:12px;color:var(--text-dim);"></div>
</div>

<div id="validationResult" style="margin-top:16px;"></div>

<script>
async function loadPipelines() {
  const sel = document.getElementById('pipelineSelect');
  const hint = document.getElementById('pipelineHint');
  try {
    const res = await fetch('/api/validate/pipelines');
    const data = await res.json();
    const list = data.pipelines || [];
    if (!list.length) {
      sel.innerHTML = '<option value="">(没有可用流水线)</option>';
      hint.textContent = '项目下没有 .aqm/pipelines/*.yaml，请先在「流水线」页面新建。';
      return;
    }
    const prev = sel.value;
    sel.innerHTML = list.map(p =>
      '<option value="' + p.name + '"' +
      (p.is_default ? ' data-default="1"' : '') + '>' +
      p.name + (p.is_default ? '（默认）' : '') +
      '</option>'
    ).join('');
    const keep = prev && list.some(p => p.name === prev);
    if (!keep) {
      const def = list.find(p => p.is_default) || list[0];
      sel.value = def.name;
    }
    const chosen = list.find(p => p.name === sel.value);
    hint.textContent = chosen && chosen.path ? '文件：' + chosen.path : '';
    await runValidation();
  } catch (e) {
    sel.innerHTML = '<option value="">(加载失败)</option>';
    hint.textContent = '获取流水线列表失败：' + e.message;
  }
}

async function runValidation() {
  const box = document.getElementById('validationResult');
  const sel = document.getElementById('pipelineSelect');
  const pipeline = sel ? sel.value : '';
  const hint = document.getElementById('pipelineHint');
  box.innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:24px;">校验中...</div>';
  const strict = document.getElementById('strictMode').checked;
  try {
    const res = await fetch('/api/validate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({strict: strict, pipeline: pipeline || null})
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || '请求失败');
    if (data.source_path && hint) hint.textContent = '文件：' + data.source_path;
    let html = '';

    html += '<div style="font-size:13px;color:var(--text-dim);margin-bottom:8px;">已校验：<strong style="color:var(--text);">' +
      (data.pipeline || '(未命名)') + '</strong></div>';

    if (data.valid) {
      html += '<div class="card" style="border-color:var(--green);">';
      html += '<h3 style="color:var(--green);">&#10003; 有效</h3>';
      html += '<div style="margin-top:8px;">';
      if (data.summary.agent_count !== undefined) html += '<div>代理数: <strong>' + data.summary.agent_count + '</strong></div>';
      if (data.summary.features && data.summary.features.length) html += '<div>特性: ' + data.summary.features.join(', ') + '</div>';
      html += '</div></div>';
    } else {
      html += '<div class="card" style="border-color:var(--red);">';
      html += '<h3 style="color:var(--red);">&#10007; 发现 ' + data.errors.length + ' 个错误</h3>';
      html += '<div style="margin-top:12px;">';
      data.errors.forEach((err, i) => {
        html += '<div style="margin-bottom:12px;padding:12px;background:var(--surface2);border-radius:6px;">';
        html += '<div><strong style="color:var(--red);">' + (i+1) + '.</strong> <code>' + err.path + '</code></div>';
        html += '<div style="margin-top:4px;">' + err.message + '</div>';
        if (err.fix) html += '<div style="margin-top:4px;color:var(--text-dim);font-size:13px;">修复: ' + err.fix + '</div>';
        html += '</div>';
      });
      html += '</div></div>';
    }

    // Warnings section
    if (data.warnings && data.warnings.length) {
      const warnColor = strict ? 'var(--red)' : '#e6a817';
      html += '<div class="card" style="margin-top:16px;border-color:' + warnColor + ';">';
      html += '<h3 style="color:' + warnColor + ';">&#9888; ' + data.warnings.length + ' 个警告</h3>';
      html += '<div style="margin-top:12px;">';
      data.warnings.forEach((w, i) => {
        html += '<div style="margin-bottom:12px;padding:12px;background:var(--surface2);border-radius:6px;">';
        html += '<div><strong style="color:' + warnColor + ';">' + (i+1) + '.</strong> <code>' + w.path + '</code></div>';
        html += '<div style="margin-top:4px;">' + w.message + '</div>';
        if (w.fix) html += '<div style="margin-top:4px;color:var(--text-dim);font-size:13px;">修复: ' + w.fix + '</div>';
        html += '</div>';
      });
      html += '</div></div>';
    }

    if (data.yaml_content) {
      html += '<div class="card" style="margin-top:16px;"><details>';
      html += '<summary style="font-size:15px;font-weight:600;color:var(--text);">原始 YAML</summary>';
      html += '<pre style="margin-top:12px;">' + data.yaml_content.replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</pre>';
      html += '</details></div>';
    }

    box.innerHTML = html;
  } catch(e) {
    box.innerHTML = '<div class="card" style="border-color:var(--red);"><h3 style="color:var(--red);">校验失败</h3><p>' + e.message + '</p></div>';
  }
}

// Load the pipeline list (and validate the default one) on page open.
window.addEventListener('DOMContentLoaded', () => setTimeout(loadPipelines, 50));
</script>"""

    return layout("校验", body, active="validate")
