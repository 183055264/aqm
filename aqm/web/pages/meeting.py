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
    <input id="mtTopic" type="text" placeholder="例如：支付模块设计评审" style="width:100%;">
  </div>
  <div class="form-group">
    <label>议题描述</label>
    <textarea id="mtDesc" rows="4" placeholder="项目背景、讨论范围、关键问题..." style="width:100%;"></textarea>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;">
    <div class="form-group">
      <label>共享文件名</label>
      <input id="mtFile" type="text" value="meeting.md" style="width:100%;">
      <div style="font-size:12px;color:var(--text-dim);margin-top:4px;">每个会议独立存放于 .aqm/meetings/&lt;会议ID&gt;/ 下，同名文件不会互相覆盖。</div>
    </div>
    <div class="form-group">
      <label>讨论轮数</label>
      <input id="mtRounds" type="number" value="1" min="1" max="10" style="width:100%;">
    </div>
    <div class="form-group">
      <label>无输出超时(秒)</label>
      <input id="mtTimeout" type="number" value="300" min="30" max="3600" style="width:100%;">
      <div style="font-size:12px;color:var(--text-dim);margin-top:4px;">只有连续这么久没有新的思考/输出才判定模型卡死；正常持续输出不会被打断。</div>
    </div>
  </div>

  <div style="margin-top:16px;">
    <label style="font-weight:600;font-size:13px;color:var(--text-dim);">参会代理</label>
    <div id="agentList" style="margin-top:8px;"></div>
    <button class="btn btn-sm" onclick="addAgentRow()" style="margin-top:8px;">+ 添加代理</button>
    <div style="font-size:12px;color:var(--text-dim);margin-top:6px;">提示：把「人类（我）」放在第一位即可由你主持；会议运行中也可以随时插话。</div>
  </div>

  <div style="margin-top:16px;display:flex;gap:8px;flex-wrap:wrap;">
    <button class="btn btn-primary" id="btnSaveMeeting" onclick="createAndStartMeeting()">创建并开始</button>
    <button class="btn" onclick="loadDefaultAgents()">加载默认配置</button>
    <button class="btn" id="btnCancelEdit" style="display:none;color:var(--red);" onclick="cancelEditMeeting()">取消编辑</button>
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
    <div id="liveOutput" style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:12px;height:70vh;min-height:420px;max-height:800px;overflow-y:auto;font-size:15px;line-height:1.6;white-space:pre-wrap;word-break:break-all;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;"></div>
    <div id="humanSpeak" class="human-speak" style="display:none;">
      <div class="human-speak-head">
        <div class="human-speak-avatar">我</div>
        <div class="human-speak-title">我的发言</div>
        <span class="human-turn-dot" title="轮到你发言"></span>
        <span id="humanWaitHint" class="human-speak-hint">会议运行中可随时插话，后续成员会看到</span>
      </div>
      <textarea id="humanSpeech" rows="3" placeholder="随时插话：纠正方向、补充问题、给出结论……（Enter 发送，Shift+Enter 换行）"></textarea>
      <div class="human-speak-foot">
        <button class="btn btn-primary btn-sm" id="btnSendHuman" onclick="sendHumanSpeech()">发送发言</button>
        <button class="btn btn-sm" id="btnSkipHuman" style="display:none;" onclick="skipHumanTurn()">跳过我的本轮</button>
        <span class="human-speak-tip">发言会写入共享文件；正在发言的成员本轮看不到，下一个成员会看到。</span>
        <span id="humanCharCount" class="human-speak-count">0 字</span>
      </div>
    </div>
    <div style="margin-top:12px;">
      <button class="btn btn-sm" onclick="loadSharedFile()">查看共享文件</button>
    </div>
    <pre id="sharedFilePreview" style="display:none;margin-top:8px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:12px;max-height:300px;overflow-y:auto;font-size:13px;line-height:1.5;white-space:pre-wrap;word-break:break-all;"></pre>
    <div id="requirementsSection" style="display:none;margin-top:18px;border-top:1px dashed var(--border);padding-top:16px;">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
          <h4 style="margin:0;color:var(--accent);">项目需求文档</h4>
          <div id="requirementsPath" style="font-size:12px;color:var(--text-dim);margin-top:4px;"></div>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
          <button class="btn btn-sm" id="btnGenerateReq" onclick="generateRequirements()">生成 / 重新生成</button>
          <button class="btn btn-sm" onclick="saveRequirements()">保存修改</button>
          <select id="handoffPipeline" title="转为开发任务时使用的流水线" onchange="updateHandoffPipelineStyle()"
                  style="padding:6px 10px;font-size:13px;border:1px solid var(--border);border-radius:6px;background:var(--surface2);color:var(--text-dim);max-width:220px;">
            <option value="">选择流水线</option>
          </select>
          <button class="btn btn-sm btn-primary" id="btnHandoff" onclick="handoffToTask()">转为开发任务</button>
        </div>
      </div>
      <div id="requirementsStatus" style="font-size:12px;color:var(--text-dim);margin-top:8px;"></div>
      <div id="meetingTasks" style="display:none;margin-top:10px;padding:10px 12px;border:1px solid var(--border);border-radius:8px;background:var(--surface2);font-size:12px;line-height:1.7;"></div>
      <textarea id="requirementsContent" rows="14" placeholder="会议完成后由主持人自动整理；也可以在这里直接编辑。" style="width:100%;margin-top:8px;background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;color:var(--text);font-size:13px;line-height:1.6;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;resize:vertical;"></textarea>
    </div>
  </div>
</div>

<style>
@keyframes agent-spin { to { transform: rotate(360deg); } }

/* ── Human speech composer ───────────────── */
.human-speak {
  position: relative;
  margin: 16px 0;
  padding: 14px 16px 12px;
  border: 1px solid var(--border);
  border-left: 4px solid var(--accent);
  border-radius: 10px;
  background: linear-gradient(180deg, rgba(88, 166, 255, 0.09), rgba(88, 166, 255, 0) 70%), var(--surface);
  transition: border-color .2s, box-shadow .2s;
}
.human-speak.is-turn {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(88, 166, 255, 0.14);
}
.human-speak-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.human-speak-avatar {
  width: 28px;
  height: 28px;
  flex: none;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--accent);
  color: #fff;
  font-size: 13px;
  font-weight: 700;
  box-shadow: 0 2px 6px rgba(88, 166, 255, .35);
}
.human-speak-title {
  font-size: 14px;
  font-weight: 700;
  color: var(--text);
}
.human-turn-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--green);
  display: none;
  box-shadow: 0 0 0 3px rgba(63, 185, 80, .18);
}
.human-speak.is-turn .human-turn-dot {
  display: inline-block;
  animation: pulse 1.5s infinite;
}
.human-speak-hint {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-dim);
  text-align: right;
  line-height: 1.4;
}
.human-speak-hint.is-turn {
  color: var(--accent);
  font-weight: 600;
}
.human-speak textarea {
  width: 100%;
  min-height: 88px;
  max-height: 40vh;
  resize: vertical;
  background: var(--surface2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  color: var(--text);
  font-size: 14px;
  line-height: 1.6;
  font-family: inherit;
  transition: border-color .15s, box-shadow .15s;
}
.human-speak textarea::placeholder { color: var(--text-dim); }
.human-speak textarea:focus {
  border-color: var(--accent);
  outline: none;
  box-shadow: 0 0 0 3px rgba(88, 166, 255, .15);
}
.human-speak-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  flex-wrap: wrap;
}
.human-speak-tip {
  font-size: 12px;
  color: var(--text-dim);
  line-height: 1.4;
}
.human-speak-count {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.human-speak .btn:disabled {
  opacity: .5;
  cursor: not-allowed;
}

.live-block { border-left: 4px solid var(--border); border-radius: 4px; padding: 8px 12px; margin: 10px 0; }
.live-block .live-head { font-weight: 600; font-size: 13px; margin-bottom: 4px; }
.live-block .live-body { display: block; white-space: pre-wrap; }
.live-block .live-think { margin: 4px 0 6px; border: 1px dashed var(--border); border-radius: 4px; padding: 4px 8px; }
.live-block .live-think summary { cursor: pointer; font-size: 12px; color: var(--text-dim); user-select: none; }
.live-block .live-think pre { margin: 6px 0 0; font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; color: var(--text-dim); max-height: 320px; overflow-y: auto; }
.live-block .live-think.live-actions { border-style: solid; }
.live-block .live-think.live-actions pre { max-height: 200px; color: var(--text); }
.live-waiting { margin: 8px 0; font-size: 13px; color: var(--text-dim); opacity: .9; }
</style>
<script>
let currentMeetingId = null;
let eventSource = null;
let humanWaitingId = null;

/* ── Per-agent colors ───────────────────── */
const AGENT_COLORS = [
  '#e06c75', '#61afef', '#98c379', '#e5c07b', '#c678dd',
  '#56b6c2', '#d19a66', '#7f9cf5', '#f78c6c', '#4ec9b0'
];
const agentColorMap = {};
let agentColorSeq = 0;

function agentColor(agentId, name) {
  const key = agentId || ('name:' + (name || ''));
  if (!agentColorMap[key]) {
    agentColorMap[key] = AGENT_COLORS[agentColorSeq % AGENT_COLORS.length];
    agentColorSeq += 1;
  }
  return agentColorMap[key];
}

function seedAgentColors(agents) {
  (agents || []).forEach(a => {
    if (a && a.id && !agentColorMap[a.id]) {
      agentColorMap[a.id] = AGENT_COLORS[agentColorSeq % AGENT_COLORS.length];
      agentColorSeq += 1;
    }
  });
}

function agentLabel(name, runtime) {
  const n = String(name || '');
  const r = runtime === 'human' ? '人类' : String(runtime || '');
  if (!r || r.toLowerCase() === n.toLowerCase()) return n;
  return `${n}（${r}）`;
}

function colorTint(hex, alpha) {
  const m = /^#([0-9a-f]{6})$/i.exec(hex || '');
  if (!m) return 'var(--surface)';
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

/* ── Agent Rows ─────────────────────────── */
function escapeValue(value) {
  return String(value || '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

let agentRowSeq = 0;
const modelCatalog = {};
const modelCatalogRequests = {};

function fetchRuntimeModels(runtime) {
  if (!runtime) return Promise.resolve(null);
  if (modelCatalog[runtime]) return Promise.resolve(modelCatalog[runtime]);
  if (modelCatalogRequests[runtime]) return modelCatalogRequests[runtime];
  const req = apiFetch(`/api/meetings/models?runtime=${encodeURIComponent(runtime)}`)
    .then(data => { modelCatalog[runtime] = data || {}; return modelCatalog[runtime]; })
    .catch(() => { modelCatalog[runtime] = { models: [] }; return modelCatalog[runtime]; })
    .finally(() => { delete modelCatalogRequests[runtime]; });
  modelCatalogRequests[runtime] = req;
  return req;
}

async function refreshModelOptions(row) {
  const select = row.querySelector('.ag-runtime');
  const input = row.querySelector('.ag-model');
  const list = row.querySelector('.ag-model-list');
  if (!select || !input || !list) return;
  const runtime = select.value;
  if (runtime === 'human') return;
  const data = await fetchRuntimeModels(runtime);
  // The runtime may have changed while the catalog was loading.
  const current = row.querySelector('.ag-runtime');
  if (!current || current.value !== runtime) return;
  const models = (data && data.models) || [];
  list.innerHTML = models
    .map(m => `<option value="${escapeValue(m.value)}"></option>`)
    .join('');
  const def = (data && data.default) || '';
  input.placeholder = models.length
    ? (def ? `默认：${def}` : '选择或输入模型')
    : '未发现候选，请手动输入';
  if (def) input.title = `默认：${def}`;
}

function addAgentRow(agent) {
  const list = document.getElementById('agentList');
  const row = document.createElement('div');
  const listId = `ag-model-list-${++agentRowSeq}`;
  const runtime = (agent && agent.runtime) || 'claude';
  row.style.cssText = 'display:grid;grid-template-columns:0.9fr 1.3fr 0.9fr 2fr auto;gap:8px;margin-bottom:8px;align-items:start;';
  row.dataset.modelRuntime = runtime;
  row.innerHTML = `
    <div class="form-group" style="margin-bottom:0;">
      <label style="font-size:12px;">运行时 *</label>
      <select class="ag-runtime" style="width:100%;" onchange="toggleAgentRuntimeFields(this)">
        <option value="claude" ${(agent&&agent.runtime==='claude')?'selected':''}>Claude</option>
        <option value="opencode" ${(agent&&agent.runtime==='opencode')?'selected':''}>opencode</option>
        <option value="kilo" ${(agent&&agent.runtime==='kilo')?'selected':''}>kilo</option>
        <option value="cline" ${(agent&&agent.runtime==='cline')?'selected':''}>cline</option>
        <option value="human" ${(agent&&agent.runtime==='human')?'selected':''}>人类（我）</option>
      </select>
    </div>
    <div class="form-group ag-model-group" style="margin-bottom:0;">
      <label style="font-size:12px;">模型</label>
      <input class="ag-model" type="text" list="${listId}" value="${escapeValue(agent&&agent.model)}" placeholder="选择或输入模型" style="width:100%;">
      <datalist class="ag-model-list" id="${listId}"></datalist>
    </div>
    <div class="form-group" style="margin-bottom:0;">
      <label style="font-size:12px;">角色</label>
      <input class="ag-role" type="text" value="${escapeValue(agent&&agent.role)}" placeholder="架构师" style="width:100%;">
    </div>
    <div class="form-group" style="margin-bottom:0;">
      <label style="font-size:12px;">系统提示词</label>
      <textarea class="ag-prompt" rows="2" placeholder="你是..." style="width:100%;font-size:12px;">${escapeValue(agent&&agent.system_prompt)}</textarea>
    </div>
    <button type="button" class="btn btn-sm" style="color:var(--red);height:38px;align-self:end;" onclick="this.closest('#agentList > div').remove()">&#10005;</button>
  `;
  list.appendChild(row);
  applyRuntimeFields(row);
  if (runtime !== 'human') refreshModelOptions(row);
}

function applyRuntimeFields(row) {
  const runtime = row.querySelector('.ag-runtime').value;
  const modelGroup = row.querySelector('.ag-model-group');
  if (modelGroup) {
    modelGroup.style.visibility = runtime === 'human' ? 'hidden' : 'visible';
  }
  const input = row.querySelector('.ag-model');
  if (runtime === 'human' && input) input.value = '';
}

function toggleAgentRuntimeFields(runtimeSelect) {
  const row = runtimeSelect.closest('#agentList > div');
  if (!row) return;
  const previous = row.dataset.modelRuntime || '';
  if (previous && previous !== runtimeSelect.value) {
    const input = row.querySelector('.ag-model');
    if (input) input.value = '';
  }
  row.dataset.modelRuntime = runtimeSelect.value;
  applyRuntimeFields(row);
  refreshModelOptions(row);
}

async function loadDefaultAgents() {
  try {
    const data = await apiFetch('/api/meetings/default-agents');
    document.getElementById('agentList').innerHTML = '';
    data.forEach(a => addAgentRow(a));
    showToast('已加载默认代理配置');
  } catch(e) { showToast('加载失败', 'error'); }
}

/* ── Create & Start ─────────────────────── */
async function createAndStartMeeting() {
  const topic = document.getElementById('mtTopic').value.trim();
  if (!topic) { showToast('请输入会议主题', 'error'); return; }

  const agents = [];
  const runtimeCounts = {};
  document.querySelectorAll('#agentList > div').forEach(row => {
    const runtime = row.querySelector('.ag-runtime').value;
    runtimeCounts[runtime] = (runtimeCounts[runtime] || 0) + 1;
    const id = runtimeCounts[runtime] === 1
      ? runtime
      : `${runtime}-${runtimeCounts[runtime]}`;
    const role = row.querySelector('.ag-role').value.trim();
    const prompt = row.querySelector('.ag-prompt').value.trim();
    const modelInput = row.querySelector('.ag-model');
    agents.push({
      id,
      name: role || id,
      runtime,
      role,
      system_prompt: prompt,
      model: runtime === 'human' ? '' : (modelInput ? modelInput.value.trim() : ''),
      directory_access: runtime === 'claude' ? 'project_read_write' : '',
    });
  });

  if (!agents.length) { showToast('请至少添加一个代理', 'error'); return; }

  const form = {
    topic: topic,
    description: document.getElementById('mtDesc').value.trim(),
    agents: agents,
    shared_file: document.getElementById('mtFile').value.trim() || 'meeting.md',
    rounds: parseInt(document.getElementById('mtRounds').value) || 1,
    timeout_per_agent: parseInt(document.getElementById('mtTimeout').value) || 300,
  };

  try {
    if (editingMeetingId) {
      // Update existing meeting
      const data = await apiFetch(`/api/meetings/${editingMeetingId}`, {
        method: 'PATCH',
        body: JSON.stringify(form),
      });
      currentMeetingId = data.id;
      editingMeetingId = null;
      document.getElementById('btnSaveMeeting').textContent = '创建并开始';
      document.getElementById('btnCancelEdit').style.display = 'none';
      await startMeeting(currentMeetingId);
      showToast('会议已更新并继续');
    } else {
      // Create new meeting
      const data = await apiFetch('/api/meetings', {
        method: 'POST',
        body: JSON.stringify(form),
      });
      currentMeetingId = data.id;
      await startMeeting(currentMeetingId);
      showToast('会议已创建并开始');
    }
  } catch(e) {
    showToast('操作失败: ' + e.message, 'error');
  }
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
    // Update UI immediately (badge + agents + list) without waiting for SSE/refresh
    const m = await apiFetch(`/api/meetings/${currentMeetingId}`).catch(() => null);
    if (m) {
      renderAgents(m.agents);
      document.getElementById('liveStatus').innerHTML = '<span class="badge badge-cancelled">已停止</span>';
    }
    refreshMeetingList();
  } catch(e) { showToast('停止失败', 'error'); }
}

/* ── Meeting List ───────────────────────── */
let editingMeetingId = null;
let meetingDirMap = {};  // id -> project_dir

async function switchServerProject(path) {
  // Switch the server's default project and restart so the sidebar follows.
  try {
    const res = await fetch('/api/project/switch', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({path: path})});
    const data = await res.json();
    if (!res.ok) {
      showToast(data.detail || '切换失败', 'error');
      return false;
    }
    showToast('正在切换到 ' + path + ' 并重启服务器...', 'info');
    const res2 = await fetch('/api/project/restart', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: path})
    });
    if (res2.ok) {
      // Poll restart; on success the page reloads and the sidebar shows the new project
      if (typeof pollRestartStatus === 'function') pollRestartStatus(2000, 30);
    } else {
      const err = await res2.json().catch(() => ({}));
      showToast('重启失败: ' + (err.detail || '请手动重启服务器'), 'error');
    }
    return true;
  } catch(e) {
    showToast('切换失败: ' + e.message, 'error');
    return false;
  }
}

function sameProjectPath(a, b) {
  const norm = p => String(p || '').replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
  return norm(a) === norm(b);
}

function ensureProjectForMeeting(m) {
  // Meeting endpoints resolve the meeting's own project directory, so editing,
  // starting or continuing a meeting from another project needs no server
  // restart. Restarting here used to kill the port mid-request and surface as
  // "Failed to fetch".
  if (!m.project_dir) return Promise.resolve(true);
  return fetch('/api/project').then(r => r.json()).then(data => {
    if (!sameProjectPath(data.current, m.project_dir)) {
      showToast(`该会议属于项目 ${m.project_dir}，本次操作将直接在该项目目录下进行`, 'info');
    }
    return true;
  }).catch(() => true);
}

async function refreshMeetingList() {
  try {
    const meetings = await apiFetch('/api/meetings');
    meetings.forEach(m => { meetingDirMap[m.id] = m.project_dir || null; });
    const container = document.getElementById('meetingList');
    if (!meetings.length) {
      container.innerHTML = '<p style="color:var(--text-dim);text-align:center;padding:16px;">暂无会议记录</p>';
      return;
    }
    let html = '<div class="card"><h3>会议记录</h3><div class="table-wrap"><table><thead><tr><th>主题</th><th>状态</th><th>代理数</th><th>轮数</th><th>目录</th><th>创建时间</th><th>操作</th></tr></thead><tbody>';
    meetings.forEach(m => {
      const statusBadge = m.status === 'running' ? '<span class="badge badge-in_progress">运行中</span>' :
                          m.status === 'completed' ? '<span class="badge badge-completed">已完成</span>' :
                          m.status === 'stopped' ? '<span class="badge badge-cancelled">已停止</span>' :
                          m.status === 'error' ? '<span class="badge badge-failed">失败</span>' :
                          '<span class="badge badge-pending">待开始</span>';
      const startLabel = m.status === 'completed' ? '重新运行' :
                         m.status === 'stopped' ? '继续' :
                         m.status === 'error' ? '重试' : '启动';
      let actions;
      if (m.status === 'running') {
        actions = `<button class="btn btn-sm" onclick="viewMeeting('${m.id}')">查看</button>
                   <button class="btn btn-sm btn-red" onclick="stopCurrentMeeting2('${m.id}')">停止</button>`;
      } else {
        let mainBtns = '';
        if (m.status === 'completed') {
          mainBtns = `<button class="btn btn-sm" onclick="startMeeting2('${m.id}')">重新运行</button>
                      <button class="btn btn-sm" onclick="continueMeeting2('${m.id}')">继续讨论</button>
                      <button class="btn btn-sm" onclick="openRequirements('${m.id}')">需求 / 转任务</button>`;
        } else if (m.status === 'stopped') {
          mainBtns = `<button class="btn btn-sm" onclick="continueMeeting2('${m.id}')">继续</button>
                      <button class="btn btn-sm" onclick="openRequirements('${m.id}')">需求 / 转任务</button>`;
        } else if (m.status === 'error') {
          mainBtns = `<button class="btn btn-sm" onclick="startMeeting2('${m.id}')">重试</button>
                      <button class="btn btn-sm" onclick="continueMeeting2('${m.id}')">继续</button>`;
        } else {
          mainBtns = `<button class="btn btn-sm" onclick="startMeeting2('${m.id}')">启动</button>`;
        }
        actions = `${mainBtns}
          <button class="btn btn-sm" onclick="editMeeting('${m.id}')">编辑</button>
          <button class="btn btn-sm" onclick="viewMeeting('${m.id}')">查看</button>
          <button class="btn btn-sm" onclick="reuseMeeting('${m.id}')">复用配置</button>
          <button class="btn btn-sm" style="color:var(--red);" onclick="deleteMeeting2('${m.id}')">删除</button>`;
      }
      const dirName = m.project_dir ? m.project_dir.split(/[/\\]/).filter(Boolean).pop() : '-';
      const dirFull = m.project_dir || '';
      const createdStr = m.created_at ? m.created_at.substring(0,16).replace('T',' ') : '-';
      html += `<tr><td><strong>${m.topic}</strong></td><td>${statusBadge}</td><td>${m.agents.length}</td><td>${m.current_round}/${m.rounds}</td><td title="${dirFull}" style="font-size:12px;color:var(--text-dim);max-width:120px;overflow:hidden;text-overflow:ellipsis;">${dirName}</td><td>${createdStr}</td><td>${actions}</td></tr>`;
    });
    html += '</tbody></table></div></div>';
    container.innerHTML = html;
  } catch(e) {}
}

async function editMeeting(id) {
  try {
    // Switch project if needed
    const m0 = await apiFetch(`/api/meetings/${id}`);
    if (m0.project_dir && !sameProjectPath(m0.project_dir, (await fetch('/api/project').then(r=>r.json())).current)) {
      const ok = await ensureProjectForMeeting(m0);
      if (!ok) return;
    }
    const m = await apiFetch(`/api/meetings/${id}`);
    document.getElementById('mtTopic').value = m.topic || '';
    document.getElementById('mtDesc').value = m.description || '';
    document.getElementById('mtFile').value = m.shared_file || 'meeting.md';
    document.getElementById('mtRounds').value = m.rounds || 1;
    document.getElementById('mtTimeout').value = m.timeout_per_agent || 300;
    document.getElementById('agentList').innerHTML = '';
    (m.agents || []).forEach(a => addAgentRow(a));
    editingMeetingId = id;
    document.getElementById('btnSaveMeeting').textContent = '保存并继续';
    document.getElementById('btnCancelEdit').style.display = '';
    document.getElementById('mtTopic').scrollIntoView({behavior:'smooth', block:'center'});
    showToast('已加载会议设置，修改后点击"保存并继续"', 'info');
  } catch(e) { showToast('加载失败: ' + e.message, 'error'); }
}

function cancelEditMeeting() {
  editingMeetingId = null;
  document.getElementById('btnSaveMeeting').textContent = '创建并开始';
  document.getElementById('btnCancelEdit').style.display = 'none';
  loadDefaultAgents();
  showToast('已取消编辑，恢复默认配置', 'info');
}

function stopCurrentMeeting2(id) {
  currentMeetingId = id;
  stopCurrentMeeting();
}

async function startMeeting2(id) {
  currentMeetingId = id;
  // Switch project if needed
  try {
    const m0 = await apiFetch(`/api/meetings/${id}`);
    if (m0.project_dir && !sameProjectPath(m0.project_dir, (await fetch('/api/project').then(r=>r.json())).current)) {
      const ok = await ensureProjectForMeeting(m0);
      if (!ok) return;
    }
  } catch(e) {}
  startMeeting(id);
}

async function continueMeeting2(id) {
  currentMeetingId = id;
  let additional = 0;
  try {
    const m0 = await apiFetch(`/api/meetings/${id}`);
    if (m0.project_dir && !sameProjectPath(m0.project_dir, (await fetch('/api/project').then(r=>r.json())).current)) {
      const ok = await ensureProjectForMeeting(m0);
      if (!ok) return;
    }
    if (m0.status === 'completed') {
      const input = prompt(`继续讨论：当前已完成 ${m0.current_round}/${m0.rounds} 轮，新增几轮？`, '1');
      if (input === null) return;
      additional = Math.max(1, parseInt(input) || 1);
    }
  } catch(e) { showToast('加载会议失败: ' + e.message, 'error'); return; }
  try {
    await apiFetch(`/api/meetings/${id}/continue`, {
      method: 'POST',
      body: JSON.stringify({additional_rounds: additional}),
    });
    showLiveView(id);
    showToast('会议已继续');
  } catch(e) { showToast('继续失败: ' + e.message, 'error'); }
}

async function reuseMeeting(id) {
  try {
    const m = await apiFetch(`/api/meetings/${id}`);
    document.getElementById('mtTopic').value = '';
    document.getElementById('mtDesc').value = m.description || '';
    document.getElementById('mtFile').value = m.shared_file || 'meeting.md';
    document.getElementById('mtRounds').value = 1;
    document.getElementById('mtTimeout').value = m.timeout_per_agent || 300;
    document.getElementById('agentList').innerHTML = '';
    (m.agents || []).forEach(a => addAgentRow(a));
    editingMeetingId = null;
    document.getElementById('btnSaveMeeting').textContent = '创建并开始';
    document.getElementById('btnCancelEdit').style.display = 'none';
    document.getElementById('mtTopic').scrollIntoView({behavior:'smooth', block:'center'});
    showToast(`已复用「${m.topic}」的参会配置，请填写新主题`, 'info');
  } catch(e) { showToast('加载失败: ' + e.message, 'error'); }
}

async function openRequirements(id) {
  await viewMeeting(id);
  const sec = document.getElementById('requirementsSection');
  if (sec) sec.scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function viewMeeting(id) {
  currentMeetingId = id;
  const m = await apiFetch(`/api/meetings/${id}`);
  // Viewing works in the meeting's own project; just inform if it differs
  try {
    const cur = (await fetch('/api/project').then(r=>r.json())).current;
    if (m.project_dir && m.project_dir !== cur) {
      showToast(`该会议属于项目 "${m.project_dir}"，已在该项目目录下读取`, 'info');
    }
  } catch(e) {}
  showLiveView(id, m);
  const file = await apiFetch(`/api/meetings/${id}/file`);
  document.getElementById('sharedFilePreview').textContent =
    (file.path ? `文件: ${file.path}\n\n` : '') + (file.content || '(空)');
  document.getElementById('sharedFilePreview').style.display = 'block';
}

async function deleteMeeting2(id) {
  if (!confirm('确定删除此会议？')) return;
  // Switch project if needed
  try {
    const m0 = await apiFetch(`/api/meetings/${id}`);
    if (m0.project_dir && m0.project_dir !== (await fetch('/api/project').then(r=>r.json())).current) {
      const ok = await ensureProjectForMeeting(m0);
      if (!ok) return;
    }
  } catch(e) {}
  try {
    await apiFetch(`/api/meetings/${id}`, {method: 'DELETE'});
    showToast('已删除');
    refreshMeetingList();
  } catch(e) { showToast('删除失败: ' + e.message, 'error'); }
}

/* ── Typewriter queue ─────────────────────── */
let _typeQueue = [];
let _typing = false;
let liveWaitingEl = null;
let liveWaitingStart = 0;
let liveWaitingLabel = '';

function showLiveWaiting(label, startedAt) {
  const out = document.getElementById('liveOutput');
  if (!out) return;
  if (!liveWaitingEl) {
    liveWaitingEl = document.createElement('div');
    liveWaitingEl.className = 'live-waiting';
  }
  liveWaitingLabel = label || '成员';
  const parsed = startedAt ? Date.parse(startedAt) : NaN;
  liveWaitingStart = isNaN(parsed) ? Date.now() : parsed;
  out.appendChild(liveWaitingEl);
  updateLiveWaiting();
}

function updateLiveWaiting() {
  if (!liveWaitingEl) return;
  const secs = Math.max(0, Math.round((Date.now() - liveWaitingStart) / 1000));
  liveWaitingEl.textContent = `⏳ ${liveWaitingLabel} 思考中... ${secs}s`;
  const out = document.getElementById('liveOutput');
  if (out && out.scrollHeight - out.scrollTop - out.clientHeight < 80) {
    out.scrollTop = out.scrollHeight;
  }
}

function clearLiveWaiting() {
  if (liveWaitingEl && liveWaitingEl.parentNode) {
    liveWaitingEl.parentNode.removeChild(liveWaitingEl);
  }
  liveWaitingEl = null;
}

/* ── Live streaming blocks ───────────────── */
const streamBlocks = {};

function scrollLiveIfNearBottom() {
  const out = document.getElementById('liveOutput');
  if (!out) return;
  if (out.scrollHeight - out.scrollTop - out.clientHeight < 120) {
    out.scrollTop = out.scrollHeight;
  }
}

function createStreamBlock(d) {
  const out = document.getElementById('liveOutput');
  const color = agentColor(d.agent_id, d.agent_name);
  const block = document.createElement('div');
  block.className = 'live-block live-streaming';
  block.style.borderLeftColor = color;
  block.style.background = colorTint(color, 0.08);

  const head = document.createElement('div');
  head.className = 'live-head';
  head.style.color = color;
  head.textContent = `${agentLabel(d.agent_name, d.runtime)} · 第${d.round}轮 · 生成中…`;
  block.appendChild(head);

  const actions = document.createElement('details');
  actions.className = 'live-think live-actions';
  actions.style.display = 'none';
  const actionsSummary = document.createElement('summary');
  actionsSummary.textContent = '操作记录（0 条）';
  const actionsPre = document.createElement('pre');
  actions.appendChild(actionsSummary);
  actions.appendChild(actionsPre);
  block.appendChild(actions);

  const details = document.createElement('details');
  details.className = 'live-think';
  details.style.display = 'none';
  const summary = document.createElement('summary');
  summary.textContent = '思考过程（0 字）';
  const pre = document.createElement('pre');
  details.appendChild(summary);
  details.appendChild(pre);
  block.appendChild(details);

  const body = document.createElement('span');
  body.className = 'live-body';
  block.appendChild(body);

  if (liveWaitingEl && liveWaitingEl.parentNode === out) {
    out.insertBefore(block, liveWaitingEl);
  } else {
    out.appendChild(block);
  }
  const state = {
    el: block, head, body, details, summary, pre,
    actions, actionsSummary, actionsPre, actionCount: 0,
    thinkingLen: 0, meta: d
  };
  streamBlocks[`${d.agent_id}:${d.round}`] = state;
  scrollLiveIfNearBottom();
  return state;
}

function finalizeStreamBlock(d, headText) {
  const key = `${d.agent_id}:${d.round}`;
  const state = streamBlocks[key];
  if (!state) return false;
  state.head.textContent = headText;
  if (typeof d.output === 'string') state.body.textContent = d.output;
  const actions = d.actions || state.actionsPre.textContent || '';
  if (actions) {
    state.actions.style.display = '';
    state.actionsPre.textContent = actions;
    const count = actions.split('\n').filter(line => line.trim()).length;
    state.actionsSummary.textContent = `操作记录（${count} 条）`;
  }
  const thinking = d.thinking || '';
  if (thinking) {
    state.details.style.display = '';
    state.summary.textContent = `思考过程（${thinking.length} 字）`;
    state.pre.textContent = thinking;
  }
  delete streamBlocks[key];
  scrollLiveIfNearBottom();
  return true;
}

function dropStreamBlocks() {
  Object.keys(streamBlocks).forEach(key => {
    const state = streamBlocks[key];
    if (state && state.el && state.el.parentNode) {
      state.el.parentNode.removeChild(state.el);
    }
    delete streamBlocks[key];
  });
}

function dropStreamBlock(agentId, round) {
  const key = `${agentId}:${round}`;
  const state = streamBlocks[key];
  if (state && state.el && state.el.parentNode) {
    state.el.parentNode.removeChild(state.el);
  }
  delete streamBlocks[key];
}

function _typeNext() {
  if (_typing) return;
  if (!_typeQueue.length) { _typing = false; return; }
  _typing = true;
  const task = _typeQueue.shift();
  const out = document.getElementById('liveOutput');
  let target;
  let head = null;
  if (task.block) {
    const block = document.createElement('div');
    block.className = 'live-block';
    const color = task.color || 'var(--border)';
    block.style.borderLeftColor = color;
    block.style.background = task.tint || colorTint(task.color, 0.08);
    if (task.head) {
      head = document.createElement('div');
      head.className = 'live-head';
      head.style.color = color;
      block.appendChild(head);
      head.textContent = '';
    }
    if (task.actions) {
      const aDetails = document.createElement('details');
      aDetails.className = 'live-think live-actions';
      const aSummary = document.createElement('summary');
      const count = String(task.actions).split('\n').filter(line => line.trim()).length;
      aSummary.textContent = `操作记录（${count} 条）`;
      const aPre = document.createElement('pre');
      aPre.textContent = task.actions;
      aDetails.appendChild(aSummary);
      aDetails.appendChild(aPre);
      block.appendChild(aDetails);
    }
    if (task.thinking) {
      const details = document.createElement('details');
      details.className = 'live-think';
      const summary = document.createElement('summary');
      summary.textContent = `思考过程（${task.thinking.length} 字）`;
      const pre = document.createElement('pre');
      pre.textContent = task.thinking;
      details.appendChild(summary);
      details.appendChild(pre);
      block.appendChild(details);
    }
    target = document.createElement('span');
    target.className = 'live-body';
    if (task.textColor) target.style.color = task.textColor;
    block.appendChild(target);
    out.appendChild(block);
  } else {
    target = document.createElement('span');
    if (task.color) target.style.color = task.color;
    out.appendChild(target);
  }
  if (liveWaitingEl) out.appendChild(liveWaitingEl);
  const text = String(task.text || '');
  const headText = String(task.head || '');
  const total = text.length;
  const step = Math.max(8, Math.floor(total / 200));
  let idx = 0;
  function tick() {
    idx = Math.min(total, idx + step);
    target.textContent += text.slice(Math.max(0, idx - step), idx);
    if (head && idx >= Math.min(headText.length, step)) {
      // The header is short; reveal it with the first chunk of the body.
      head.textContent = headText;
    }
    out.scrollTop = out.scrollHeight;
    if (idx >= total) {
      if (head) head.textContent = headText;
      _typing = false;
      _typeNext();
      return;
    }
    setTimeout(tick, 16);
  }
  tick();
}

function _enqueueTyping(text, options) {
  const task = { text: text };
  if (options) Object.assign(task, options);
  _typeQueue.push(task);
  _typeNext();
}

/* ── Human speech ───────────────────────── */
function updateHumanCharCount() {
  const el = document.getElementById('humanSpeech');
  const counter = document.getElementById('humanCharCount');
  if (!el || !counter) return;
  const len = (el.value || '').length;
  counter.textContent = len ? `${len} 字` : '0 字';
}

function setHumanTurnActive(name) {
  const panel = document.getElementById('humanSpeak');
  const hint = document.getElementById('humanWaitHint');
  const skip = document.getElementById('btnSkipHuman');
  const el = document.getElementById('humanSpeech');
  if (!panel) return;
  if (name) {
    panel.classList.add('is-turn');
    if (hint) {
      hint.textContent = `轮到你（${name}）发言，请输入后发送`;
      hint.classList.add('is-turn');
    }
    if (skip) skip.style.display = '';
    if (el) el.placeholder = `轮到你（${name}）发言：输入后发送，或点击“跳过我的本轮”`;
  } else {
    panel.classList.remove('is-turn');
    if (hint) {
      hint.textContent = '会议运行中可随时插话，后续成员会看到';
      hint.classList.remove('is-turn');
    }
    if (skip) skip.style.display = 'none';
    if (el) el.placeholder = '随时插话：纠正方向、补充问题、给出结论……';
  }
}

function updateHumanSpeakPanel(meeting) {
  const panel = document.getElementById('humanSpeak');
  if (!panel) return;
  const running = !!(meeting && meeting.status === 'running');
  panel.style.display = running ? 'block' : 'none';
  if (running && meeting.waiting_for_human) {
    humanWaitingId = meeting.waiting_for_human;
    setHumanTurnActive(meeting.waiting_human_name || '我');
  } else {
    humanWaitingId = null;
    setHumanTurnActive(null);
  }
}

async function sendHumanSpeech() {
  if (!currentMeetingId) return;
  const el = document.getElementById('humanSpeech');
  const btn = document.getElementById('btnSendHuman');
  const text = (el && el.value || '').trim();
  if (!text) { showToast('请输入发言内容', 'error'); return; }
  if (btn) { btn.disabled = true; btn.textContent = '发送中...'; }
  try {
    await apiFetch(`/api/meetings/${currentMeetingId}/speak`, {
      method: 'POST',
      body: JSON.stringify({ content: text, agent_id: humanWaitingId || '' }),
    });
    if (el) { el.value = ''; updateHumanCharCount(); }
    showToast('发言已发送');
  } catch(e) {
    showToast('发送失败: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '发送发言'; }
  }
}

async function skipHumanTurn() {
  if (!currentMeetingId || !humanWaitingId) return;
  try {
    await apiFetch(`/api/meetings/${currentMeetingId}/speak`, {
      method: 'POST',
      body: JSON.stringify({ skip: true, agent_id: humanWaitingId }),
    });
    humanWaitingId = null;
    document.getElementById('btnSkipHuman').style.display = 'none';
    showToast('已跳过本轮发言');
  } catch(e) { showToast('操作失败: ' + e.message, 'error'); }
}

document.addEventListener('keydown', function(event) {
  if (!event.ctrlKey || event.key !== 'Enter') return;
  const el = document.getElementById('humanSpeech');
  if (el && document.activeElement === el) {
    event.preventDefault();
    sendHumanSpeech();
  }
});

/* ── Live View ──────────────────────────── */
function showLiveView(meetingId, meeting) {
  document.getElementById('meetingLive').style.display = 'block';
  document.getElementById('sharedFilePreview').style.display = 'none';
  const reqSection = document.getElementById('requirementsSection');
  if (reqSection) reqSection.style.display = 'block';
  loadRequirements(meetingId);
  loadMeetingTasks(meetingId);
  if (!meeting) {
    apiFetch(`/api/meetings/${meetingId}`).then(m => {
      document.getElementById('liveTitle').textContent = m.topic;
      renderAgents(m.agents);
      seedLiveWaiting(m);
      updateHumanSpeakPanel(m);
    });
  } else {
    document.getElementById('liveTitle').textContent = meeting.topic;
    renderAgents(meeting.agents);
    seedLiveWaiting(meeting);
    updateHumanSpeakPanel(meeting);
  }
  if (eventSource) eventSource.close();
  eventSource = new EventSource(`/api/meetings/${meetingId}/events`);
  eventSource.addEventListener('meeting_started', e => {
    const d = JSON.parse(e.data);
    _enqueueTyping(`\n${'═'.repeat(40)}\n会议开始: ${d.topic}\n参会: ${d.agents.join(', ')}\n${'═'.repeat(40)}\n`);
    document.getElementById('liveStatus').innerHTML = '<span class="badge badge-in_progress">运行中</span>';
  });
  eventSource.addEventListener('agents_starting', () => {
    _enqueueTyping('\n正在并行启动所有成员的常驻会话...\n');
  });
  eventSource.addEventListener('agent_ready', e => {
    const d = JSON.parse(e.data);
    _enqueueTyping(`${agentLabel(d.agent_name, d.runtime)} 会话已就绪，等待发言\n`, {
      color: agentColor(d.agent_id, d.agent_name),
    });
    apiFetch(`/api/meetings/${meetingId}`, {quiet: true})
      .then(m => renderAgents(m.agents))
      .catch(() => {});
  });
  eventSource.addEventListener('agent_start', e => {
    const d = JSON.parse(e.data);
    _enqueueTyping(`\n[第 ${d.round} 轮] ${agentLabel(d.agent_name, d.runtime)} 开始发言…\n`, {
      color: agentColor(d.agent_id, d.agent_name),
    });
    showLiveWaiting(d.agent_name, d.started_at);
    updateAgentStatus(d.agent_id, 'running', d.agent_name);
  });
  eventSource.addEventListener('agent_stream', e => {
    const d = JSON.parse(e.data);
    const key = `${d.agent_id}:${d.round}`;
    let state = streamBlocks[key];
    if (!state) state = createStreamBlock(d);
    const chunk = d.chunk || '';
    if (d.kind === 'action') {
      state.actionCount += chunk.split('\n').filter(line => line.trim()).length;
      if (state.actions.style.display === 'none') {
        state.actions.style.display = '';
        state.actions.open = true;
      }
      state.actionsPre.textContent += chunk;
      state.actionsSummary.textContent = `操作记录（${state.actionCount} 条）`;
    } else if (d.kind === 'thinking') {
      state.thinkingLen += chunk.length;
      if (state.details.style.display === 'none') {
        state.details.style.display = '';
        state.details.open = true;
      }
      state.pre.textContent += chunk;
      state.summary.textContent = `思考过程（${state.thinkingLen} 字）`;
    } else if (d.kind === 'process') {
      // Prose the agent wrote before calling a tool is planning narration,
      // not the meeting speech. Move it out of the answer body and into the
      // collapsible process panel so the final speech stays clean.
      const narrations = String(chunk).split(/\n{2,}/).map(s => s.trim()).filter(Boolean);
      narrations.forEach(narration => {
        if (state.pre.textContent) state.pre.textContent += '\n\n';
        state.pre.textContent += narration;
        const bodyText = state.body.textContent.replace(/\s+$/, '');
        if (bodyText.endsWith(narration)) {
          state.body.textContent = bodyText.slice(0, bodyText.length - narration.length);
        }
      });
      if (narrations.length) {
        state.thinkingLen = state.pre.textContent.length;
        state.summary.textContent = `思考过程（${state.thinkingLen} 字）`;
        state.details.style.display = '';
      }
    } else {
      state.body.textContent += chunk;
    }
    scrollLiveIfNearBottom();
  });
  eventSource.addEventListener('agent_complete', e => {
    const d = JSON.parse(e.data);
    clearLiveWaiting();
    const color = agentColor(d.agent_id, d.agent_name);
    const headText = `${agentLabel(d.agent_name, d.runtime)} · 第${d.round}轮 · ${d.output_length} 字`;
    if (!finalizeStreamBlock(d, headText)) {
      _enqueueTyping(d.output || '(无内容)', {
        block: true,
        color: color,
        tint: colorTint(color, 0.08),
        head: headText,
        thinking: d.thinking || '',
        actions: d.actions || '',
      });
    }
    updateAgentStatus(d.agent_id, 'completed', d.agent_name);
  });
  eventSource.addEventListener('agent_error', e => {
    const d = JSON.parse(e.data);
    clearLiveWaiting();
    dropStreamBlock(d.agent_id, d.round);
    _enqueueTyping(d.error || '未知错误', {
      block: true,
      color: '#ff7b72',
      tint: 'rgba(255, 123, 114, 0.10)',
      head: `${d.agent_name} · 发言失败`,
    });
    updateAgentStatus(d.agent_id, 'error', d.agent_name);
  });
  eventSource.addEventListener('human_turn', e => {
    const d = JSON.parse(e.data);
    clearLiveWaiting();
    humanWaitingId = d.agent_id;
    _enqueueTyping(`\n[${d.round}轮] 等待 ${d.agent_name} 发言...\n`, {
      color: agentColor(d.agent_id, d.agent_name),
    });
    const hint = document.getElementById('humanWaitHint');
    const skip = document.getElementById('btnSkipHuman');
    setHumanTurnActive(d.agent_name || '我');
    if (hint) hint.textContent = `轮到你（${d.agent_name}）发言，请输入后发送`;
    if (skip) skip.style.display = '';
    document.getElementById('humanSpeak').style.display = 'block';
    const input = document.getElementById('humanSpeech');
    if (input) input.focus();
    updateAgentStatus(d.agent_id, 'awaiting_input', d.agent_name);
  });
  eventSource.addEventListener('human_message', e => {
    const d = JSON.parse(e.data);
    const color = agentColor(d.agent_id, d.agent_name);
    _enqueueTyping(d.output || '', {
      block: true,
      color: color,
      tint: colorTint(color, 0.10),
      head: `${d.agent_name}（人类）· 第${d.round}轮${d.waiting ? '' : ' · 插话'}`,
    });
    humanWaitingId = null;
    setHumanTurnActive(null);
    const hint = document.getElementById('humanWaitHint');
    if (hint) hint.textContent = '发言已写入共享记录，后续成员会看到';
    updateAgentStatus(d.agent_id, 'completed', d.agent_name);
  });
  eventSource.addEventListener('human_skipped', e => {
    const d = JSON.parse(e.data);
    _enqueueTyping(`\n[${d.round}轮] ${d.agent_name} 跳过了本轮发言\n`, {
      color: agentColor(d.agent_id, d.agent_name),
    });
    humanWaitingId = null;
    setHumanTurnActive(null);
    updateAgentStatus(d.agent_id, 'skipped', d.agent_name);
  });
  eventSource.addEventListener('round_start', e => {
    const d = JSON.parse(e.data);
    _enqueueTyping(`\n\n${'═'.repeat(40)}\n第 ${d.round} 轮讨论开始\n${'═'.repeat(40)}\n`);
  });
  eventSource.addEventListener('round_complete', e => {
    const d = JSON.parse(e.data);
    clearLiveWaiting();
    dropStreamBlocks();
    const spoke = d.completed_agents || 0;
    const failed = d.error_agents || 0;
    const skipped = d.skipped_agents || 0;
    let line = d.interrupted
      ? `第 ${d.round} 轮已中断`
      : `第 ${d.round} 轮完成: ${spoke} 人发言`;
    // Only mention failures/skips when they actually happened, so a fully
    // successful round never renders a confusing "0 成功" / "0 失败" tail.
    if (failed) line += `，${failed} 人失败`;
    if (skipped) line += `，${skipped} 人跳过`;
    _enqueueTyping(line + '\n');
  });
  eventSource.addEventListener('meeting_complete', e => {
    const d = JSON.parse(e.data);
    clearLiveWaiting();
    dropStreamBlocks();
    const failed = d.status === 'error';
    const label = failed ? '失败' : d.status === 'completed' ? '已完成' : '已停止';
    document.getElementById('liveStatus').innerHTML = `<span class="badge ${failed?'badge-failed':d.status==='completed'?'badge-completed':'badge-cancelled'}">会议${label}</span>`;
    showToast(failed ? `会议失败: ${d.error || '所有成员均未能发言'}` : d.status === 'completed' ? '会议已完成' : '会议已停止', failed ? 'error' : 'success');
    // Refresh agent cards so the last running agent no longer shows "思考中..."
    apiFetch(`/api/meetings/${meetingId}`, {quiet: true})
      .then(m => renderAgents(m.agents))
      .catch(() => {});
    humanWaitingId = null;
    const humanPanel = document.getElementById('humanSpeak');
    if (humanPanel) { humanPanel.style.display = 'none'; humanPanel.classList.remove('is-turn'); }
    eventSource.close();
    refreshMeetingList();
    const reqSection = document.getElementById('requirementsSection');
    if (reqSection) reqSection.style.display = 'block';
    loadRequirements(meetingId);
    loadMeetingTasks(meetingId);
    if (d.status === 'completed') {
      if (_requirementsPoll) clearInterval(_requirementsPoll);
      let tries = 0;
      _requirementsPoll = setInterval(async () => {
        tries++;
        const data = await loadRequirements(meetingId);
        const done = !data || data.status !== 'running';
        if ((done && tries > 7) || tries > 300) {
          clearInterval(_requirementsPoll);
          _requirementsPoll = null;
        }
      }, 2000);
    }
  });
  eventSource.addEventListener('requirements_started', () => {
    const statusEl = document.getElementById('requirementsStatus');
    if (statusEl) { statusEl.textContent = '主持人正在整理需求文档…'; statusEl.style.color = 'var(--text-dim)'; }
  });
  eventSource.addEventListener('requirements_ready', e => {
    const d = JSON.parse(e.data);
    const contentEl = document.getElementById('requirementsContent');
    if (contentEl && d.content) contentEl.value = d.content;
    const statusEl = document.getElementById('requirementsStatus');
    if (statusEl) { statusEl.textContent = '需求文档已生成，可直接编辑或转为开发任务。'; statusEl.style.color = 'var(--green)'; }
    showToast('需求文档已生成');
  });
  eventSource.addEventListener('requirements_error', e => {
    const d = JSON.parse(e.data);
    const statusEl = document.getElementById('requirementsStatus');
    if (statusEl) { statusEl.textContent = '生成失败: ' + (d.error || ''); statusEl.style.color = 'var(--red)'; }
  });
  refreshMeetingList();
}

function seedLiveWaiting(meeting) {
  const out = document.getElementById('liveOutput');
  if (!out || out.textContent.trim()) return;
  const m = meeting || {};
  const agents = m.agents || [];
  const runningAgent = agents.find(a => a.status === 'running');
  const waitingAgent = agents.find(a => a.status === 'waiting') || agents[0];
  const nextAgent = runningAgent || waitingAgent;
  const round = m.current_round || 1;
  let text = `\n${'═'.repeat(40)}\n会议已连接: ${m.topic || ''}\n`;
  if (agents.length) text += `参会: ${agents.map(a => a.name).join(', ')}\n`;
  text += `${'═'.repeat(40)}\n`;
  if (m.status === 'running') {
    if (nextAgent) {
      text += `\n[第 ${round} 轮] 等待 ${agentLabel(nextAgent.name, nextAgent.runtime)} 继续发言…`;
    } else {
      text += `\n[第 ${round} 轮] 等待模型事件返回...`;
    }
  } else {
    text += `\n会议状态: ${m.status || 'idle'}，等待启动或继续。`;
  }
  _enqueueTyping(text + '\n');
  if (m.status === 'running' && runningAgent) {
    showLiveWaiting(runningAgent.name, runningAgent.started_at);
  }
}

function renderAgents(agents) {
  seedAgentColors(agents);
  let html = '<div style="display:flex;gap:8px;flex-wrap:wrap;">';
  agents.forEach(a => {
    let statusColor, statusLabel, statusIcon = '';
    if (a.status === 'running') {
      statusColor = 'var(--accent)';
      statusLabel = '思考中...';
      statusIcon = '<span class="agent-spinner" style="display:inline-block;width:10px;height:10px;border:2px solid var(--accent);border-top-color:transparent;border-radius:50%;animation:agent-spin .8s linear infinite;margin-right:4px;vertical-align:middle;"></span>';
    } else if (a.status === 'completed') {
      statusColor = 'var(--green)';
      statusLabel = '已完成';
      statusIcon = '<span style="margin-right:4px;">✓</span>';
    } else if (a.status === 'error') {
      statusColor = 'var(--red)';
      statusLabel = '出错';
      statusIcon = '<span style="margin-right:4px;">✗</span>';
    } else if (a.status === 'awaiting_input') {
      statusColor = 'var(--accent)';
      statusLabel = '等待你发言';
      statusIcon = '<span style="margin-right:4px;">✎</span>';
    } else if (a.status === 'skipped') {
      statusColor = 'var(--text-dim)';
      statusLabel = '已跳过';
      statusIcon = '<span style="margin-right:4px;">–</span>';
    } else {
      statusColor = 'var(--text-dim)';
      statusLabel = '等待中';
      statusIcon = '<span style="margin-right:4px;opacity:.5;">○</span>';
    }
    const runtimeLabel = a.runtime === 'human' ? '人类' : a.runtime;
    const color = agentColor(a.id, a.name);
    html += `<div style="padding:8px 12px;border:1px solid ${(a.status==='running'||a.status==='awaiting_input')?'var(--accent)':'var(--border)'};border-left:4px solid ${color};border-radius:6px;background:var(--surface);min-width:120px;">
      <div style="font-weight:600;color:${color};">${a.name}</div>
      <div style="font-size:12px;color:var(--text-dim);">${runtimeLabel} · ${a.role||''}${a.resident?' · 常驻中':''}</div>
      <div style="font-size:12px;color:${statusColor};margin-top:4px;">${statusIcon}<span class="ag-elapsed"${(a.status==='running'&&a.started_at)?` data-started="${a.started_at}"`:''}>${statusLabel}</span></div>
    </div>`;
  });
  html += '</div>';
  document.getElementById('liveAgents').innerHTML = html;
}

let _agentRefreshTimer = null;
function updateAgentStatus(agentId, status, name) {
  if (!currentMeetingId) return;
  // agent_ready/agent_start/agent_complete often arrive in bursts. Coalesce
  // them into one refresh and keep it quiet: a transient network hiccup here
  // must not pop a "Failed to fetch" toast over the running meeting.
  if (_agentRefreshTimer) return;
  _agentRefreshTimer = setTimeout(() => {
    _agentRefreshTimer = null;
    if (!currentMeetingId) return;
    apiFetch(`/api/meetings/${currentMeetingId}`, {quiet: true})
      .then(m => renderAgents(m.agents))
      .catch(() => {});
  }, 200);
}

async function loadSharedFile() {
  if (!currentMeetingId) return;
  try {
    const data = await apiFetch(`/api/meetings/${currentMeetingId}/file`);
    const preview = document.getElementById('sharedFilePreview');
    preview.textContent =
      (data.path ? `文件: ${data.path}\n\n` : '') + (data.content || '(空)');
    preview.style.display = preview.style.display === 'none' ? 'block' : 'none';
  } catch(e) {}
}

/* ── Requirements handoff ────────────────── */
let _requirementsPoll = null;

async function loadRequirements(meetingId) {
  const id = meetingId || currentMeetingId;
  if (!id) return;
  try {
    const data = await apiFetch(`/api/meetings/${id}/requirements`, {quiet: true});
    const pathEl = document.getElementById('requirementsPath');
    const statusEl = document.getElementById('requirementsStatus');
    const contentEl = document.getElementById('requirementsContent');
    if (pathEl) pathEl.textContent = data.path || '';
    if (contentEl && document.activeElement !== contentEl) {
      contentEl.value = data.content || '';
    }
    if (statusEl) {
      if (data.status === 'running') {
        statusEl.textContent = '主持人正在整理需求文档…';
      } else if (data.status === 'error') {
        statusEl.textContent = '生成失败: ' + (data.error || '未知错误');
        statusEl.style.color = 'var(--red)';
      } else if (data.exists) {
        statusEl.textContent = '已生成，可直接编辑或转为开发任务。';
        statusEl.style.color = 'var(--green)';
      } else {
        statusEl.textContent = '尚未生成。会议结束后会自动整理，也可以点击“生成 / 重新生成”。';
        statusEl.style.color = 'var(--text-dim)';
      }
    }
    return data;
  } catch (e) {
    return null;
  }
}

async function loadMeetingTasks(meetingId) {
  const el = document.getElementById('meetingTasks');
  if (!el) return;
  const id = meetingId || currentMeetingId;
  if (!id) { el.style.display = 'none'; el.innerHTML = ''; return; }
  try {
    const tasks = await apiFetch(`/api/meetings/${id}/tasks`, {quiet: true});
    if (!Array.isArray(tasks) || !tasks.length) {
      el.style.display = 'none';
      el.innerHTML = '';
      return;
    }
    const colors = {
      completed: 'var(--green)', in_progress: 'var(--accent)',
      pending: 'var(--orange)', failed: 'var(--red)', stalled: 'var(--orange)',
      cancelled: 'var(--text-dim)', awaiting_gate: 'var(--purple)',
      awaiting_human_input: 'var(--purple)'
    };
    const rows = tasks.map(t => {
      const color = colors[t.status] || 'var(--text-dim)';
      const when = t.created_at ? new Date(t.created_at).toLocaleString() : '';
      return '<div style="display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;margin-top:3px;">'
        + '<a href="/tasks/' + t.id + '" style="font-family:ui-monospace,Consolas,monospace;">' + t.id + '</a>'
        + '<span style="color:' + color + ';font-weight:600;">' + t.status + '</span>'
        + '<span style="color:var(--text-dim);">'
        + (t.pipeline ? t.pipeline + ' · ' : '')
        + t.stage_count + ' 阶段 · ' + when + '</span>'
        + '</div>';
    }).join('');
    el.innerHTML = '<div style="font-weight:600;margin-bottom:4px;">该会议已创建的任务（'
      + tasks.length + '）</div>' + rows;
    el.style.display = 'block';
  } catch (e) {
    el.style.display = 'none';
  }
}

async function generateRequirements() {
  if (!currentMeetingId) return;
  const btn = document.getElementById('btnGenerateReq');
  const statusEl = document.getElementById('requirementsStatus');
  if (btn) { btn.disabled = true; btn.textContent = '生成中...'; }
  if (statusEl) { statusEl.textContent = '主持人正在整理需求文档…'; statusEl.style.color = 'var(--text-dim)'; }
  try {
    await apiFetch(`/api/meetings/${currentMeetingId}/requirements`, {method: 'POST'});
    if (_requirementsPoll) clearInterval(_requirementsPoll);
    let tries = 0;
    _requirementsPoll = setInterval(async () => {
      tries++;
      const data = await loadRequirements(currentMeetingId);
      if (!data || data.status !== 'running' || tries > 300) {
        clearInterval(_requirementsPoll);
        _requirementsPoll = null;
        if (btn) { btn.disabled = false; btn.textContent = '生成 / 重新生成'; }
      }
    }, 2000);
    showToast('已开始整理需求文档');
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = '生成 / 重新生成'; }
    if (statusEl) { statusEl.textContent = '生成失败: ' + e.message; statusEl.style.color = 'var(--red)'; }
  }
}

async function saveRequirements() {
  if (!currentMeetingId) return;
  const content = document.getElementById('requirementsContent')?.value ?? '';
  try {
    await apiFetch(`/api/meetings/${currentMeetingId}/requirements`, {
      method: 'PUT', body: JSON.stringify({content}),
    });
    showToast('需求文档已保存');
    loadRequirements(currentMeetingId);
  } catch (e) { showToast('保存失败: ' + e.message, 'error'); }
}

function updateHandoffPipelineStyle() {
  const sel = document.getElementById('handoffPipeline');
  if (!sel) return;
  sel.style.color = sel.value ? 'var(--text)' : 'var(--text-dim)';
}

async function loadHandoffPipelines() {
  const sel = document.getElementById('handoffPipeline');
  if (!sel) return;
  try {
    const data = await apiFetch('/api/pipelines');
    const list = data.pipelines || [];
    const def = data.default || '';
    sel.innerHTML = '';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = '选择流水线';
    sel.appendChild(placeholder);
    list.forEach(p => {
      const o = document.createElement('option');
      o.value = p.name;
      o.textContent = p.name + (p.name === def ? '（默认）' : '');
      sel.appendChild(o);
    });
    updateHandoffPipelineStyle();
  } catch (e) {}
}

async function handoffToTask() {
  if (!currentMeetingId) return;
  const content = document.getElementById('requirementsContent')?.value?.trim();
  if (!content) { showToast('需求文档为空，请先生成或填写', 'error'); return; }
  const pipelineSel = document.getElementById('handoffPipeline');
  const pipeline = pipelineSel?.value || '';
  if (!pipeline) {
    showToast('请先选择流水线', 'error');
    if (pipelineSel) {
      pipelineSel.focus();
      pipelineSel.style.borderColor = 'var(--red)';
      setTimeout(() => { pipelineSel.style.borderColor = 'var(--border)'; }, 1500);
    }
    return;
  }
  const btn = document.getElementById('btnHandoff');
  const resetBtn = () => { if (btn) { btn.disabled = false; btn.textContent = '转为开发任务'; } };
  if (btn) { btn.disabled = true; btn.textContent = '创建任务中...'; }
  const postHandoff = (force) => apiFetch(`/api/meetings/${currentMeetingId}/handoff`, {
    method: 'POST',
    quiet: true,
    body: JSON.stringify(force ? {pipeline, force: true} : {pipeline}),
  });
  try {
    // Persist any manual edits before handing off.
    await apiFetch(`/api/meetings/${currentMeetingId}/requirements`, {
      method: 'PUT', body: JSON.stringify({content}),
    });
    const data = await postHandoff(false);
    showToast('开发任务已创建: ' + data.task_id);
    setTimeout(() => { location.href = '/tasks/' + data.task_id; }, 700);
  } catch (e) {
    const detail = e && e.detail;
    if (e && e.status === 409 && detail && detail.code === 'task_exists') {
      const ok = window.confirm(detail.message + '\n\n是否仍要基于当前需求文档创建新的开发任务？');
      if (ok) {
        try {
          const data = await postHandoff(true);
          showToast('开发任务已创建: ' + data.task_id);
          setTimeout(() => { location.href = '/tasks/' + data.task_id; }, 700);
          return;
        } catch (e2) {
          showToast('转任务失败: ' + e2.message, 'error');
        }
      }
      resetBtn();
      return;
    }
    showToast('转任务失败: ' + (e && e.message ? e.message : '请求失败'), 'error');
    resetBtn();
  }
}

/* ── Init ───────────────────────────────── */
setInterval(() => {
  document.querySelectorAll('.ag-elapsed[data-started]').forEach(el => {
    const started = Date.parse(el.dataset.started);
    if (isNaN(started)) return;
    const secs = Math.max(0, Math.round((Date.now() - started) / 1000));
    el.textContent = `思考中... ${secs}s`;
  });
  updateLiveWaiting();
}, 1000);

function _initMeetingPage() {
  if (typeof apiFetch === 'function') {
    loadDefaultAgents();
    refreshMeetingList();
    loadHandoffPipelines();
    const humanInput = document.getElementById('humanSpeech');
    if (humanInput) {
      humanInput.addEventListener('input', updateHumanCharCount);
      humanInput.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter' || e.shiftKey || e.isComposing || e.keyCode === 229) return;
        e.preventDefault();
        sendHumanSpeech();
      });
      updateHumanCharCount();
    }
  } else {
    setTimeout(_initMeetingPage, 50);
  }
}
_initMeetingPage();
</script>
"""
    return layout("会议协作", body, active="meeting")
