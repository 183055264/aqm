"""Task detail page — tabbed interface with overview, live output, timeline, context, chunks."""

from __future__ import annotations

from aqm.core.agent import AgentDefinition
from aqm.core.task import Task, TaskStatus
from aqm.web.templates import badge, copy_pre, esc, fmt_duration, fmt_time, layout


def render_task_detail(
    task: Task,
    agents: dict[str, AgentDefinition],
    context_md: str,
) -> str:
    short_id = task.id[:10]
    show_live = task.status in (TaskStatus.in_progress, TaskStatus.awaiting_gate, TaskStatus.awaiting_human_input)

    # ── Agent tooltip helper ──────────────────────────────────────────
    def agent_tip(agent_id: str) -> str:
        a = agents.get(agent_id)
        if not a:
            return esc(agent_id)
        tip_parts = [f"运行时: {a.runtime or 'N/A'}"]
        if a.gate:
            tip_parts.append(f"审批门: {a.gate.type}")
        if a.mcp:
            tip_parts.append(f"MCP 服务: {', '.join(m.server for m in a.mcp)}")
        tip_text = esc("\\n".join(tip_parts))
        name = esc(a.name or a.id)
        return f'<span class="agent-tip" data-tip="{tip_text}">{name}</span>'

    # ── Meta card ─────────────────────────────────────────────────────
    meta = f"""\
<div class="card">
  <div class="meta-row">
    <div class="meta-item"><span class="meta-label">标识:</span> {esc(task.id)}</div>
    <div class="meta-item"><span class="meta-label">状态:</span> {badge(task.status.value)}</div>
    <div class="meta-item"><span class="meta-label">代理:</span> {agent_tip(task.current_agent_id or '-')}</div>
  </div>
  <div class="meta-row">
    <div class="meta-item"><span class="meta-label">创建时间:</span> {fmt_time(task.created_at)}</div>
    <div class="meta-item"><span class="meta-label">更新时间:</span> {fmt_time(task.updated_at)}</div>
    <div class="meta-item"><span class="meta-label">阶段数:</span> {len(task.stages)}</div>
  </div>
  <div style="margin-top:12px;">
    <span class="meta-label">描述:</span>
    <p style="margin-top:4px;">{esc(task.description)}</p>
  </div>
</div>"""

    # ── Gate action panel ─────────────────────────────────────────────
    gate_actions = ""
    if task.status == TaskStatus.awaiting_gate:
        gate_actions = f"""\
<div class="card" style="border-color:var(--orange);">
  <h3 style="color:var(--orange);"><span class="live-dot" style="background:var(--orange);"></span>等待审批</h3>
  <div style="margin-top:12px;">
    <div class="form-group">
      <label for="gateReason">原因 (拒绝时必填)</label>
      <input id="gateReason" type="text" placeholder="可选原因...">
    </div>
    <button class="btn btn-green" onclick="gateAction('approve')">通过</button>
    <button class="btn btn-red" style="margin-left:8px;" onclick="gateAction('reject')">拒绝</button>
  </div>
</div>"""

    # ── Human input panel ─────────────────────────────────────────────
    human_input_panel = ""
    if task.status == TaskStatus.awaiting_human_input:
        pending = task.metadata.get("_human_input_pending", {})
        hi_agent = pending.get("agent_id", "")
        hi_questions = pending.get("questions", [])
        hi_mode = pending.get("mode", "")
        questions_html = "".join(
            f'<div style="background:var(--surface2);border-left:3px solid var(--cyan);'
            f'padding:10px 14px;margin:8px 0;border-radius:0 6px 6px 0;font-size:14px;">'
            f'{esc(q)}</div>'
            for q in hi_questions
        )
        human_input_panel = f"""\
<div class="card" style="border-color:var(--cyan);">
  <h3 style="color:var(--cyan);"><span class="live-dot" style="background:var(--cyan);"></span>代理需要您的输入</h3>
  <div style="margin-top:4px;font-size:13px;color:var(--text-dim);">
    代理 <strong>{esc(hi_agent)}</strong> ({esc(hi_mode)} 模式)
  </div>
  <div style="margin-top:12px;">
    {questions_html}
    <div class="form-group" style="margin-top:12px;">
      <label for="humanInput">您的回复</label>
      <textarea id="humanInput" rows="4" placeholder="输入您的回复..."></textarea>
    </div>
    <button class="btn btn-primary" onclick="submitHumanInput()">提交回复</button>
  </div>
</div>"""

    # ── Restart panel ─────────────────────────────────────────────────
    restart_panel = ""
    if task.status in (TaskStatus.failed, TaskStatus.completed, TaskStatus.stalled, TaskStatus.cancelled):
        stage_options = "".join(
            f'<option value="{s.stage_number}">阶段 {s.stage_number} — {esc(s.agent_id)}</option>'
            for s in task.stages
        )
        restart_panel = f"""\
<div class="card" style="border-color:var(--accent);">
  <h3>重启任务</h3>
  <div style="margin-top:12px;">
    <div class="form-group">
      <label for="restartStage">从阶段重启</label>
      <select id="restartStage">
        <option value="">默认 (自动检测)</option>
        {stage_options}
      </select>
    </div>
    <button class="btn btn-green" onclick="restartTask()">重启</button>
  </div>
</div>"""

    # ── Live progress panel ───────────────────────────────────────────
    live_panel = ""
    if show_live:
        panel_title = (
            "流水线运行中" if task.status == TaskStatus.in_progress
            else "等待人工输入" if task.status == TaskStatus.awaiting_human_input
            else "等待审批"
        )
        live_panel = f"""\
<div class="card" style="border-color:var(--accent);">
  <h3 id="liveTitle"><span class="live-dot"></span>{panel_title}</h3>
  <div class="progress-bar" style="margin-top:8px;"><div class="fill" id="progressFill" style="width:0%"></div></div>
  <div id="liveStatus" style="margin-top:8px;font-size:13px;color:var(--text-dim);"></div>
  <div id="thinkingPanel" style="display:none;margin-top:12px;">
    <div style="display:flex;align-items:center;gap:8px;cursor:pointer;" onclick="toggleThinking()">
      <span style="font-size:13px;font-weight:600;color:var(--purple);">
        <span id="thinkingIcon" style="display:inline-block;transition:transform .2s;">&#9654;</span> 思考
      </span>
      <span id="thinkingBadge" style="font-size:11px;background:#1a1040;color:var(--purple);border:1px solid #3b2d6b;padding:1px 8px;border-radius:10px;"></span>
    </div>
    <pre id="thinkingOutput" style="background:#0d0a1a;border:1px solid #2d2050;border-radius:6px;padding:12px;font-size:11px;max-height:300px;overflow-y:auto;white-space:pre-wrap;color:#bc8cff;display:none;font-style:italic;"></pre>
  </div>
  <div style="margin-top:8px;">
    <div style="font-size:13px;font-weight:600;color:var(--accent);margin-bottom:6px;" id="outputLabel" style="display:none;">输出</div>
    <pre id="liveOutput" style="margin-top:0;max-height:400px;overflow-y:auto;display:none;"></pre>
  </div>
  <button class="btn btn-red btn-sm" style="margin-top:12px;" id="cancelBtn" onclick="cancelRunningTask()">取消</button>
</div>"""

    # ── Stage timeline ────────────────────────────────────────────────
    if not task.stages:
        timeline = '<div class="timeline" id="liveTimeline"><div class="empty-state" style="padding:24px;">暂无记录阶段。</div></div>'
    else:
        items = []
        for s in task.stages:
            status_class = ""
            if s.gate_result == "approved":
                status_class = "approved"
            elif s.gate_result == "rejected":
                status_class = "rejected"
            elif "ERROR" in s.output_text:
                status_class = "failed"
            elif s.gate_result is None and task.status == TaskStatus.awaiting_gate and s == task.stages[-1]:
                status_class = "awaiting"

            gate_info = ""
            if s.gate_result:
                gate_info = f' · 审批门: {badge(s.gate_result)}'
                if s.reject_reason:
                    gate_info += f' <span style="font-size:12px;color:var(--text-dim);">({esc(s.reject_reason[:100])})</span>'

            retry_info = ""
            if s.retry_count > 0:
                retry_info = f' · <span style="color:var(--warn);">重试 {s.retry_count}×</span>'
                if s.retry_reason:
                    retry_info += f' <span style="font-size:12px;color:var(--text-dim);">({esc(s.retry_reason[:120])})</span>'

            duration = fmt_duration(s.started_at, s.finished_at)
            duration_html = f'<span class="duration">{duration}</span>' if duration else ""

            output_preview = esc(s.output_text[:200]) + ("..." if len(s.output_text) > 200 else "")

            items.append(
                f'<div class="timeline-item {status_class}">'
                f'<div><strong>阶段 {s.stage_number}</strong> · '
                f'{agent_tip(s.agent_id)}'
                f'{gate_info}{retry_info}{duration_html}</div>'
                f'<div style="font-size:12px;color:var(--text-dim);">'
                f'{fmt_time(s.started_at)} → {fmt_time(s.finished_at)}</div>'
                f'<div style="font-size:13px;margin-top:4px;color:var(--text-dim);">{output_preview}</div>'
                 f'<details><summary>完整输入</summary>{copy_pre(s.input_text)}</details>'
                 f'<details><summary>完整输出</summary>{copy_pre(s.output_text)}</details>'
                f'</div>'
            )
        timeline = f'<div class="timeline" id="liveTimeline">{"".join(items)}</div>'

    # ── Context viewer ────────────────────────────────────────────────
    if context_md:
        context_section = f"""\
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
  <span style="font-weight:600;">上下文文件</span>
  <a href="/api/tasks/{esc(task.id)}/context" class="btn btn-sm" download="context.md">下载</a>
</div>
{copy_pre(context_md)}"""
    else:
        context_section = '<div class="empty-state" style="padding:24px;">该任务没有上下文文件。</div>'

    # ── Chunks panel ──────────────────────────────────────────────────
    chunks_section = f"""\
<div id="chunksPanel">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
    <span style="font-weight:600;">工作块</span>
    <div style="display:flex;gap:8px;">
      <input id="newChunkDesc" type="text" placeholder="新工作块描述..." style="width:260px;padding:6px 10px;border:1px solid var(--border);border-radius:4px;background:var(--surface2);color:var(--text);font-size:13px;">
      <button class="btn btn-sm btn-primary" onclick="addChunk()">添加</button>
    </div>
  </div>
  <div id="chunkList"><div class="empty-state" style="padding:16px;font-size:13px;">加载工作块中...</div></div>
</div>
<script>
async function loadChunks() {{
  try {{
    const chunks = await apiFetch('/api/tasks/{esc(task.id)}/chunks');
    const el = document.getElementById('chunkList');
    if (!chunks.length) {{ el.innerHTML = '<div class="empty-state" style="padding:16px;font-size:13px;">暂无工作块。</div>'; return; }}
    el.innerHTML = chunks.map(c => `
      <div class="chunk-item" data-id="${{c.id}}">
        <span class="badge badge-${{c.status}}">${{c.status}}</span>
        <span class="chunk-desc">${{c.id}}: ${{c.description}}</span>
        <select onchange="updateChunk('${{c.id}}', this.value)" style="width:auto;padding:4px 8px;font-size:12px;background:var(--surface2);border:1px solid var(--border);border-radius:4px;color:var(--text);">
          <option value="pending" ${{c.status==='pending'?'selected':''}}>待处理</option>
          <option value="in_progress" ${{c.status==='in_progress'?'selected':''}}>进行中</option>
          <option value="done" ${{c.status==='done'?'selected':''}}>已完成</option>
        </select>
        <button class="btn btn-sm" onclick="removeChunk('${{c.id}}')" style="color:var(--red);">移除</button>
      </div>
    `).join('');
  }} catch(e) {{}}
}}
async function addChunk() {{
  const desc = document.getElementById('newChunkDesc').value.trim();
  if (!desc) return;
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/chunks', {{method:'POST', body:JSON.stringify({{description:desc}})}});
    document.getElementById('newChunkDesc').value = '';
    loadChunks();
  }} catch(e) {{}}
}}
async function updateChunk(chunkId, status) {{
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/chunks/' + chunkId, {{method:'PATCH', body:JSON.stringify({{status}})}});
    loadChunks();
  }} catch(e) {{}}
}}
async function removeChunk(chunkId) {{
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/chunks/' + chunkId, {{method:'DELETE'}});
    loadChunks();
  }} catch(e) {{}}
}}
loadChunks();
</script>"""

    # ── Fix form ──────────────────────────────────────────────────────
    fix_section = ""
    if task.status in (TaskStatus.completed, TaskStatus.failed):
        fix_section = f"""\
<div class="card" style="margin-top:16px;">
  <h3>后续修复</h3>
  <form id="fixForm" style="margin-top:8px;">
    <div class="form-group">
      <label for="fixDesc">需要修复什么?</label>
      <textarea id="fixDesc" placeholder="描述需要修复的内容..."></textarea>
    </div>
    <button type="submit" class="btn btn-primary">开始修复</button>
  </form>
</div>"""

    # ── Assemble tabs ─────────────────────────────────────────────────
    # Tab: Overview (meta + action panels + fix)
    tab_overview = f"{meta}\n{gate_actions}\n{human_input_panel}\n{restart_panel}\n{fix_section}"

    # Tab: Live Output (only visible when task is active)
    tab_output = live_panel if show_live else '<div class="empty-state" style="padding:24px;">任务当前未在运行。</div>'

    # Default tab
    default_tab = "tab-2" if show_live else "tab-1"

    body = f"""\
<h1>Task {esc(short_id)}</h1>

<input type="radio" name="tab" id="tab-1" {"checked" if default_tab == "tab-1" else ""}>
<input type="radio" name="tab" id="tab-2" {"checked" if default_tab == "tab-2" else ""}>
<input type="radio" name="tab" id="tab-3">
<input type="radio" name="tab" id="tab-4">
<input type="radio" name="tab" id="tab-5">

<div class="tabs">
  <label for="tab-1" class="tab">概览</label>
  <label for="tab-2" class="tab">{"实时输出" if show_live else "输出"}</label>
  <label for="tab-3" class="tab">时间线 ({len(task.stages)})</label>
  <label for="tab-4" class="tab">上下文</label>
  <label for="tab-5" class="tab">工作块</label>
</div>

<div class="tab-panels">
  <div class="tab-panel" id="panel-1">{tab_overview}</div>
  <div class="tab-panel" id="panel-2">{tab_output}</div>
  <div class="tab-panel" id="panel-3">{timeline}</div>
  <div class="tab-panel" id="panel-4">{context_section}</div>
  <div class="tab-panel" id="panel-5">{chunks_section}</div>
</div>"""

    # ── Page JS ───────────────────────────────────────────────────────
    js = f"""\
<script>
/* Tab hash persistence */
function setTabFromHash() {{
  const hash = location.hash.replace('#', '');
  const map = {{'overview':'tab-1','output':'tab-2','timeline':'tab-3','context':'tab-4','chunks':'tab-5'}};
  const tabId = map[hash];
  if (tabId) document.getElementById(tabId).checked = true;
}}
setTabFromHash();
window.addEventListener('hashchange', setTabFromHash);
document.querySelectorAll('.tabs label').forEach(l => {{
  l.addEventListener('click', () => {{
    const map = {{'tab-1':'overview','tab-2':'output','tab-3':'timeline','tab-4':'context','tab-5':'chunks'}};
    const hash = map[l.getAttribute('for')];
    if (hash) history.replaceState(null, '', '#' + hash);
  }});
}});

/* Gate actions */
async function gateAction(action) {{
  const reason = document.getElementById('gateReason')?.value?.trim() || '';
  if (action === 'reject' && !reason) {{ showToast('拒绝时需提供原因', 'error'); return; }}
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/' + action, {{method:'POST', body:JSON.stringify({{reason:reason||undefined}})}});
    showToast('任务' + action + '已处理');
    setTimeout(() => location.reload(), 600);
  }} catch(e) {{}}
}}

/* Human input */
async function submitHumanInput() {{
  const response = document.getElementById('humanInput')?.value?.trim();
  if (!response) {{ showToast('请输入回复', 'error'); return; }}
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/human-input', {{method:'POST', body:JSON.stringify({{response}})}});
    showToast('回复已提交');
    setTimeout(() => location.reload(), 600);
  }} catch(e) {{}}
}}

/* Restart */
async function restartTask() {{
  const stageVal = document.getElementById('restartStage')?.value;
  const body = stageVal ? {{from_stage: parseInt(stageVal)}} : {{}};
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/restart', {{method:'POST', body:JSON.stringify(body)}});
    showToast('任务重启中...');
    setTimeout(() => location.reload(), 800);
  }} catch(e) {{}}
}}

/* Cancel */
async function cancelRunningTask() {{
  if (!confirm('取消此运行中的流水线?')) return;
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/cancel', {{method:'POST', body:'{{}}'}});
    showToast('已请求取消');
    setTimeout(() => location.reload(), 1000);
  }} catch(e) {{}}
}}

/* Fix form */
document.getElementById('fixForm')?.addEventListener('submit', async (e) => {{
  e.preventDefault();
  const desc = document.getElementById('fixDesc')?.value?.trim();
  if (!desc) return;
  try {{
    const data = await apiFetch('/api/fix', {{method:'POST', body:JSON.stringify({{parent_task_id:'{esc(task.id)}', description:desc}})}});
    showToast('修复已启动: ' + data.task_id);
    setTimeout(() => location.href = '/tasks/' + data.task_id, 600);
  }} catch(e) {{}}
}});

/* Thinking toggle */
let thinkingExpanded = false;
function toggleThinking() {{
  thinkingExpanded = !thinkingExpanded;
  const el = document.getElementById('thinkingOutput');
  const icon = document.getElementById('thinkingIcon');
  if (el) el.style.display = thinkingExpanded ? 'block' : 'none';
  if (icon) icon.style.transform = thinkingExpanded ? 'rotate(90deg)' : 'rotate(0deg)';
  if (thinkingExpanded && el) el.scrollTop = el.scrollHeight;
}}
</script>"""

    # ── SSE listener (separate, only when live) ───────────────────────
    sse_js = ""
    if show_live:
        sse_js = f"""\
<script>
(function() {{
  const es = new EventSource('/api/tasks/{esc(task.id)}/events');
  const statusEl = document.getElementById('liveStatus');
  const outputEl = document.getElementById('liveOutput');
  const titleEl = document.getElementById('liveTitle');
  const thinkingPanel = document.getElementById('thinkingPanel');
  const thinkingOutput = document.getElementById('thinkingOutput');
  const thinkingBadge = document.getElementById('thinkingBadge');
  const outputLabel = document.getElementById('outputLabel');
  let thinkingLines = 0;

  es.addEventListener('stage_start', (e) => {{
    const d = JSON.parse(e.data);
    titleEl.innerHTML = '<span class="live-dot"></span>流水线运行中';
    statusEl.innerHTML = '<span class="live-dot"></span>阶段 ' + d.stage_number + ': <strong>' + d.agent_id + '</strong>';
    if (outputEl) {{ outputEl.textContent = ''; outputEl.style.display = 'block'; }}
    if (outputLabel) outputLabel.style.display = 'block';
    if (thinkingOutput) thinkingOutput.textContent = '';
    thinkingLines = 0;
    if (thinkingPanel) thinkingPanel.style.display = 'none';
  }});
  es.addEventListener('stage_thinking', (e) => {{
    const d = JSON.parse(e.data);
    if (thinkingPanel) thinkingPanel.style.display = 'block';
    thinkingLines++;
    if (thinkingBadge) thinkingBadge.textContent = thinkingLines + ' chunks';
    if (thinkingOutput) {{ thinkingOutput.textContent += d.text + '\\n'; if (thinkingExpanded) thinkingOutput.scrollTop = thinkingOutput.scrollHeight; }}
  }});
  es.addEventListener('stage_output', (e) => {{
    const d = JSON.parse(e.data);
    if (outputEl) {{ outputEl.style.display = 'block'; outputEl.textContent += d.text + '\\n'; outputEl.scrollTop = outputEl.scrollHeight; }}
    if (outputLabel) outputLabel.style.display = 'block';
  }});
  es.addEventListener('tool_call', (e) => {{
    const d = JSON.parse(e.data);
    const label = '[Tool: ' + (d.tool || 'unknown') + '] ' + (d.title || '') + ' (' + (d.status || '?') + ')';
    if (outputEl) {{
      outputEl.style.display = 'block';
      outputEl.textContent += label + '\\n';
      outputEl.scrollTop = outputEl.scrollHeight;
    }}
    if (outputLabel) outputLabel.style.display = 'block';
  }});
  es.addEventListener('stage_complete', (e) => {{
    const d = JSON.parse(e.data);
    statusEl.innerHTML = '阶段 ' + d.stage_number + ': <strong>' + d.agent_id + '</strong> — ' + (d.gate_result || '完成');
  }});
  es.addEventListener('pipeline_resuming', (e) => {{
    titleEl.innerHTML = '<span class="live-dot"></span>恢复中...';
    statusEl.innerHTML = '加载下一个代理...';
  }});
  es.addEventListener('human_input_waiting', () => {{ es.close(); setTimeout(() => location.reload(), 800); }});
  es.addEventListener('gate_waiting', () => {{ es.close(); setTimeout(() => location.reload(), 800); }});
  es.addEventListener('task_complete', () => {{
    es.close();
    showToast('流水线已完成');
    titleEl.innerHTML = '<span style="color:var(--green);">&#10003;</span> 已完成';
    document.getElementById('cancelBtn').style.display = 'none';
  }});
  es.addEventListener('task_cancelled', () => {{ es.close(); setTimeout(() => location.reload(), 800); }});
  es.addEventListener('task_failed', (e) => {{
    es.close();
    const d = JSON.parse(e.data);
    showToast('流水线失败: ' + (d.error || ''), 'error');
    titleEl.innerHTML = '<span style="color:var(--red);">&#10007;</span> 失败';
    document.getElementById('cancelBtn').style.display = 'none';
  }});
  es.onerror = () => es.close();
}})();
</script>"""

    return layout(
        f"任务 {short_id}",
        f"{body}\n{js}\n{sse_js}",
        active="tasks",
        breadcrumbs=[("看板", "/"), (f"任务 {short_id}", None)],
    )
