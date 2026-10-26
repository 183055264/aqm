"""Meeting page — multi-agent collaborative discussion."""

from __future__ import annotations

from aqm.web.templates import layout


def render_meeting() -> str:
    body = r"""
<h1>会议协作</h1>

<div class="card">
  <h3>创建会议</h3>
  <div class="form-group" style="margin-top:12px;">
    <label>会议主题 *</label>
    <input id="mtTopic" type="text" placeholder="例如：AI服装打板CAD架构讨论" style="width:100%;">
  </div>
  <div class="form-group">
    <label>议题描述</label>
    <textarea id="mtDesc" rows="4" placeholder="项目背景、讨论范围、关键问题..." style="width:100%;"></textarea>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;">
    <div class="form-group">
      <label>共享文件名</label>
      <input id="mtFile" type="text" value="meeting.md" style="width:100%;">
    </div>
    <div class="form-group">
      <label>讨论轮数</label>
      <input id="mtRounds" type="number" value="1" min="1" max="10" style="width:100%;">
    </div>
    <div class="form-group">
      <label>每Agent超时(秒)</label>
      <input id="mtTimeout" type="number" value="300" min="30" max="1800" style="width:100%;">
    </div>
  </div>

  <div style="margin-top:16px;">
    <label style="font-weight:600;font-size:13px;color:var(--text-dim);">参会Agent</label>
    <div id="agentList" style="margin-top:8px;"></div>
    <button class="btn btn-sm" onclick="addAgentRow()" style="margin-top:8px;">+ 添加Agent</button>
  </div>

  <div style="margin-top:16px;display:flex;gap:8px;">
    <button class="btn btn-primary" onclick="createAndStartMeeting()">创建并开始</button>
    <button class="btn" onclick="loadDefaultAgents()">加载默认配置</button>
  </div>
</div>

<div id="meetingList" style="margin-top:24px;"></div>

<div id="meetingLive" style="display:none;">
  <div class="card" style="border-color:var(--accent);">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
      <h3 id="liveTitle">会议进行中...</h3>
      <div style="display:flex;gap:8px;">
        <button class="btn btn-sm btn-red" onclick="stopCurrentMeeting()">停止</button>
        <a class="btn btn-sm" href="/meeting">关闭</a>
      </div>
    </div>
    <div id="liveStatus" style="margin-bottom:12px;"></div>
    <div id="liveAgents" style="margin-bottom:16px;"></div>
    <h4 style="margin-bottom:8px;">实时输出</h4>
    <pre id="liveOutput" style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:12px;max-height:400px;overflow-y:auto;font-size:13px;line-height:1.5;white-space:pre-wrap;word-break:break-all;"></pre>
    <div style="margin-top:12px;">
      <button class="btn btn-sm" onclick="loadSharedFile()">查看共享文件</button>
    </div>
    <pre id="sharedFilePreview" style="display:none;margin-top:8px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:12px;max-height:300px;overflow-y:auto;font-size:13px;line-height:1.5;white-space:pre-wrap;word-break:break-all;"></pre>
  </div>
</div>

<script>
let currentMeetingId = null;
let eventSource = null;

/* ── Agent Rows ─────────────────────────── */
function addAgentRow(agent) {
  const list = document.getElementById('agentList');
  const row = document.createElement('div');
  row.style.cssText = 'display:grid;grid-template-columns:1fr 1fr 1fr 2fr;gap:8px;margin-bottom:8px;align-items:start;';
  row.innerHTML = `
    <div class="form-group" style="margin-bottom:0;">
      <label style="font-size:12px;">ID *</label>
      <input class="ag-id" type="text" value="${(agent&&agent.id)||''}" placeholder="agent_id" style="width:100%;">
    </div>
    <div class="form-group" style="margin-bottom:0;">
      <label style="font-size:12px;">运行时 *</label>
      <select class="ag-runtime" style="width:100%;">
        <option value="claude" ${(agent&&agent.runtime==='claude')?'selected':''}>Claude</option>
        <option value="opencode" ${(agent&&agent.runtime==='opencode')?'selected':''}>opencode</option>
        <option value="kilo" ${(agent&&agent.runtime==='kilo')?'selected':''}>kilo</option>
        <option value="cline" ${(agent&&agent.runtime==='cline')?'selected':''}>cline</option>
      </select>
    </div>
    <div class="form-group" style="margin-bottom:0;">
      <label style="font-size:12px;">角色</label>
      <input class="ag-role" type="text" value="${(agent&&agent.role)||''}" placeholder="架构师" style="width:100%;">
    </div>
    <div style="display:flex;gap:8px;align-items:flex-end;">
      <div class="form-group" style="flex:1;margin-bottom:0;">
        <label style="font-size:12px;">系统提示词</label>
        <textarea class="ag-prompt" rows="2" placeholder="你是..." style="width:100%;font-size:12px;">${(agent&&agent.system_prompt)||''}</textarea>
      </div>
      <button type="button" class="btn btn-sm" style="color:var(--red);height:38px;" onclick="this.closest('div[style]').remove()">&#10005;</button>
    </div>
  `;
  list.appendChild(row);
}

async function loadDefaultAgents() {
  try {
    const data = await apiFetch('/api/meetings/default-agents');
    document.getElementById('agentList').innerHTML = '';
    data.forEach(a => addAgentRow(a));
    showToast('已加载默认Agent配置');
  } catch(e) { showToast('加载失败', 'error'); }
}

/* ── Create & Start ─────────────────────── */
async function createAndStartMeeting() {
  const topic = document.getElementById('mtTopic').value.trim();
  if (!topic) { showToast('请输入会议主题', 'error'); return; }

  const agents = [];
  document.querySelectorAll('#agentList > div').forEach(row => {
    const id = row.querySelector('.ag-id').value.trim();
    const runtime = row.querySelector('.ag-runtime').value;
    const role = row.querySelector('.ag-role').value.trim();
    const prompt = row.querySelector('.ag-prompt').value.trim();
    if (!id || !runtime) { showToast('Agent ID和运行时不能为空', 'error'); return; }
    agents.push({id, name: role || id, runtime, role: role, system_prompt: prompt});
  });

  if (!agents.length) { showToast('请至少添加一个Agent', 'error'); return; }

  try {
    const data = await apiFetch('/api/meetings', {
      method: 'POST',
      body: JSON.stringify({
        topic: topic,
        description: document.getElementById('mtDesc').value.trim(),
        agents: agents,
        shared_file: document.getElementById('mtFile').value.trim() || 'meeting.md',
        rounds: parseInt(document.getElementById('mtRounds').value) || 1,
        timeout_per_agent: parseInt(document.getElementById('mtTimeout').value) || 300,
      }),
    });
    currentMeetingId = data.id;
    await startMeeting(currentMeetingId);
    showToast('会议已创建并开始');
  } catch(e) { showToast('创建失败: ' + e.message, 'error'); }
}

/* ── Start / Stop ───────────────────────── */
async function startMeeting(meetingId) {
  try {
    await apiFetch(`/api/meetings/${meetingId}/start`, {method: 'POST'});
    showLiveView(meetingId);
  } catch(e) { showToast('启动失败: ' + e.message, 'error'); }
}

async function stopCurrentMeeting() {
  if (!currentMeetingId) return;
  if (!confirm('确定停止会议？')) return;
  try {
    await apiFetch(`/api/meetings/${currentMeetingId}/stop`, {method: 'POST'});
    showToast('会议已停止');
  } catch(e) { showToast('停止失败', 'error'); }
}

/* ── Meeting List ───────────────────────── */
async function refreshMeetingList() {
  try {
    const meetings = await apiFetch('/api/meetings');
    const container = document.getElementById('meetingList');
    if (!meetings.length) {
      container.innerHTML = '<p style="color:var(--text-dim);text-align:center;padding:16px;">暂无会议记录</p>';
      return;
    }
    let html = '<div class="card"><h3>会议记录</h3><div class="table-wrap"><table><thead><tr><th>主题</th><th>状态</th><th>Agent数</th><th>轮数</th><th>创建时间</th><th>操作</th></tr></thead><tbody>';
    meetings.forEach(m => {
      const statusBadge = m.status === 'running' ? '<span class="badge badge-in_progress">运行中</span>' :
                          m.status === 'completed' ? '<span class="badge badge-completed">已完成</span>' :
                          m.status === 'stopped' ? '<span class="badge badge-cancelled">已停止</span>' :
                          m.status === 'error' ? '<span class="badge badge-failed">失败</span>' :
                          '<span class="badge badge-pending">待开始</span>';
      const actions = m.status === 'running'
        ? `<button class="btn btn-sm btn-red" onclick="stopCurrentMeeting2('${m.id}')">停止</button>`
        : `<button class="btn btn-sm" onclick="startMeeting2('${m.id}')">启动</button>
           <button class="btn btn-sm" onclick="viewMeeting('${m.id}')">查看</button>
           <button class="btn btn-sm" style="color:var(--red);" onclick="deleteMeeting2('${m.id}')">删除</button>`;
      html += `<tr><td><strong>${m.topic}</strong></td><td>${statusBadge}</td><td>${m.agents.length}</td><td>${m.current_round}/${m.rounds}</td><td>${m.created_at.substring(0,16).replace('T',' ')}</td><td>${actions}</td></tr>`;
    });
    html += '</tbody></table></div></div>';
    container.innerHTML = html;
  } catch(e) {}
}

function stopCurrentMeeting2(id) {
  currentMeetingId = id;
  stopCurrentMeeting();
}

function startMeeting2(id) {
  currentMeetingId = id;
  startMeeting(id);
}

async function viewMeeting(id) {
  currentMeetingId = id;
  const m = await apiFetch(`/api/meetings/${id}`);
  showLiveView(id, m);
  const file = await apiFetch(`/api/meetings/${id}/file`);
  document.getElementById('sharedFilePreview').textContent = file.content || '(空)';
  document.getElementById('sharedFilePreview').style.display = 'block';
}

async function deleteMeeting2(id) {
  if (!confirm('确定删除此会议？')) return;
  try {
    await apiFetch(`/api/meetings/${id}`, {method: 'DELETE'});
    showToast('已删除');
    refreshMeetingList();
  } catch(e) { showToast('删除失败: ' + e.message, 'error'); }
}

/* ── Live View ──────────────────────────── */
function showLiveView(meetingId, meeting) {
  document.getElementById('meetingLive').style.display = 'block';
  document.getElementById('sharedFilePreview').style.display = 'none';
  if (!meeting) {
    apiFetch(`/api/meetings/${meetingId}`).then(m => {
      document.getElementById('liveTitle').textContent = m.topic;
      renderAgents(m.agents);
    });
  } else {
    document.getElementById('liveTitle').textContent = meeting.topic;
    renderAgents(meeting.agents);
  }
  if (eventSource) eventSource.close();
  eventSource = new EventSource(`/api/meetings/${meetingId}/events`);
  eventSource.addEventListener('meeting_started', e => {
    const d = JSON.parse(e.data);
    document.getElementById('liveOutput').textContent += `\n${'═'.repeat(40)}\n会议开始: ${d.topic}\n参会: ${d.agents.join(', ')}\n${'═'.repeat(40)}\n`;
    document.getElementById('liveStatus').innerHTML = '<span class="badge badge-in_progress">运行中</span>';
  });
  eventSource.addEventListener('agent_start', e => {
    const d = JSON.parse(e.data);
    document.getElementById('liveOutput').textContent += `\n[${d.round}轮] ${d.agent_name} (${d.runtime}) 开始发言...\n`;
    updateAgentStatus(d.agent_id, 'running', d.agent_name);
    document.getElementById('liveOutput').scrollTop = document.getElementById('liveOutput').scrollHeight;
  });
  eventSource.addEventListener('agent_complete', e => {
    const d = JSON.parse(e.data);
    document.getElementById('liveOutput').textContent += `\n${d.agent_name} 发言完成 (${d.output_length}字符)\n${'─'.repeat(40)}\n`;
    updateAgentStatus(d.agent_id, 'completed', d.agent_name);
    document.getElementById('liveOutput').scrollTop = document.getElementById('liveOutput').scrollHeight;
  });
  eventSource.addEventListener('agent_error', e => {
    const d = JSON.parse(e.data);
    document.getElementById('liveOutput').textContent += `\n[错误] ${d.agent_name}: ${d.error}\n${'─'.repeat(40)}\n`;
    updateAgentStatus(d.agent_id, 'error', d.agent_name);
    document.getElementById('liveOutput').scrollTop = document.getElementById('liveOutput').scrollHeight;
  });
  eventSource.addEventListener('round_start', e => {
    const d = JSON.parse(e.data);
    document.getElementById('liveOutput').textContent += `\n\n${'═'.repeat(40)}\n第 ${d.round} 轮讨论开始\n${'═'.repeat(40)}\n`;
  });
  eventSource.addEventListener('round_complete', e => {
    const d = JSON.parse(e.data);
    document.getElementById('liveOutput').textContent += `第 ${d.round} 轮完成: ${d.completed_agents}成功, ${d.error_agents}失败\n`;
  });
  eventSource.addEventListener('meeting_complete', e => {
    const d = JSON.parse(e.data);
    document.getElementById('liveStatus').innerHTML = `<span class="badge ${d.status==='completed'?'badge-completed':'badge-cancelled'}">会议${d.status==='completed'?'已完成':'已停止'}</span>`;
    showToast(d.status === 'completed' ? '会议已完成' : '会议已停止');
    eventSource.close();
    refreshMeetingList();
  });
  refreshMeetingList();
}

function renderAgents(agents) {
  let html = '<div style="display:flex;gap:8px;flex-wrap:wrap;">';
  agents.forEach(a => {
    const statusColor = a.status === 'running' ? 'var(--accent)' : a.status === 'completed' ? 'var(--green)' : a.status === 'error' ? 'var(--red)' : 'var(--text-dim)';
    const statusLabel = a.status === 'running' ? '发言中' : a.status === 'completed' ? '已完成' : a.status === 'error' ? '出错' : '等待中';
    html += `<div style="padding:8px 12px;border:1px solid var(--border);border-radius:6px;background:var(--surface);min-width:120px;">
      <div style="font-weight:600;color:${statusColor};">${a.name}</div>
      <div style="font-size:12px;color:var(--text-dim);">${a.runtime} · ${a.role||''}</div>
      <div style="font-size:12px;color:${statusColor};margin-top:4px;">${statusLabel}</div>
    </div>`;
  });
  html += '</div>';
  document.getElementById('liveAgents').innerHTML = html;
}

function updateAgentStatus(agentId, status, name) {
  if (!currentMeetingId) return;
  apiFetch(`/api/meetings/${currentMeetingId}`).then(m => renderAgents(m.agents));
}

async function loadSharedFile() {
  if (!currentMeetingId) return;
  try {
    const data = await apiFetch(`/api/meetings/${currentMeetingId}/file`);
    const preview = document.getElementById('sharedFilePreview');
    preview.textContent = data.content || '(空)';
    preview.style.display = preview.style.display === 'none' ? 'block' : 'none';
  } catch(e) {}
}

/* ── Init ───────────────────────────────── */
loadDefaultAgents();
refreshMeetingList();
</script>
"""
    return layout("会议协作", body, active="meeting")
