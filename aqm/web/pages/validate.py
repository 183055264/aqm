"""Validate page — agents.yaml validation with error display."""

from __future__ import annotations

from aqm.web.templates import layout


def render_validate() -> str:
    body = """\
<h1>校验流水线</h1>

<div class="card">
  <p style="font-size:14px;color:var(--text-dim);margin-bottom:12px;">
    根据 JSON Schema 规范校验您的 .aqm/agents.yaml。
  </p>
  <label style="margin-left:16px;font-size:14px;cursor:pointer;">
    <input type="checkbox" id="strictMode" style="margin-right:4px;"> 严格模式 (警告视为错误)
  </label>
  <button class="btn btn-primary" onclick="runValidation()">立即校验</button>
</div>

<div id="validationResult" style="margin-top:16px;"></div>

<script>
async function runValidation() {
  document.getElementById('validationResult').innerHTML = '<div style="text-align:center;color:var(--text-dim);padding:24px;">校验中...</div>';
  const strict = document.getElementById('strictMode').checked;
  try {
    const data = await apiFetch('/api/validate', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({strict: strict})});
    let html = '';

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

    document.getElementById('validationResult').innerHTML = html;
  } catch(e) {
    document.getElementById('validationResult').innerHTML = '<div class="card" style="border-color:var(--red);"><h3 style="color:var(--red);">校验失败</h3><p>' + e.message + '</p></div>';
  }
}

// Auto-validate on load (defer to ensure apiFetch is defined)
window.addEventListener('DOMContentLoaded', () => setTimeout(runValidation, 50));
</script>"""

    return layout("校验", body, active="validate")
