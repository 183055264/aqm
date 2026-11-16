"""Team templates page — manage the global, project-independent member library."""

from __future__ import annotations

from aqm.web.templates import layout


def render_team_templates() -> str:
    body = r"""
<style>
/* The member grid uses bare inputs (no .form-group wrapper), so give them the
   same look and height as the rest of the console. */
.tt-agent-row input, .tt-agent-row select, .tt-agent-row textarea {
  background: var(--surface2); border: 1px solid var(--border); border-radius: 6px;
  padding: 6px 8px; color: var(--text); font-size: 12px; font-family: inherit;
}
.tt-agent-row input:focus, .tt-agent-row select:focus, .tt-agent-row textarea:focus {
  border-color: var(--accent); outline: none;
}
.tt-agent-row input, .tt-agent-row select { height: 34px; box-sizing: border-box; }
</style>
<h1>团队模板</h1>

<div class="card">
  <p style="font-size:14px;color:var(--text-dim);margin-bottom:8px;">
    团队模板存放在软件安装目录的 <code id="ttRoot">templates/</code> 下，与项目目录无关；
    任何新项目的会议、流水线都可以直接调用。
  </p>
  <p style="font-size:13px;color:var(--text-dim);">
    模板保存成员（id / 运行时 / 角色 / 提示词）。会议或任务一旦创建，就会把自己的成员配置固化成快照，
    之后修改模板不会影响已经创建好的记录。提示词会原样带入，留空表示使用运行时自己的默认提示词。
  </p>
</div>

<div class="card" style="margin-top:16px;">
  <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
    <h3 style="margin:0;">模板列表</h3>
    <div style="display:flex;gap:8px;">
      <button class="btn" onclick="loadTemplates()">刷新</button>
      <button class="btn btn-primary" onclick="startNewTemplate()">+ 新建模板</button>
    </div>
  </div>
  <div id="ttList" style="margin-top:12px;">
    <div class="empty-state" style="padding:24px;">加载中…</div>
  </div>
</div>

<div class="card" id="ttEditor" style="display:none;margin-top:16px;border-color:var(--accent);">
  <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
    <h3 style="margin:0;" id="ttEditorTitle">新建模板</h3>
    <div style="display:flex;gap:8px;">
      <button class="btn btn-sm btn-primary" onclick="saveTemplate()">保存</button>
      <button class="btn btn-sm" onclick="closeTemplateEditor()">取消</button>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 2fr;gap:12px;margin-top:12px;">
    <div class="form-group" style="margin-bottom:0;">
      <label>模板名 *</label>
      <input id="ttName" type="text" placeholder="例如 my-team" style="width:100%;">
      <div style="font-size:11px;color:var(--text-dim);margin-top:4px;">字母、数字、点、下划线、短横线；编辑时不可改。</div>
    </div>
    <div class="form-group" style="margin-bottom:0;">
      <label>说明</label>
      <input id="ttDesc" type="text" placeholder="这套团队适合做什么" style="width:100%;">
    </div>
  </div>

  <div style="margin-top:16px;">
    <label style="font-weight:600;font-size:13px;color:var(--text-dim);">成员</label>
    <div id="ttAgents" style="margin-top:8px;"></div>
    <button class="btn btn-sm" onclick="addTtAgent()" style="margin-top:8px;">+ 添加成员</button>
    <div style="font-size:12px;color:var(--text-dim);margin-top:6px;">
      模板中的提示词会原样带入会议/流水线；留空表示交给该运行时自己的默认提示词。
    </div>
  </div>
</div>

<script>
const TT_RUNTIMES = ['claude', 'opencode', 'kilo', 'cline', 'gemini', 'codex'];
let TT_EDIT_NAME = null;

function escapeValue(v) {
  return String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function runtimeOptions(selected) {
  return TT_RUNTIMES.map(r =>
    '<option value="' + r + '"' + (r === selected ? ' selected' : '') + '>' + r + '</option>'
  ).join('');
}

function addTtAgent(agent) {
  agent = agent || {};
  const list = document.getElementById('ttAgents');
  const row = document.createElement('div');
  row.className = 'tt-agent-row';
  row.style.cssText = 'display:grid;grid-template-columns:1fr 1fr 1fr 1fr 3fr auto;gap:8px;margin-bottom:8px;align-items:start;';
  row.innerHTML = `
    <input class="tt-agent-id" type="text" placeholder="id" value="${escapeValue(agent.id)}" style="width:100%;">
    <input class="tt-agent-name" type="text" placeholder="名称" value="${escapeValue(agent.name)}" style="width:100%;">
    <select class="tt-agent-runtime" style="width:100%;">${runtimeOptions(agent.runtime || 'claude')}</select>
    <input class="tt-agent-role" type="text" placeholder="角色" value="${escapeValue(agent.role)}" style="width:100%;">
    <textarea class="tt-agent-prompt" rows="2" placeholder="系统提示词（留空 = 运行时默认）" style="width:100%;font-size:12px;">${escapeValue(agent.system_prompt)}</textarea>
    <button type="button" class="btn btn-sm" style="color:var(--red);align-self:center;" onclick="this.closest('.tt-agent-row').remove()">&#10005;</button>
  `;
  list.appendChild(row);
}

function closeTemplateEditor() {
  document.getElementById('ttEditor').style.display = 'none';
  TT_EDIT_NAME = null;
}

function startNewTemplate() {
  TT_EDIT_NAME = null;
  document.getElementById('ttEditorTitle').textContent = '新建模板';
  document.getElementById('ttName').value = '';
  document.getElementById('ttName').disabled = false;
  document.getElementById('ttDesc').value = '';
  document.getElementById('ttAgents').innerHTML = '';
  addTtAgent({ id: 'claude', name: 'Claude', runtime: 'claude', role: '架构师' });
  document.getElementById('ttEditor').style.display = 'block';
  document.getElementById('ttEditor').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function editTemplate(name) {
  try {
    const data = await apiFetch('/api/team-templates/' + encodeURIComponent(name));
    TT_EDIT_NAME = name;
    document.getElementById('ttEditorTitle').textContent = '编辑模板: ' + name;
    const nameInput = document.getElementById('ttName');
    nameInput.value = name;
    nameInput.disabled = true;
    document.getElementById('ttDesc').value = data.description || '';
    document.getElementById('ttAgents').innerHTML = '';
    (data.agents || []).forEach(a => addTtAgent(a));
    document.getElementById('ttEditor').style.display = 'block';
    document.getElementById('ttEditor').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    showToast('加载模板失败: ' + (e.message || e), 'error');
  }
}

function collectAgents() {
  const agents = [];
  document.querySelectorAll('#ttAgents .tt-agent-row').forEach(row => {
    agents.push({
      id: row.querySelector('.tt-agent-id').value.trim(),
      name: row.querySelector('.tt-agent-name').value.trim(),
      runtime: row.querySelector('.tt-agent-runtime').value,
      role: row.querySelector('.tt-agent-role').value.trim(),
      system_prompt: row.querySelector('.tt-agent-prompt').value.trim(),
    });
  });
  return agents;
}

async function saveTemplate() {
  const name = (TT_EDIT_NAME || document.getElementById('ttName').value.trim());
  if (!name) { showToast('请填写模板名', 'error'); return; }
  const agents = collectAgents().filter(a => a.id);
  if (!agents.length) { showToast('请至少添加一个成员', 'error'); return; }
  const payload = {
    name: name,
    description: document.getElementById('ttDesc').value.trim(),
    agents: agents,
  };
  try {
    if (TT_EDIT_NAME) {
      await apiFetch('/api/team-templates/' + encodeURIComponent(TT_EDIT_NAME), {
        method: 'PUT', body: JSON.stringify(payload),
      });
    } else {
      await apiFetch('/api/team-templates', {
        method: 'POST', body: JSON.stringify(payload),
      });
    }
    showToast('模板已保存');
    closeTemplateEditor();
    await loadTemplates();
  } catch (e) {
    showToast('保存失败: ' + (e.message || e), 'error');
  }
}

async function deleteTemplate(name) {
  if (!confirm('删除模板 ' + name + '？已创建的会议/任务不受影响。')) return;
  try {
    await apiFetch('/api/team-templates/' + encodeURIComponent(name), { method: 'DELETE' });
    showToast('模板已删除');
    await loadTemplates();
  } catch (e) {
    showToast('删除失败: ' + (e.message || e), 'error');
  }
}

async function setProjectDefault(name) {
  try {
    await apiFetch('/api/team-templates/default', {
      method: 'POST', body: JSON.stringify({ name }),
    });
    showToast('已设为当前项目的默认模板');
    await loadTemplates();
  } catch (e) {
    showToast('设置失败: ' + (e.message || e), 'error');
  }
}

function renderTemplates(items, currentDefault) {
  const box = document.getElementById('ttList');
  if (!items.length) {
    box.innerHTML = '<div class="empty-state" style="padding:24px;">还没有模板，点击右上角「新建模板」。</div>';
    return;
  }
  const rows = items.map(t => {
    const isDefault = currentDefault && t.name === currentDefault;
    return '<tr>' +
      '<td><strong>' + escapeValue(t.name) + '</strong>' +
        (isDefault ? ' <span class="badge badge-completed">项目默认</span>' : '') + '</td>' +
      '<td>' + (t.agent_count || 0) + '</td>' +
      '<td style="color:var(--text-dim);font-size:13px;">' + escapeValue(t.description || '-') + '</td>' +
      '<td><div style="display:flex;gap:6px;flex-wrap:wrap;">' +
        '<button class="btn btn-sm" onclick="editTemplate(\'' + t.name + '\')">编辑</button>' +
        '<button class="btn btn-sm" onclick="setProjectDefault(\'' + t.name + '\')">设为项目默认</button>' +
        '<button class="btn btn-sm" style="color:var(--red);" onclick="deleteTemplate(\'' + t.name + '\')">删除</button>' +
      '</div></td>' +
    '</tr>';
  }).join('');
  box.innerHTML = '<div class="table-wrap"><table>' +
    '<thead><tr><th>名称</th><th>成员数</th><th>说明</th><th>操作</th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table></div>';
}

async function loadTemplates() {
  try {
    const data = await apiFetch('/api/team-templates');
    if (data.root) document.getElementById('ttRoot').textContent = data.root;
    renderTemplates(data.items || [], data.default || '');
  } catch (e) {
    document.getElementById('ttList').innerHTML =
      '<div style="color:var(--red);padding:16px;">加载失败：' + escapeValue(e.message || e) + '</div>';
  }
}

/* The layout's shared helpers (apiFetch / showToast) are loaded *after* this
   page's inline script, so initialise only once the document is parsed. */
document.addEventListener('DOMContentLoaded', function () {
  loadTemplates();
});
</script>
"""
    return layout("团队模板", body, active="team-templates")
