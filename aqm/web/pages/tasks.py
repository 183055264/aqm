"""Tasks page — run pipeline form + full task list with filtering/sorting."""

from __future__ import annotations

from aqm.core.agent import AgentDefinition
from aqm.core.task import Task, TaskStatus
from aqm.web.templates import badge, esc, fmt_time, layout


def render_tasks(
    tasks: list[Task],
    agents: dict[str, AgentDefinition],
    pipelines: list[str] | None = None,
    current_pipeline: str = "default",
    agent_error: str | None = None,
) -> str:
    # Pipeline selector
    pipeline_selector = ""
    if pipelines and len(pipelines) > 1:
        pipe_options = "".join(
            f'<option value="{esc(p)}"{"selected" if p == current_pipeline else ""}>{esc(p)}</option>'
            for p in pipelines
        )
        pipeline_selector = f"""\
<div class="card" style="margin-bottom:16px;padding:12px 16px;display:flex;align-items:center;gap:12px;">
  <label style="font-weight:600;white-space:nowrap;">流水线:</label>
  <select id="pipelineSelector" onchange="location.href='/tasks?pipeline='+this.value"
          style="flex:1;max-width:300px;">{pipe_options}</select>
  <span style="font-size:12px;opacity:.6;">{len(pipelines)} 条流水线</span>
</div>"""

    error_banner = ""
    if agent_error:
        error_banner = (
            f'<div class="card" style="background:var(--surface2);border-left:4px solid var(--orange);'
            f'padding:12px 16px;margin-bottom:16px;">'
            f'<strong style="color:var(--orange);">警告: 流水线配置错误</strong>'
            f'<p style="margin:6px 0 0;font-size:13px;opacity:.85;">{esc(agent_error)}</p>'
            f'</div>'
        )

    # Agent options for dropdown
    agent_options = "".join(
        f'<option value="{esc(a.id)}">{esc(a.name)} ({esc(a.id)})</option>'
        for a in agents.values()
    )

    # Run pipeline form
    form = f"""\
<div class="card">
  <h3>运行流水线</h3>
  <form id="runForm" style="margin-top:12px;">
    <div class="form-group">
      <label for="runDesc">任务描述</label>
      <textarea id="runDesc" placeholder="描述你希望流水线执行的任务..."></textarea>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
      <div class="form-group">
        <label for="runAgent">起始代理</label>
        <select id="runAgent"><option value="">默认 (第一个代理)</option>{agent_options}</select>
      </div>
      <div class="form-group">
        <label for="runPriority">优先级</label>
        <select id="runPriority">
          <option value="critical">紧急</option>
          <option value="high">高</option>
          <option value="normal" selected>普通</option>
          <option value="low">低</option>
        </select>
      </div>
    </div>
    <button type="submit" class="btn btn-primary">开始运行</button>
  </form>
</div>"""

    # Unique agents from tasks for filter
    task_agents = sorted(set(t.current_agent_id for t in tasks if t.current_agent_id))
    agent_filter_options = "".join(f'<option value="{esc(a)}">{esc(a)}</option>' for a in task_agents)

    # Filter bar
    filter_bar = f"""\
<div class="filter-bar">
  <div class="form-group">
    <label>状态</label>
    <select id="filterStatus" onchange="applyFilters()" style="width:140px;">
      <option value="">全部</option>
      <option value="pending">待处理</option>
      <option value="in_progress">运行中</option>
      <option value="completed">已完成</option>
      <option value="failed">失败</option>
      <option value="awaiting_gate">等待审批</option>
      <option value="awaiting_human_input">等待输入</option>
      <option value="cancelled">已取消</option>
      <option value="stalled">停滞</option>
    </select>
  </div>
  <div class="form-group">
    <label>代理</label>
    <select id="filterAgent" onchange="applyFilters()" style="width:140px;">
      <option value="">全部</option>
      {agent_filter_options}
    </select>
  </div>
  <div class="form-group">
    <label>优先级</label>
    <select id="filterPriority" onchange="applyFilters()" style="width:120px;">
      <option value="">全部</option>
      <option value="critical">紧急</option>
      <option value="high">高</option>
      <option value="normal">普通</option>
      <option value="low">低</option>
    </select>
  </div>
  <div class="form-group flex-1">
    <label>搜索</label>
    <input id="filterSearch" type="text" placeholder="搜索描述..." oninput="applyFilters()">
  </div>
</div>"""

    # Batch action bar
    batch_bar = """\
<div class="batch-bar" id="batchBar">
  <span class="count" id="batchCount">已选择 0 项</span>
  <button class="btn btn-sm btn-red" onclick="batchCancel()">取消选中</button>
  <button class="btn btn-sm" onclick="batchRestart()">重启选中</button>
  <button class="btn btn-sm" onclick="clearSelection()">清除</button>
</div>"""

    # Task table
    if not tasks:
        table_body = """\
<div class="empty-state">
  <div class="icon">📋</div>
  <div class="title">暂无任务</div>
  <div class="desc">在上方运行一条流水线来创建第一个任务。</div>
</div>"""
        table = f'<h2 style="margin-top:24px;">任务</h2>\n{table_body}'
    else:
        row_list = []
        for t in tasks:
            desc = esc(t.description[:60])
            if len(t.description) > 60:
                desc += "..."
            agent = esc(t.current_agent_id or "-")
            p_name = t.priority.name

            actions_items = f'<a href="/tasks/{esc(t.id)}">查看</a>'
            if t.status in (TaskStatus.completed, TaskStatus.failed):
                actions_items += f'<button onclick="showFixForm(\'{esc(t.id)}\')">修复</button>'
            if t.status in (TaskStatus.failed, TaskStatus.completed, TaskStatus.stalled, TaskStatus.cancelled):
                actions_items += f'<button onclick="restartTask(\'{esc(t.id)}\')">重启</button>'
            if t.status == TaskStatus.awaiting_gate:
                actions_items += f'<a href="/tasks/{esc(t.id)}">审批/拒绝</a>'
            if t.status in (TaskStatus.pending, TaskStatus.in_progress, TaskStatus.awaiting_gate, TaskStatus.awaiting_human_input):
                actions_items += f'<div class="divider"></div><button class="danger" onclick="showCancelModal(\'{esc(t.id)}\')">取消</button>'

            row_list.append(
                f'<tr data-status="{esc(t.status.value)}" data-agent="{agent}" '
                f'data-desc="{esc(t.description.lower())}" data-priority="{p_name}" '
                f'data-created="{t.created_at.isoformat() if t.created_at else ""}" data-id="{esc(t.id)}">'
                f'<td><input type="checkbox" class="row-check" onchange="updateBatch()"></td>'
                f'<td><a href="/tasks/{esc(t.id)}">{esc(t.id)}</a></td>'
                f'<td>{badge(t.status.value)}</td>'
                f'<td style="font-size:12px;color:var(--text-dim);">{p_name}</td>'
                f'<td>{agent}</td>'
                f'<td title="{esc(t.description)}">{desc}</td>'
                f'<td>{len(t.stages)}</td>'
                f'<td style="font-size:12px;">{fmt_time(t.created_at)}</td>'
                f'<td>'
                f'<div class="dropdown">'
                f'<button class="btn btn-sm" onclick="toggleDropdown(this)">操作</button>'
                f'<div class="dropdown-menu">{actions_items}</div>'
                f'</div></td>'
                f'</tr>'
            )
        rows = "\n".join(row_list)

        table = f"""\
<h2 style="margin-top:24px;">任务</h2>
{filter_bar}
{batch_bar}
<div class="table-wrap">
<table id="taskTable">
<thead><tr>
  <th style="width:32px;"><input type="checkbox" id="selectAll" onchange="toggleSelectAll(this)"></th>
  <th data-sort="id">标识 <span class="sort-icon"></span></th>
  <th data-sort="status">状态 <span class="sort-icon"></span></th>
  <th data-sort="priority">优先级 <span class="sort-icon"></span></th>
  <th data-sort="agent">代理 <span class="sort-icon"></span></th>
  <th>描述</th>
  <th data-sort="stages">阶段 <span class="sort-icon"></span></th>
  <th data-sort="created">创建时间 <span class="sort-icon"></span></th>
  <th>操作</th>
</tr></thead>
<tbody id="taskBody">{rows}</tbody>
</table>
</div>
<div class="pagination" id="pagination"></div>"""

    # Cancel modal
    cancel_modal = """\
<div class="modal-overlay" id="cancelModal">
  <div class="modal modal-sm">
    <h3>取消任务</h3>
    <input type="hidden" id="cancelTaskId">
    <div class="form-group">
      <label for="cancelReason">原因 (可选)</label>
      <input id="cancelReason" type="text" placeholder="为什么要取消?">
    </div>
    <div style="display:flex;gap:8px;margin-top:12px;">
      <button class="btn btn-red" onclick="confirmCancel()">取消任务</button>
      <button class="btn" onclick="document.getElementById('cancelModal').classList.remove('show')">关闭</button>
    </div>
  </div>
</div>"""

    # Fix modal
    fix_modal = """\
<div class="modal-overlay" id="fixModal">
  <div class="modal">
    <h3>修复任务 <span id="fixTaskId"></span></h3>
    <form id="fixForm">
      <input type="hidden" id="fixParentId">
      <div class="form-group">
        <label for="fixDesc">需要修复什么?</label>
        <textarea id="fixDesc" placeholder="描述需要修复的内容..."></textarea>
      </div>
      <button type="submit" class="btn btn-primary">开始修复</button>
      <button type="button" class="btn" onclick="document.getElementById('fixModal').classList.remove('show')">取消</button>
    </form>
  </div>
</div>"""

    js = """\
<script>
/* ── Run form ──────────────────────────────── */
document.getElementById('runForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const desc = document.getElementById('runDesc').value.trim();
  if (!desc) return;
  const agent = document.getElementById('runAgent').value;
  const priority = document.getElementById('runPriority').value;
  const pipelineSel = document.getElementById('pipelineSelector');
  const body = {description: desc, priority: priority};
  if (agent) body.agent_id = agent;
  if (pipelineSel) body.pipeline = pipelineSel.value;
  const btn = e.target.querySelector('button[type=submit]');
    btn.disabled = true; btn.textContent = '启动中...';
    try {
      const data = await apiFetch('/api/run', {method:'POST', body:JSON.stringify(body)});
      showToast('流水线已启动: ' + data.task_id);
      setTimeout(() => location.href = '/tasks/' + data.task_id, 600);
    } catch(err) { btn.disabled = false; btn.textContent = '开始运行'; }
});

/* ── Fix modal ─────────────────────────────── */
function showFixForm(taskId) {
  document.getElementById('fixParentId').value = taskId;
  document.getElementById('fixTaskId').textContent = taskId;
  document.getElementById('fixModal').classList.add('show');
}
document.getElementById('fixForm')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const parentId = document.getElementById('fixParentId').value;
  const desc = document.getElementById('fixDesc').value.trim();
  if (!desc) return;
  try {
    const data = await apiFetch('/api/fix', {method:'POST', body:JSON.stringify({parent_task_id:parentId, description:desc})});
      showToast('修复已启动: ' + data.task_id);
    setTimeout(() => location.href = '/tasks/' + data.task_id, 600);
  } catch(err) {}
});
document.querySelectorAll('.modal-overlay').forEach(m => {
  m.addEventListener('click', (e) => { if (e.target === m) m.classList.remove('show'); });
});

/* ── Cancel modal ──────────────────────────── */
function showCancelModal(taskId) {
  document.getElementById('cancelTaskId').value = taskId;
  document.getElementById('cancelReason').value = '';
  document.getElementById('cancelModal').classList.add('show');
}
async function confirmCancel() {
  const taskId = document.getElementById('cancelTaskId').value;
  const reason = document.getElementById('cancelReason').value.trim();
  const body = reason ? {reason} : {};
  try {
    await apiFetch('/api/tasks/' + taskId + '/cancel', {method:'POST', body:JSON.stringify(body)});
    showToast('任务已取消');
    document.getElementById('cancelModal').classList.remove('show');
    setTimeout(() => location.reload(), 600);
  } catch(e) {}
}

/* ── Restart ───────────────────────────────── */
async function restartTask(taskId) {
  try {
    await apiFetch('/api/tasks/' + taskId + '/restart', {method:'POST', body:'{}'});
    showToast('任务重启中');
    setTimeout(() => location.href = '/tasks/' + taskId, 600);
  } catch(e) {}
}

/* ── Filtering ─────────────────────────────── */
function applyFilters() {
  const status = document.getElementById('filterStatus')?.value || '';
  const agent = document.getElementById('filterAgent')?.value || '';
  const priority = document.getElementById('filterPriority')?.value || '';
  const search = document.getElementById('filterSearch')?.value?.toLowerCase() || '';
  document.querySelectorAll('#taskBody tr').forEach(row => {
    const show = (!status || row.dataset.status === status)
      && (!agent || row.dataset.agent === agent)
      && (!priority || row.dataset.priority === priority)
      && (!search || row.dataset.desc?.includes(search));
    row.style.display = show ? '' : 'none';
  });
  paginate();
}

/* ── Sorting ───────────────────────────────── */
let sortCol = '', sortAsc = true;
document.querySelectorAll('th[data-sort]').forEach(th => {
  th.addEventListener('click', () => {
    const col = th.dataset.sort;
    if (sortCol === col) { sortAsc = !sortAsc; } else { sortCol = col; sortAsc = true; }
    const tbody = document.getElementById('taskBody');
    if (!tbody) return;
    const rows = Array.from(tbody.querySelectorAll('tr'));
    rows.sort((a, b) => {
      let av = a.dataset[col] || '';
      let bv = b.dataset[col] || '';
      if (col === 'stages') { av = a.children[6]?.textContent||'0'; bv = b.children[6]?.textContent||'0'; return sortAsc ? av-bv : bv-av; }
      return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
    });
    rows.forEach(r => tbody.appendChild(r));
    document.querySelectorAll('th[data-sort] .sort-icon').forEach(s => s.textContent = '');
    th.querySelector('.sort-icon').textContent = sortAsc ? ' ▲' : ' ▼';
    paginate();
  });
});

/* ── Pagination ────────────────────────────── */
const PAGE_SIZE = 25;
let currentPage = 1;
function paginate() {
  const rows = Array.from(document.querySelectorAll('#taskBody tr')).filter(r => r.style.display !== 'none');
  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  if (currentPage > totalPages) currentPage = totalPages;
  rows.forEach((row, i) => {
    row.style.display = (i >= (currentPage-1)*PAGE_SIZE && i < currentPage*PAGE_SIZE) ? '' : 'none';
  });
  const pag = document.getElementById('pagination');
  if (!pag || rows.length <= PAGE_SIZE) { if(pag) pag.innerHTML = ''; return; }
  let html = '<button '+(currentPage<=1?'disabled':'')+' onclick="goPage('+(currentPage-1)+')">上一页</button>';
  for (let p = 1; p <= totalPages; p++) {
    html += '<button class="'+(p===currentPage?'active':'')+'" onclick="goPage('+p+')">'+p+'</button>';
  }
  html += '<button '+(currentPage>=totalPages?'disabled':'')+' onclick="goPage('+(currentPage+1)+')">下一页</button>';
  html += '<span class="info">'+rows.length+' 个任务</span>';
  pag.innerHTML = html;
}
function goPage(p) { currentPage = p; applyFilters(); }

/* ── Batch selection ───────────────────────── */
function toggleSelectAll(el) {
  document.querySelectorAll('#taskBody .row-check').forEach(cb => {
    if (cb.closest('tr').style.display !== 'none') cb.checked = el.checked;
  });
  updateBatch();
}
function updateBatch() {
  const checked = document.querySelectorAll('#taskBody .row-check:checked');
  const bar = document.getElementById('batchBar');
  if (!bar) return;
  if (checked.length > 0) {
    bar.classList.add('show');
    document.getElementById('batchCount').textContent = checked.length + ' 项已选择';
  } else {
    bar.classList.remove('show');
  }
}
function getSelectedIds() {
  return Array.from(document.querySelectorAll('#taskBody .row-check:checked'))
    .map(cb => cb.closest('tr').dataset.id);
}
function clearSelection() {
  document.querySelectorAll('#taskBody .row-check').forEach(cb => cb.checked = false);
  document.getElementById('selectAll').checked = false;
  updateBatch();
}
async function batchCancel() {
  const ids = getSelectedIds();
  if (!ids.length || !confirm('取消 ' + ids.length + ' 个任务?')) return;
  for (const id of ids) {
    try { await apiFetch('/api/tasks/' + id + '/cancel', {method:'POST', body:'{}'}); } catch(e) {}
  }
  showToast('已取消 ' + ids.length + ' 个任务');
  setTimeout(() => location.reload(), 600);
}
async function batchRestart() {
  const ids = getSelectedIds();
  if (!ids.length || !confirm('重启 ' + ids.length + ' 个任务?')) return;
  for (const id of ids) {
    try { await apiFetch('/api/tasks/' + id + '/restart', {method:'POST', body:'{}'}); } catch(e) {}
  }
  showToast('正在重启 ' + ids.length + ' 个任务');
  setTimeout(() => location.reload(), 600);
}

/* Init pagination */
paginate();
</script>"""

    return layout(
        "任务",
        f"{pipeline_selector}\n{error_banner}\n{form}\n{table}\n{cancel_modal}\n{fix_modal}\n{js}",
        active="tasks",
    )
