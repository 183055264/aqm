"""Pipeline management page — create, edit (visual + YAML), duplicate, delete."""

from __future__ import annotations

from aqm.web.templates import esc, layout


def render_pipelines(
    pipelines: list[dict],
    current_default: str,
    edit_name: str | None = None,
    edit_content: str | None = None,
) -> str:
    # Pipeline table
    if pipelines:
        rows = []
        for p in pipelines:
            name = p["name"]
            default_badge = '<span class="badge badge-completed">default</span>' if p.get("is_default") else ""
            rows.append(f"""\
<tr>
  <td><a href="/pipelines?edit={esc(name)}">{esc(name)}</a></td>
  <td>{p.get('agent_count', 0)}</td>
  <td>{default_badge}</td>
  <td>
    <div class="dropdown">
      <button class="btn btn-sm" onclick="toggleDropdown(this)">操作</button>
      <div class="dropdown-menu">
        <a href="/pipelines?edit={esc(name)}">编辑</a>
        <button onclick="duplicatePipeline('{esc(name)}')">复制</button>
        <button onclick="setDefault('{esc(name)}')">设为默认</button>
        <a href="/api/pipelines/{esc(name)}/yaml" download>下载 YAML</a>
        <div class="divider"></div>
        <button class="danger" onclick="deletePipeline('{esc(name)}')">删除</button>
      </div>
    </div>
  </td>
</tr>""")
        table = f"""\
<div class="table-wrap">
<table>
  <thead><tr><th>名称</th><th>代理数</th><th>状态</th><th>操作</th></tr></thead>
  <tbody>{"".join(rows)}</tbody>
</table>
</div>"""
    else:
        table = """\
<div class="empty-state">
  <div class="title">暂无流水线</div>
  <div class="desc">创建第一条流水线来开始使用。</div>
</div>"""

    # Create section
    create_section = """\
<div class="card">
  <h3>创建流水线</h3>
  <div style="display:flex;gap:12px;margin-top:12px;flex-wrap:wrap;">
    <div class="form-group" style="flex:1;min-width:200px;">
      <label for="newName">流水线名称</label>
      <input id="newName" type="text" placeholder="my-pipeline"
             onkeydown="if(event.key==='Enter')createPipeline('template')">
    </div>
    <div style="display:flex;gap:8px;align-items:flex-end;">
      <button class="btn btn-primary" onclick="createPipeline('template')">从模板创建</button>
      <button class="btn" onclick="createPipeline('blank')">空白</button>
    </div>
  </div>
</div>"""

    # Editor section (Visual + YAML tabs)
    editor_section = ""
    if edit_name and edit_content is not None:
        editor_section = f"""\
<div class="card" style="border-color:var(--accent);">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
    <h3>编辑: {esc(edit_name)}</h3>
    <div style="display:flex;gap:8px;">
      <button class="btn btn-sm" onclick="validateYaml()">校验</button>
      <button class="btn btn-sm btn-primary" onclick="saveFromCurrentTab()">保存</button>
      <a class="btn btn-sm" href="/pipelines">关闭</a>
    </div>
  </div>
  <div id="validationResult" style="margin-bottom:8px;"></div>

  <!-- Editor Tabs -->
  <div class="tabs" style="margin-bottom:0;">
    <label class="tab" style="padding:8px 20px;cursor:pointer;border-bottom:2px solid var(--accent);color:var(--accent);"
           id="tabVisual" onclick="switchEditorTab('visual')">可视化编辑器</label>
    <label class="tab" style="padding:8px 20px;cursor:pointer;"
           id="tabYaml" onclick="switchEditorTab('yaml')">YAML 编辑器</label>
  </div>

  <!-- Visual Editor Panel -->
  <div id="panelVisual" style="margin-top:16px;">
    <div id="agentCards">
      <div style="text-align:center;color:var(--text-dim);padding:24px;">加载代理中...</div>
    </div>
    <button class="btn btn-primary" onclick="showAgentForm(null)" style="margin-top:12px;">+ 添加代理</button>
  </div>

  <!-- YAML Editor Panel -->
  <div id="panelYaml" style="display:none;margin-top:16px;">
    <textarea id="yamlEditor" class="yaml-editor" spellcheck="false">{esc(edit_content)}</textarea>
  </div>
</div>

<!-- Agent Edit Modal -->
<div class="modal-overlay" id="agentModal">
  <div class="modal modal-lg">
    <h3 id="agentModalTitle">添加代理</h3>
    <form id="agentForm" onsubmit="return false;">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
        <div class="form-group">
          <label>代理 ID *</label>
          <input id="af_id" type="text" placeholder="my-agent" required>
        </div>
        <div class="form-group">
          <label>名称</label>
          <input id="af_name" type="text" placeholder="我的代理">
        </div>
        <div class="form-group">
          <label>运行时</label>
          <select id="af_runtime" onchange="onAgentRuntimeChange()">
            <option value="claude">Claude</option>
            <option value="gemini">Gemini</option>
            <option value="codex">Codex</option>
            <option value="opencode">OpenCode</option>
            <option value="kilo">Kilo</option>
            <option value="cline">Cline</option>
          </select>
          <div style="font-size:11px;color:var(--text-dim);margin-top:4px;">切换运行时会重载模型列表，并清除原代理的 CLI 参数</div>
        </div>
        <div class="form-group">
          <label>模型</label>
          <select id="af_model">
            <option value="">默认模型</option>
          </select>
          <div id="af_modelHint" style="font-size:11px;color:var(--text-dim);margin-top:4px;">选择「默认模型」则使用该 CLI 的默认模型</div>
        </div>
      </div>
      <div class="form-group">
        <label>系统提示词</label>
        <textarea id="af_prompt" rows="4" placeholder="你是一个有用的代理。任务：{{{{ input }}}}"></textarea>
      </div>

      <!-- Type / session -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
        <div class="form-group">
          <label>类型</label>
          <select id="af_type" onchange="onAgentTypeChange()">
            <option value="agent">普通代理</option>
            <option value="session">会话节点</option>
          </select>
        </div>
        <div class="form-group" id="af_summaryGroup" style="display:none;">
          <label>总结成员</label>
          <select id="af_summary"><option value="">（无）</option></select>
        </div>
      </div>

      <div id="sessionFields" style="display:none;margin-top:8px;padding:12px;border:1px solid var(--border);border-radius:6px;background:var(--surface2);">
        <div class="form-group">
          <label>参与者</label>
          <div id="af_participants" class="participant-list" style="display:flex;flex-wrap:wrap;gap:8px;margin-top:6px;"></div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
          <div class="form-group">
            <label>最大轮次</label>
            <input id="af_maxRounds" type="number" min="1" max="30" value="3">
          </div>
          <div class="form-group">
            <label>共识方式</label>
            <select id="af_consensusMethod">
              <option value="vote">投票</option>
              <option value="moderator_decides">主持人决定</option>
            </select>
          </div>
          <div class="form-group">
            <label>共识关键词</label>
            <input id="af_consensusKeyword" type="text" placeholder="VOTE: AGREE">
          </div>
          <div class="form-group">
            <label>通过要求</label>
            <select id="af_consensusRequire">
              <option value="all">全部同意</option>
              <option value="majority">多数同意</option>
            </select>
          </div>
        </div>
        <label style="font-size:12px;display:flex;align-items:center;gap:6px;">
          <input type="checkbox" id="af_requireChunks"> 所有工作块完成才算达成共识
        </label>
      </div>

      <!-- Work chunks -->
      <div style="margin-top:16px;padding:12px;border:1px solid var(--border);border-radius:6px;">
        <label style="font-size:13px;cursor:pointer;display:flex;align-items:center;gap:6px;">
          <input type="checkbox" id="af_chunksEnabled" onchange="onChunksToggle()"> 启用工作块
        </label>
        <div id="chunkInputs" style="display:none;margin-top:10px;">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
            <div class="form-group">
              <label>未完成时</label>
              <select id="af_chunkEnforce">
                <option value="retry">自动重跑（推荐）</option>
                <option value="warn">只提醒</option>
                <option value="fail">直接失败</option>
                <option value="off">忽略</option>
              </select>
            </div>
            <div class="form-group">
              <label>重跑次数上限</label>
              <input id="af_chunkRetryLimit" type="number" min="0" max="10" value="2">
            </div>
          </div>
          <div class="form-group">
            <label>初始工作块（每行一条）</label>
            <textarea id="af_chunksInitial" rows="3" placeholder="实现登录接口&#10;补齐单元测试"></textarea>
          </div>
          <div style="display:flex;flex-direction:column;gap:6px;font-size:12px;">
            <label><input type="checkbox" id="af_chunkAutoSplit"> 允许按需求文档自动拆分</label>
            <label><input type="checkbox" id="af_chunkOwnerFilter"> 只考核本成员名下的工作块</label>
            <label><input type="checkbox" id="af_chunkAutoClaim"> 开工时自动认领第一个待处理块</label>
          </div>
        </div>
      </div>

      <!-- Handoffs -->
      <div style="margin-top:12px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <label style="font-weight:600;font-size:13px;color:var(--text-dim);">交接</label>
          <button type="button" class="btn btn-sm" onclick="addHandoffRow()">+ 添加</button>
        </div>
        <div id="handoffRows" style="margin-top:8px;"></div>
        <div style="font-size:12px;color:var(--text-dim);margin-top:6px;">目标可多选；交接的「任务说明 / 载荷 payload」请在 YAML 编辑器里配置，这里保存时会自动保留。</div>
      </div>

      <!-- Gate -->
      <div style="margin-top:16px;">
        <label style="font-size:13px;color:var(--text-dim);cursor:pointer;">
          <input type="checkbox" id="af_gateEnabled" onchange="document.getElementById('gateFields').style.display=this.checked?'block':'none'"> 启用审批门
        </label>
        <div id="gateFields" style="display:none;margin-top:8px;">
          <div style="display:grid;grid-template-columns:1fr 2fr;gap:12px;">
            <div class="form-group">
              <label>类型</label>
              <select id="af_gateType"><option value="llm">LLM 审批</option><option value="human">人工审批</option></select>
            </div>
            <div class="form-group">
              <label>提示词</label>
              <input id="af_gatePrompt" type="text" placeholder="这个输出是否可以投产?">
            </div>
          </div>
        </div>
      </div>

      <div style="display:flex;gap:8px;margin-top:20px;">
        <button type="button" class="btn btn-primary" onclick="saveAgent()">保存代理</button>
        <button type="button" class="btn" onclick="closeAgentModal()">取消</button>
        <button type="button" class="btn btn-red" id="deleteAgentBtn" style="margin-left:auto;display:none;"
                onclick="deleteAgent()">删除</button>
      </div>
    </form>
  </div>
</div>"""

    js = f"""\
<script>
const EDIT_PIPELINE = {f"'{esc(edit_name)}'" if edit_name else "null"};
let currentTab = 'visual';
let editingAgentId = null;
let EDITING_AGENT = null;
let PIPELINE_AGENTS = [];

/* ── Tab switching ─────────────────────────── */
function switchEditorTab(tab) {{
  currentTab = tab;
  document.getElementById('panelVisual').style.display = tab === 'visual' ? 'block' : 'none';
  document.getElementById('panelYaml').style.display = tab === 'yaml' ? 'block' : 'none';
  document.getElementById('tabVisual').style.borderBottomColor = tab === 'visual' ? 'var(--accent)' : 'transparent';
  document.getElementById('tabVisual').style.color = tab === 'visual' ? 'var(--accent)' : 'var(--text-dim)';
  document.getElementById('tabYaml').style.borderBottomColor = tab === 'yaml' ? 'var(--accent)' : 'transparent';
  document.getElementById('tabYaml').style.color = tab === 'yaml' ? 'var(--accent)' : 'var(--text-dim)';
  if (tab === 'visual') loadAgentCards();
  if (tab === 'yaml') loadYamlContent();
}}

/* ── Save from current tab ─────────────────── */
async function saveFromCurrentTab() {{
  if (currentTab === 'yaml') {{
    await savePipelineYaml();
  }}
  showToast('流水线已保存');
}}

/* ── Load agent cards ──────────────────────── */
async function loadAgentCards() {{
  if (!EDIT_PIPELINE) return;
  try {{
    const agents = await apiFetch('/api/pipelines/' + encodeURIComponent(EDIT_PIPELINE) + '/agents');
    PIPELINE_AGENTS = Array.isArray(agents) ? agents : [];
    renderAgentCards(agents);
  }} catch(e) {{
    document.getElementById('agentCards').innerHTML = '<div style="color:var(--red);padding:12px;">加载代理失败</div>';
  }}
}}

function renderAgentCards(agents) {{
  const container = document.getElementById('agentCards');
  if (!agents.length) {{
    container.innerHTML = '<div class="empty-state" style="padding:24px;"><div class="title">暂无代理</div><div class="desc">添加第一个代理来开始使用。</div></div>';
    return;
  }}
  container.innerHTML = agents.map((a, i) => {{
    const runtimeColors = {{claude:'var(--accent)', gemini:'var(--green)', codex:'var(--purple)'}};
    const rc = runtimeColors[a.runtime] || 'var(--text-dim)';
    const modelHtml = a.model
      ? '<span style="font-size:11px;color:var(--text-dim);margin-left:6px;">· ' + String(a.model).replace(/</g, '&lt;') + '</span>'
      : '';
    const handoffs = (a.handoffs || []).map(h =>
      '<span style="display:inline-block;background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:1px 8px;font-size:11px;margin:2px;">' +
      '→ ' + (h.to||'') + (h.condition && h.condition !== 'always' ? ' [' + h.condition + ']' : '') + '</span>'
    ).join('');
    const gateHtml = a.gate ? '<span class="badge" style="background:#1a1040;color:var(--purple);border:1px solid #3b2d6b;font-size:10px;margin-left:6px;">' + a.gate.type + ' gate</span>' : '';
    const typeHtml = a.type === 'session'
      ? '<span class="badge" style="background:#102a43;color:#63b3ed;font-size:10px;margin-left:6px;">session</span>'
      : '';
    const chunksHtml = a.chunks
      ? '<span class="badge" style="background:#1f3d2b;color:#68d391;font-size:10px;margin-left:6px;">工作块 ' + (a.chunks.enforce || 'retry') + '</span>'
      : '';
    const participantsHtml = (a.type === 'session' && (a.participants || []).length)
      ? '<div style="font-size:11px;color:var(--text-dim);margin-top:4px;">参与者: ' + a.participants.join('、') + '</div>'
      : '';
    const promptPreview = (a.system_prompt || '').substring(0, 100).replace(/</g, '&lt;');

    return `<div class="card" style="padding:14px;margin-bottom:8px;border-left:3px solid ${{rc}};">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;">
        <div>
          <div style="font-weight:700;color:var(--accent);">${{a.name || a.id}}
            <span style="font-size:11px;color:var(--text-dim);font-weight:400;margin-left:6px;">${{a.id}}</span>
            <span style="font-size:11px;color:${{rc}};margin-left:6px;">${{a.runtime}}</span>
            ${{modelHtml}}
            ${{typeHtml}}
            ${{chunksHtml}}
            ${{gateHtml}}
          </div>
          ${{participantsHtml}}
          ${{promptPreview ? '<div style="font-size:12px;color:var(--text-dim);margin-top:4px;">' + promptPreview + (a.system_prompt && a.system_prompt.length > 100 ? '...' : '') + '</div>' : ''}}
          ${{handoffs ? '<div style="margin-top:6px;">' + handoffs + '</div>' : ''}}
        </div>
        <div style="display:flex;gap:4px;flex-shrink:0;">
          <button class="btn btn-sm" onclick="editAgent('${{a.id}}')">编辑</button>
          <button class="btn btn-sm" style="color:var(--red);" onclick="confirmDeleteAgent('${{a.id}}')">删除</button>
        </div>
      </div>
    </div>`;
  }}).join('');
}}

/* ── Load YAML content ─────────────────────── */
async function loadYamlContent() {{
  if (!EDIT_PIPELINE) return;
  try {{
    const data = await apiFetch('/api/pipelines/' + encodeURIComponent(EDIT_PIPELINE));
    document.getElementById('yamlEditor').value = data.content;
  }} catch(e) {{}}
}}

/* ── Agent form ────────────────────────────── */
const MODEL_CACHE = {{}};

async function fetchRuntimeModels(runtime) {{
  const rt = runtime || 'claude';
  if (MODEL_CACHE[rt]) return MODEL_CACHE[rt];
  try {{
    const data = await apiFetch('/api/meetings/models?runtime=' + encodeURIComponent(rt));
    MODEL_CACHE[rt] = Array.isArray(data.models) ? data.models : [];
  }} catch (e) {{
    MODEL_CACHE[rt] = [];
  }}
  return MODEL_CACHE[rt];
}}

async function loadModelOptions(runtime, selected) {{
  const sel = document.getElementById('af_model');
  const hint = document.getElementById('af_modelHint');
  if (!sel) return;
  sel.innerHTML = '';
  const def = document.createElement('option');
  def.value = '';
  def.textContent = '默认模型';
  sel.appendChild(def);

  const models = await fetchRuntimeModels(runtime);
  const seen = new Set();
  models.forEach(m => {{
    const value = typeof m === 'string' ? m : (m && m.value) || '';
    if (!value || seen.has(value)) return;
    seen.add(value);
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = (typeof m === 'object' && m && m.label) ? m.label : value;
    sel.appendChild(opt);
  }});

  const wanted = String(selected || '').trim();
  if (wanted && !seen.has(wanted)) {{
    const opt = document.createElement('option');
    opt.value = wanted;
    opt.textContent = wanted + '（当前值，不在列表）';
    sel.appendChild(opt);
  }}
  sel.value = wanted;

  if (hint) {{
    if (wanted) hint.textContent = '下次运行使用该模型';
    else if (models.length) hint.textContent = '选择「默认模型」则使用该 CLI 的默认模型';
    else hint.textContent = '该运行时暂未发现可选模型，将使用默认模型';
  }}
}}

async function onAgentRuntimeChange() {{
  const runtime = document.getElementById('af_runtime').value;
  await loadModelOptions(runtime, '');
}}

function onAgentTypeChange() {{
  const isSession = document.getElementById('af_type').value === 'session';
  document.getElementById('sessionFields').style.display = isSession ? 'block' : 'none';
  document.getElementById('af_summaryGroup').style.display = isSession ? 'block' : 'none';
}}

function onChunksToggle() {{
  const enabled = document.getElementById('af_chunksEnabled').checked;
  document.getElementById('chunkInputs').style.display = enabled ? 'block' : 'none';
}}

function renderParticipantOptions(selected) {{
  const container = document.getElementById('af_participants');
  const summary = document.getElementById('af_summary');
  const chosen = new Set(selected || []);
  container.innerHTML = '';
  summary.innerHTML = '<option value="">（无）</option>';
  PIPELINE_AGENTS.forEach(a => {{
    if (a.id === document.getElementById('af_id').value) return;
    const label = document.createElement('label');
    label.style.cssText = 'display:flex;align-items:center;gap:5px;font-size:12px;';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = a.id;
    cb.checked = chosen.has(a.id);
    cb.addEventListener('change', () => {{
      const current = summary.value;
      summary.innerHTML = '<option value="">（无）</option>';
      container.querySelectorAll('input:checked').forEach(picked => {{
        const agent = PIPELINE_AGENTS.find(x => x.id === picked.value);
        if (!agent) return;
        const opt = document.createElement('option');
        opt.value = agent.id;
        opt.textContent = agent.name ? agent.name + ' (' + agent.id + ')' : agent.id;
        summary.appendChild(opt);
      }});
      if (Array.from(summary.options).some(o => o.value === current)) {{
        summary.value = current;
      }}
    }});
    const span = document.createElement('span');
    span.textContent = a.name ? a.name + ' (' + a.id + ')' : a.id;
    label.appendChild(cb);
    label.appendChild(span);
    container.appendChild(label);

    const opt = document.createElement('option');
    opt.value = a.id;
    opt.textContent = a.name ? a.name + ' (' + a.id + ')' : a.id;
    if (chosen.has(a.id)) summary.appendChild(opt);
  }});
}}

async function showAgentForm(agentId) {{
  editingAgentId = agentId;
  EDITING_AGENT = null;
  document.getElementById('agentModalTitle').textContent = agentId ? '编辑代理' : '添加代理';
  document.getElementById('deleteAgentBtn').style.display = agentId ? 'inline-block' : 'none';
  // Clear form
  document.getElementById('af_id').value = '';
  document.getElementById('af_name').value = '';
  document.getElementById('af_runtime').value = 'claude';
  document.getElementById('af_prompt').value = '';
  document.getElementById('af_type').value = 'agent';
  onAgentTypeChange();
  document.getElementById('af_maxRounds').value = 3;
  document.getElementById('af_consensusMethod').value = 'vote';
  document.getElementById('af_consensusKeyword').value = 'VOTE: AGREE';
  document.getElementById('af_consensusRequire').value = 'all';
  document.getElementById('af_requireChunks').checked = false;
  document.getElementById('af_chunksEnabled').checked = false;
  document.getElementById('af_chunkEnforce').value = 'retry';
  document.getElementById('af_chunkRetryLimit').value = 2;
  document.getElementById('af_chunksInitial').value = '';
  document.getElementById('af_chunkAutoSplit').checked = true;
  document.getElementById('af_chunkOwnerFilter').checked = true;
  document.getElementById('af_chunkAutoClaim').checked = true;
  onChunksToggle();
  document.getElementById('af_gateEnabled').checked = false;
  document.getElementById('gateFields').style.display = 'none';
  document.getElementById('af_gateType').value = 'llm';
  document.getElementById('af_gatePrompt').value = '';
  document.getElementById('handoffRows').innerHTML = '';
  document.getElementById('af_id').disabled = false;
  await loadModelOptions(document.getElementById('af_runtime').value, '');

  // Make sure the target agent list is available for the handoff dropdown
  if (!PIPELINE_AGENTS.length && EDIT_PIPELINE) {{
    await loadAgentCards();
  }}
  renderParticipantOptions([]);

  if (agentId) {{
    // Populate from API
    try {{
      const agents = await apiFetch('/api/pipelines/' + encodeURIComponent(EDIT_PIPELINE) + '/agents');
      PIPELINE_AGENTS = Array.isArray(agents) ? agents : [];
      const a = agents.find(x => x.id === agentId);
      if (a) {{
        EDITING_AGENT = a;
        document.getElementById('af_id').value = a.id;
        document.getElementById('af_id').disabled = true;
        document.getElementById('af_name').value = a.name || '';
        document.getElementById('af_runtime').value = a.runtime || 'claude';
        document.getElementById('af_prompt').value = a.system_prompt || '';
        await loadModelOptions(a.runtime || 'claude', a.model || '');
        document.getElementById('af_type').value = a.type || 'agent';
        onAgentTypeChange();
        renderParticipantOptions(a.participants || []);
        document.getElementById('af_maxRounds').value = a.max_rounds || 3;
        document.getElementById('af_summary').value = a.summary_agent || '';
        if (a.consensus) {{
          document.getElementById('af_consensusMethod').value = a.consensus.method || 'vote';
          document.getElementById('af_consensusKeyword').value = a.consensus.keyword || 'VOTE: AGREE';
          document.getElementById('af_consensusRequire').value = a.consensus.require || 'all';
          document.getElementById('af_requireChunks').checked = !!a.consensus.require_chunks_done;
        }}
        if (a.chunks) {{
          document.getElementById('af_chunksEnabled').checked = a.chunks.enabled !== false;
          document.getElementById('af_chunkEnforce').value = a.chunks.enforce || 'retry';
          document.getElementById('af_chunkRetryLimit').value =
            (a.chunks.retry_limit === undefined || a.chunks.retry_limit === null) ? 2 : a.chunks.retry_limit;
          const initial = a.chunks.initial || [];
          document.getElementById('af_chunksInitial').value = initial.map(item =>
            typeof item === 'string' ? item : (item.description || item.title || '')
          ).join('\\n');
          document.getElementById('af_chunkAutoSplit').checked = a.chunks.auto_split !== false;
          document.getElementById('af_chunkOwnerFilter').checked = a.chunks.owner_filter !== false;
          document.getElementById('af_chunkAutoClaim').checked = a.chunks.auto_claim !== false;
          onChunksToggle();
        }}
        if (a.gate) {{
          document.getElementById('af_gateEnabled').checked = true;
          document.getElementById('gateFields').style.display = 'block';
          document.getElementById('af_gateType').value = a.gate.type || 'llm';
          document.getElementById('af_gatePrompt').value = a.gate.prompt || '';
        }}
        (a.handoffs || []).forEach(h => addHandoffRow(h.to, h.condition || 'always'));
      }}
    }} catch(e) {{}}
  }}

  document.getElementById('agentModal').classList.add('show');
}}

function editAgent(agentId) {{ showAgentForm(agentId); }}
function closeAgentModal() {{ document.getElementById('agentModal').classList.remove('show'); }}

document.getElementById('agentModal')?.addEventListener('click', function(e) {{
  if (e.target === this) closeAgentModal();
}});

/* ── Handoff rows ──────────────────────────── */
function updateHandoffLabel(multi) {{
  const checked = Array.from(multi.querySelectorAll('input:checked')).map(cb => cb.value);
  const btn = multi.querySelector('.ho-multi-btn');
  if (!checked.length) {{
    btn.textContent = '选择目标代理';
    btn.style.color = 'var(--text-dim)';
    multi.dataset.value = '';
    return;
  }}
  const labels = checked.map(id => {{
    const a = PIPELINE_AGENTS.find(x => x.id === id);
    return a ? (a.name ? a.name + ' (' + id + ')' : id) : id;
  }});
  btn.textContent = labels.join('、');
  btn.style.color = 'var(--text)';
  multi.dataset.value = checked.join(', ');
}}

function closeHandoffMenus() {{
  document.querySelectorAll('.ho-multi-menu.show').forEach(m => m.classList.remove('show'));
}}

function positionHandoffMenu(menu, btn) {{
  const rect = btn.getBoundingClientRect();
  const gap = 4;
  const spaceBelow = window.innerHeight - rect.bottom - gap - 8;
  const spaceAbove = rect.top - gap - 8;
  const openUp = spaceBelow < 160 && spaceAbove > spaceBelow;
  const maxH = Math.max(120, Math.min(240, openUp ? spaceAbove : spaceBelow));
  menu.style.left = rect.left + 'px';
  menu.style.width = rect.width + 'px';
  menu.style.maxHeight = maxH + 'px';
  if (openUp) {{
    menu.style.top = 'auto';
    menu.style.bottom = (window.innerHeight - rect.top + gap) + 'px';
  }} else {{
    menu.style.top = (rect.bottom + gap) + 'px';
    menu.style.bottom = 'auto';
  }}
}}

function addHandoffRow(to, condition) {{
  const container = document.getElementById('handoffRows');
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:8px;margin-bottom:6px;align-items:center;';

  const multi = document.createElement('div');
  multi.className = 'ho-multi';

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'ho-multi-btn';
  btn.textContent = '选择目标代理';
  btn.style.color = 'var(--text-dim)';

  const menu = document.createElement('div');
  menu.className = 'ho-multi-menu';

  const selected = String(to || '').split(',').map(s => s.trim()).filter(Boolean);
  const options = PIPELINE_AGENTS.map(a => ({{ id: a.id, label: a.name ? a.name + ' (' + a.id + ')' : a.id }}));
  selected.forEach(t => {{
    if (!options.some(o => o.id === t)) options.push({{ id: t, label: t + '（未定义）' }});
  }});

  options.forEach(opt => {{
    const label = document.createElement('label');
    label.className = 'ho-multi-item';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = opt.id;
    cb.checked = selected.includes(opt.id);
    cb.addEventListener('change', () => updateHandoffLabel(multi));
    const span = document.createElement('span');
    span.textContent = opt.label;
    label.appendChild(cb);
    label.appendChild(span);
    menu.appendChild(label);
  }});

  if (!options.length) {{
    const empty = document.createElement('div');
    empty.className = 'ho-multi-empty';
    empty.textContent = '暂无其他代理可交接';
    menu.appendChild(empty);
  }}

  btn.addEventListener('click', (e) => {{
    e.stopPropagation();
    const wasOpen = menu.classList.contains('show');
    closeHandoffMenus();
    if (wasOpen) return;
    positionHandoffMenu(menu, btn);
    menu.classList.add('show');
  }});
  menu.addEventListener('click', (e) => e.stopPropagation());

  multi.appendChild(btn);
  multi.appendChild(menu);

  const cond = document.createElement('select');
  cond.className = 'ho-cond';
  cond.style.cssText = 'padding:6px 8px;font-size:13px;border:1px solid var(--border);border-radius:4px;background:var(--surface2);color:var(--text);';
  cond.innerHTML = '<option value="always">始终</option>' +
    '<option value="on_approve">审批通过</option>' +
    '<option value="on_reject">审批拒绝</option>' +
    '<option value="on_pass">审批放行</option>' +
    '<option value="auto">自动</option>';
  const condVal = condition || 'always';
  if (!['always', 'on_approve', 'on_reject', 'on_pass', 'auto'].includes(condVal)) {{
    const custom = document.createElement('option');
    custom.value = condVal;
    custom.textContent = condVal;
    cond.appendChild(custom);
  }}
  cond.value = condVal;

  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'btn btn-sm';
  del.style.color = 'var(--red)';
  del.textContent = '✕';
  del.addEventListener('click', () => row.remove());

  row.appendChild(multi);
  row.appendChild(cond);
  row.appendChild(del);
  container.appendChild(row);
  updateHandoffLabel(multi);
}}

document.addEventListener('click', function() {{
  closeHandoffMenus();
}});
window.addEventListener('scroll', function(e) {{
  // Ignore scrolling inside the dropdown itself
  if (e.target && e.target.closest && e.target.closest('.ho-multi-menu')) return;
  closeHandoffMenus();
}}, true);
window.addEventListener('resize', closeHandoffMenus);

/* ── Save agent ────────────────────────────── */
function normalizeHandoffTo(value) {{
  return String(value || '').split(',').map(s => s.trim()).filter(Boolean).sort().join(',');
}}

function findOriginalHandoff(to, condition, used) {{
  const originals = (EDITING_AGENT && EDITING_AGENT.handoffs) || [];
  const key = normalizeHandoffTo(to);
  let fallback = -1;
  for (let i = 0; i < originals.length; i++) {{
    if (used.has(i)) continue;
    if (normalizeHandoffTo(originals[i].to) !== key) continue;
    if ((originals[i].condition || 'always') === condition) {{
      used.add(i);
      return originals[i];
    }}
    if (fallback < 0) fallback = i;
  }}
  if (fallback >= 0) {{
    used.add(fallback);
    return originals[fallback];
  }}
  return null;
}}

async function saveAgent() {{
  const id = document.getElementById('af_id').value.trim();
  if (!id) {{     showToast('代理 ID 必填', 'error'); return; }}

  const body = {{
    id: id,
    name: document.getElementById('af_name').value.trim(),
    runtime: document.getElementById('af_runtime').value,
    model: document.getElementById('af_model').value.trim(),
    system_prompt: document.getElementById('af_prompt').value,
    handoffs: [],
    gate: null,
  }};

  // Type-specific fields
  const agentType = document.getElementById('af_type').value;
  body.type = agentType;
  if (agentType === 'session') {{
    body.participants = Array.from(
      document.querySelectorAll('#af_participants input:checked')
    ).map(cb => cb.value);
    body.max_rounds = parseInt(document.getElementById('af_maxRounds').value || '3', 10) || 3;
    body.summary_agent = document.getElementById('af_summary').value || null;
    body.consensus = {{
      method: document.getElementById('af_consensusMethod').value,
      keyword: document.getElementById('af_consensusKeyword').value || 'VOTE: AGREE',
      require: document.getElementById('af_consensusRequire').value,
      require_chunks_done: document.getElementById('af_requireChunks').checked,
    }};
  }} else {{
    body.participants = [];
    body.max_rounds = null;
    body.consensus = null;
    body.summary_agent = null;
  }}

  // Work blocks
  if (document.getElementById('af_chunksEnabled').checked) {{
    body.chunks = {{
      enabled: true,
      enforce: document.getElementById('af_chunkEnforce').value,
      retry_limit: parseInt(document.getElementById('af_chunkRetryLimit').value || '2', 10) || 0,
      initial: document.getElementById('af_chunksInitial').value
        .split('\\n').map(s => s.trim()).filter(Boolean),
      auto_split: document.getElementById('af_chunkAutoSplit').checked,
      owner_filter: document.getElementById('af_chunkOwnerFilter').checked,
      auto_claim: document.getElementById('af_chunkAutoClaim').checked,
    }};
  }} else {{
    body.chunks = null;
  }}

  // Collect handoffs. Fields not exposed by the visual editor (task / payload)
  // are copied from the original definition so saving never drops them.
  const usedOriginal = new Set();
  document.querySelectorAll('#handoffRows > div').forEach(row => {{
    const multi = row.querySelector('.ho-multi');
    const to = ((multi && multi.dataset.value) || '').trim();
    const cond = row.querySelector('.ho-cond').value;
    if (!to) return;
    const item = {{to, condition: cond}};
    const orig = findOriginalHandoff(to, cond, usedOriginal);
    if (orig) {{
      if (orig.task) item.task = orig.task;
      if (orig.payload) item.payload = orig.payload;
    }}
    body.handoffs.push(item);
  }});

  // Gate
  if (document.getElementById('af_gateEnabled').checked) {{
    const gate = {{
      type: document.getElementById('af_gateType').value,
      prompt: document.getElementById('af_gatePrompt').value,
    }};
    const oldGate = EDITING_AGENT && EDITING_AGENT.gate;
    if (oldGate) {{
      if (oldGate.max_retries !== undefined && oldGate.max_retries !== null) gate.max_retries = oldGate.max_retries;
      if (oldGate.model) gate.model = oldGate.model;
    }}
    body.gate = gate;
  }}

  const url = '/api/pipelines/' + encodeURIComponent(EDIT_PIPELINE) + '/agents' + (editingAgentId ? '/' + encodeURIComponent(editingAgentId) : '');
  const method = editingAgentId ? 'PUT' : 'POST';

  try {{
    const result = await apiFetch(url, {{method, body:JSON.stringify(body)}});
    if (result && result.cleared && result.cleared.length) {{
      showToast('运行时已切换，已清除 ' + result.cleared.join(' / '));
    }} else {{
      showToast(editingAgentId ? '代理已更新' : '代理已添加');
    }}
    closeAgentModal();
    loadAgentCards();
  }} catch(e) {{}}
}}

/* ── Delete agent ──────────────────────────── */
async function deleteAgent() {{
  if (!editingAgentId || !confirm('删除代理 "' + editingAgentId + '"?')) return;
  try {{
    await apiFetch('/api/pipelines/' + encodeURIComponent(EDIT_PIPELINE) + '/agents/' + encodeURIComponent(editingAgentId), {{method:'DELETE'}});
    showToast('代理已删除');
    closeAgentModal();
    loadAgentCards();
  }} catch(e) {{}}
}}
async function confirmDeleteAgent(agentId) {{
  if (!confirm('删除代理 "' + agentId + '"?')) return;
  try {{
    await apiFetch('/api/pipelines/' + encodeURIComponent(EDIT_PIPELINE) + '/agents/' + encodeURIComponent(agentId), {{method:'DELETE'}});
    showToast('代理已删除');
    loadAgentCards();
  }} catch(e) {{}}
}}

/* ── Pipeline CRUD ─────────────────────────── */
async function createPipeline(mode) {{
  const name = document.getElementById('newName').value.trim();
  if (!name) {{ showToast('请输入流水线名称', 'error'); return; }}
  const content = mode === 'template'
    ? 'agents:\\n  - id: planner\\n    name: Planner\\n    runtime: claude\\n    system_prompt: "Plan: {{{{ input }}}}"\\n    handoffs:\\n      - to: developer\\n\\n  - id: developer\\n    name: Developer\\n    runtime: claude\\n    system_prompt: "Build: {{{{ input }}}}"\\n'
    : 'agents:\\n  - id: agent_1\\n    name: Agent 1\\n    runtime: claude\\n    system_prompt: "{{{{ input }}}}"\\n';
  try {{
    await apiFetch('/api/pipelines', {{method:'POST', body:JSON.stringify({{name, content}})}});
    showToast('流水线已创建');
    location.href = '/pipelines?edit=' + encodeURIComponent(name);
  }} catch(e) {{}}
}}
async function savePipelineYaml() {{
  const content = document.getElementById('yamlEditor')?.value;
  if (!EDIT_PIPELINE || !content) return;
  try {{
    await apiFetch('/api/pipelines/' + encodeURIComponent(EDIT_PIPELINE), {{method:'PUT', body:JSON.stringify({{content}})}});
  }} catch(e) {{}}
}}
async function validateYaml() {{
  let content;
  if (currentTab === 'yaml') {{
    content = document.getElementById('yamlEditor').value;
  }} else {{
    const data = await apiFetch('/api/pipelines/' + encodeURIComponent(EDIT_PIPELINE));
    content = data.content;
  }}
  try {{
    const r = await apiFetch('/api/validate', {{method:'POST', body:JSON.stringify({{yaml_content:content}})}});
    const el = document.getElementById('validationResult');
    if (r.valid) {{
      const count = (r.summary && r.summary.agent_count) || r.agent_count || 0;
      el.innerHTML = '<span style="color:var(--green);">\\u2713 有效 (' + count + ' 个代理)</span>';
    }} else {{
      el.innerHTML = '<span style="color:var(--red);">\\u2717 ' + (r.errors||[]).map(e=>e.message||e).join(', ') + '</span>';
    }}
  }} catch(e) {{}}
}}
async function deletePipeline(name) {{
  if (!confirm('删除流水线 "' + name + '"?')) return;
  try {{
    await apiFetch('/api/pipelines/' + encodeURIComponent(name), {{method:'DELETE'}});
    showToast('流水线已删除');
    location.reload();
  }} catch(e) {{}}
}}
async function duplicatePipeline(name) {{
  const newName = prompt('新流水线名称:');
  if (!newName) return;
  try {{
    await apiFetch('/api/pipelines/' + encodeURIComponent(name) + '/duplicate', {{method:'POST', body:JSON.stringify({{new_name:newName}})}});
    showToast('流水线已复制');
    location.reload();
  }} catch(e) {{}}
}}
async function setDefault(name) {{
  try {{
    await apiFetch('/api/pipelines/default', {{method:'POST', body:JSON.stringify({{name}})}});
    showToast('默认已设为 ' + name);
    location.reload();
  }} catch(e) {{}}
}}

/* Init: load agent cards if editing */
if (EDIT_PIPELINE) setTimeout(loadAgentCards, 50);
</script>"""

    body = (
        f"<h1>流水线</h1>\n"
        f"{create_section}\n"
        f"{editor_section}\n"
        f"{table}\n"
        f"{js}"
    )

    return layout("流水线", body, active="pipelines")
