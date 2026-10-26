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
      <input type="checkbox" id="offlineMode" checked style="margin-right:4px;"> 仅离线 (本地 + 内置)
    </label>
  </div>
</div>

<div id="searchResults" style="margin-top:16px;">
  <p style="text-align:center;color:var(--text-dim);padding:40px 0;">输入关键词后点击「搜索」</p>
</div>

<div class="card" style="margin-top:24px;">
  <h3>发布当前流水线</h3>
  <p style="font-size:13px;color:var(--text-dim);margin-bottom:12px;">
    将流水线发布到注册表，使用语义化版本管理。
  </p>
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;">
    <div class="form-group">
      <label for="pubName">流水线名称</label>
      <input id="pubName" type="text" placeholder="my-pipeline">
    </div>
    <div class="form-group">
      <label for="pubDesc">描述</label>
      <input id="pubDesc" type="text" placeholder="这个流水线做什么...">
    </div>
    <div class="form-group">
      <label for="pubVersion">版本 (可选)</label>
      <input id="pubVersion" type="text" placeholder="auto-increment">
    </div>
  </div>
  <button class="btn btn-primary" onclick="publishPipeline(false)">发布到 GitHub</button>
  <button class="btn" onclick="publishPipeline(true)" style="margin-left:8px;">保存到本地</button>
</div>

<script>
async function searchRegistry() {
  const query = document.getElementById('searchQuery').value.trim();
  const offline = document.getElementById('offlineMode').checked;
  const params = new URLSearchParams();
  if (query) params.set('query', query);
  if (offline) params.set('offline', 'true');

    document.getElementById('searchResults').innerHTML =
      '<div style="text-align:center;color:var(--text-dim);padding:24px;">搜索中...</div>';

  try {
    const data = await apiFetch('/api/registry/search?' + params.toString());
    if (!data.length) {
      document.getElementById('searchResults').innerHTML =
        '<div class="empty-state"><div class="title">未找到流水线</div><div class="desc">试试其他关键词或检查注册表。</div></div>';
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
    document.getElementById('searchResults').innerHTML = html;
  } catch(e) {
    document.getElementById('searchResults').innerHTML =
      '<div class="empty-state"><div class="title">搜索失败</div></div>';
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

async function publishPipeline(localOnly) {
  const name = document.getElementById('pubName').value.trim();
  const desc = document.getElementById('pubDesc').value.trim();
  const version = document.getElementById('pubVersion').value.trim();
  if (!name) { showToast('流水线名称必填', 'error'); return; }
  const body = {name, description: desc, local_only: localOnly};
  if (version) body.version = version;
  try {
    const data = await apiFetch('/api/registry/publish', {method:'POST', body:JSON.stringify(body)});
    const vLabel = data.version ? ' v' + data.version : '';
    if (data.pr_url) {
      showToast('已创建 PR: ' + name + vLabel);
    } else {
      showToast('已发布 ' + name + vLabel + ' 到本地');
    }
  } catch(e) {}
}

// No auto-search — user triggers search manually
</script>"""

    return layout("注册表", body, active="registry")
