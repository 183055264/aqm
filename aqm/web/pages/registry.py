"""Registry page — search, pull (with version), publish pipelines."""

from __future__ import annotations

from aqm.web.templates import layout


def render_registry() -> str:
    body = """\
<h1>流水线注册表</h1>

<div class="card">
  <h3>搜索流水线</h3>
  <div style="display:flex;gap:8px;margin-top:12px;align-items:flex-end;">
    <div class="form-group" style="flex:1;margin-bottom:0;">
      <label for="searchQuery">关键词</label>
      <input id="searchQuery" type="text" placeholder="例如：代码审查、内容、数据..."
             onkeydown="if(event.key==='Enter')searchRegistry()">
    </div>
    <button class="btn btn-primary" onclick="searchRegistry()" style="height:38px;">搜索</button>
  </div>
  <div style="margin-top:8px;">
    <label style="font-size:13px;color:var(--text-dim);cursor:pointer;">
      <input type="checkbox" id="offlineMode" checked style="margin-right:4px;"> 仅离线 (本地注册表，不访问 GitHub)
    </label>
  </div>
</div>

<div id="searchResults" style="margin-top:16px;">
  <p style="text-align:center;color:var(--text-dim);padding:40px 0;">输入关键词后点击「搜索」</p>
</div>

<div class="card" style="margin-top:24px;">
  <h3>发布流水线</h3>
  <p style="font-size:13px;color:var(--text-dim);margin-bottom:12px;">
    将项目中的流水线发布到注册表，使用语义化版本管理。
  </p>
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;">
    <div class="form-group">
      <label for="pubPipeline">来源流水线</label>
      <select id="pubPipeline" style="width:100%;">
        <option value="">(默认流水线)</option>
      </select>
    </div>
    <div class="form-group">
      <label for="pubName">发布名称</label>
      <input id="pubName" type="text" placeholder="我的流水线">
    </div>
    <div class="form-group">
      <label for="pubVersion">版本 (可选)</label>
      <input id="pubVersion" type="text" placeholder="自动递增">
    </div>
  </div>
  <div class="form-group" style="margin-top:4px;">
    <label for="pubDesc">描述</label>
    <input id="pubDesc" type="text" placeholder="这个流水线做什么...">
  </div>
  <button class="btn btn-primary" onclick="publishPipeline(false)">发布到 GitHub</button>
  <button class="btn" onclick="publishPipeline(true)" style="margin-left:8px;">保存到本地</button>
  <div id="publishResult" style="margin-top:12px;font-size:13px;"></div>
</div>

<script>
async function searchRegistry() {
  const query = document.getElementById('searchQuery').value.trim();
  const offline = document.getElementById('offlineMode').checked;
  const params = new URLSearchParams();
  if (query) params.set('query', query);
  if (offline) params.set('offline', 'true');

  const box = document.getElementById('searchResults');
  box.innerHTML =
    '<div style="text-align:center;color:var(--text-dim);padding:24px;">搜索中...</div>';

  try {
    const data = await apiFetch('/api/registry/search?' + params.toString());
    if (!data.length) {
      let title = '未找到流水线';
      let desc = '试试其他关键词。';
      if (offline) {
        desc = '本地注册表没有匹配项。本地注册表为空时请取消勾选「仅离线」再搜索 GitHub（国内网络较慢），或先发布一条流水线到本地。';
      } else if (query) {
        desc = 'GitHub 注册表中没有匹配项。可尝试中文词（代码 / 数据 / 内容 / 设计 / 客服）或英文词（code / data / content / design）。';
      } else {
        desc = '注册表索引为空，或当前网络无法访问 GitHub。';
      }
      box.innerHTML =
        '<div class="empty-state"><div class="title">' + title + '</div><div class="desc">' + desc + '</div></div>';
      return;
    }
    let html = `<div class="table-wrap"><table>
      <thead><tr><th>名称</th><th>来源</th><th>版本</th><th>描述</th><th>代理数</th><th>操作</th></tr></thead>
      <tbody>`;
    data.forEach(p => {
      const sourceColors = {github:'var(--purple)', bundled:'var(--accent)', local:'var(--green)'};
      const sourceColor = sourceColors[p.source] || 'var(--text-dim)';
      const latestVersion = p.latest || p.version || '-';
      const versions = p.versions || [];

      // Version selector + pull button
      let pullHtml;
      if (versions.length > 1) {
        const options = versions.map(v =>
          `<option value="${v}"${v === p.latest ? ' selected' : ''}>${v}</option>`
        ).join('');
        pullHtml = `
          <div style="display:flex;gap:4px;align-items:center;">
            <select id="ver-${p.name}" style="padding:4px 6px;font-size:12px;border:1px solid var(--border);
              border-radius:4px;background:var(--surface2);color:var(--text);width:80px;">
              ${options}
            </select>
            <button class="btn btn-sm btn-primary" onclick="pullPipeline('${p.name}',
              document.getElementById('ver-${p.name}').value)">拉取</button>
          </div>`;
      } else {
        pullHtml = `<button class="btn btn-sm btn-primary" onclick="pullPipeline('${p.name}','${latestVersion}')">拉取</button>`;
      }

      html += `<tr>
        <td><strong>${p.name}</strong></td>
        <td><span style="color:${sourceColor};font-size:12px;">${p.source}</span></td>
        <td><span style="font-family:monospace;font-size:12px;">${latestVersion}</span></td>
        <td style="max-width:250px;font-size:13px;">${p.description || '<span style="color:var(--text-dim)">-</span>'}</td>
        <td>${p.agents_count || '-'}</td>
        <td>${pullHtml}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
    box.innerHTML = html;
  } catch(e) {
    box.innerHTML =
      '<div class="empty-state"><div class="title">搜索失败</div><div class="desc">' + e.message + '</div></div>';
  }
}

async function pullPipeline(name, version) {
  const label = version && version !== '-' ? name + '@' + version : name;
  if (!confirm('拉取 "' + label + '" 到您的项目?')) return;
  const offline = document.getElementById('offlineMode').checked;
  const body = {pipeline_name: name, offline: offline};
  if (version && version !== '-') body.version = version;
  try {
    const data = await apiFetch('/api/registry/pull', {method:'POST', body:JSON.stringify(body)});
    const vLabel = data.version ? ' v' + data.version : '';
    showToast('已拉取 ' + name + vLabel + ' (' + data.agents_count + ' 个代理)');
  } catch(e) {}
}

async function loadPublishPipelines() {
  const sel = document.getElementById('pubPipeline');
  if (!sel) return;
  try {
    const res = await fetch('/api/pipelines');
    const data = await res.json();
    const list = data.pipelines || [];
    sel.innerHTML = '<option value="">(默认流水线: ' + (data.default || 'default') + ')</option>' +
      list.map(p => '<option value="' + p.name + '">' + p.name +
        (p.is_default ? '（默认）' : '') + '</option>').join('');
  } catch(e) {}
}

async function publishPipeline(localOnly) {
  const name = document.getElementById('pubName').value.trim();
  const desc = document.getElementById('pubDesc').value.trim();
  const version = document.getElementById('pubVersion').value.trim();
  const pipeline = document.getElementById('pubPipeline').value;
  const out = document.getElementById('publishResult');
  if (!name) { showToast('流水线名称必填', 'error'); return; }
  const body = {name, description: desc, local_only: localOnly};
  if (version) body.version = version;
  if (pipeline) body.pipeline = pipeline;
  out.textContent = localOnly ? '保存中...' : '发布中（首次会 fork/clone 注册表，可能较慢）...';
  try {
    const data = await apiFetch('/api/registry/publish', {method:'POST', body:JSON.stringify(body)});
    const vLabel = data.version ? ' v' + data.version : '';
    if (data.github_error) {
      out.innerHTML = '<span style="color:#e6a817;">已保存到本地 ' + name + vLabel +
        '；GitHub 发布失败：' + data.github_error + '</span>';
      showToast('已保存到本地，GitHub 发布失败', 'error');
    } else if (data.pr_url) {
      out.innerHTML = '已创建 PR：<a href="' + data.pr_url + '" target="_blank" rel="noopener">' + data.pr_url + '</a>';
      showToast('已创建 PR: ' + name + vLabel);
    } else {
      out.textContent = '已保存 ' + name + vLabel + ' 到本地注册表（' + data.location + '）';
      showToast('已发布 ' + name + vLabel + ' 到本地');
    }
    loadPublishPipelines();
  } catch(e) {
    out.innerHTML = '<span style="color:var(--red);">发布失败：' + e.message + '</span>';
  }
}

// Search on load (offline by default), and populate the publish dropdown.
window.addEventListener('DOMContentLoaded', () => {
  searchRegistry();
  loadPublishPipelines();
});
</script>"""

    return layout("注册表", body, active="registry")
