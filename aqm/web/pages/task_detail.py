"""Task detail page — tabbed interface with overview, live output, timeline, context, chunks."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from aqm.core.agent import AgentDefinition
from aqm.core.artifacts import collect_task_files, format_size
from aqm.core.gate_review import build_gate_review
from aqm.core.pipeline import is_pure_nudge
from aqm.core.task import Task, TaskStatus
from aqm.web.markdown import render_markdown
from aqm.web.templates import (
    badge,
    copy_pre,
    display_status,
    esc,
    fmt_duration,
    fmt_time,
    layout,
)

_GATE_ITEM_MARKS = {
    "conclusion": ("结论", "var(--cyan)"),
    "check": ("待确认", "var(--orange)"),
    "todo": ("☐", "var(--orange)"),
    "done": ("✓", "var(--green)"),
    "section": ("§", "var(--text-dim)"),
    "note": ("•", "var(--text-dim)"),
}

_CHUNK_STATUS_LABELS = {
    "pending": "待处理",
    "in_progress": "进行中",
    "blocked": "已阻塞",
    "done": "已完成",
}


def _render_gate_review(review: dict) -> str:
    """Render the concrete checklist shown above the approve/reject buttons."""
    if not review:
        return ""
    subject_bits = []
    if review.get("stage_number"):
        subject_bits.append(f"阶段 {review['stage_number']}")
    if review.get("agent_name"):
        label = review["agent_name"]
        if review.get("agent_id") and review["agent_id"] != label:
            label = f"{label}（{review['agent_id']}）"
        subject_bits.append(label)
    subject = " · ".join(subject_bits) or "未知成员"

    items_html = ""
    for item in review.get("items") or []:
        state = str(item.get("state") or "check")
        mark, color = _GATE_ITEM_MARKS.get(state, ("•", "var(--text-dim)"))
        items_html += (
            f'<li class="gate-item gate-item-{esc(state)}">'
            f'<span class="gate-item-mark" style="color:{color};">{esc(mark)}</span>'
            f'<span>{esc(item.get("text") or "")}</span></li>'
        )
    if items_html:
        items_html = f'<ul class="gate-items">{items_html}</ul>'
    else:
        items_html = (
            '<div class="gate-empty">该成员没有留下可解析的检查项，'
            "请结合下方完整输出与产物判断。</div>"
        )

    if review.get("items_source") == "gate_prompt":
        source_note = "以上条目来自流水线为该审批门配置的 <code>gate.prompt</code>。"
    elif review.get("items_source") == "stage_output":
        source_note = (
            "以上条目由该成员本阶段输出自动提取（结论 / 检查列表 / 章节），"
            "也可在下方补充你需要额外确认的事项。"
        )
    else:
        source_note = ""

    chunks = review.get("chunks") or {}
    chunks_html = ""
    if chunks.get("total"):
        rows = ""
        for chunk in chunks.get("pending") or []:
            label = _CHUNK_STATUS_LABELS.get(chunk.get("status", ""), chunk.get("status", ""))
            owner = f" · {chunk['owner']}" if chunk.get("owner") else ""
            rows += (
                f'<li><a href="#chunks" style="color:var(--text);">'
                f'{esc(chunk.get("id", ""))}</a> '
                f'<span class="gate-chunk-status">{esc(label)}</span>{esc(owner)} — '
                f'{esc(chunk.get("description", ""))}</li>'
            )
        more = chunks["pending_count"] - len(chunks.get("pending") or [])
        if more > 0:
            rows += f'<li class="gate-more">… 还有 {more} 个未完成工作块</li>'
        if rows:
            chunks_html = (
                '<div class="gate-section">'
                f'<div class="gate-section-title">未完成工作块 '
                f'（{chunks.get("done", 0)}/{chunks.get("total", 0)} 已完成，'
                f'剩余 {chunks.get("pending_count", 0)} 个）</div>'
                f'<ul class="gate-chunks">{rows}</ul></div>'
            )
        else:
            chunks_html = (
                '<div class="gate-section"><div class="gate-section-title">工作块</div>'
                f'<div class="gate-empty">全部 {chunks.get("total", 0)} 个已完成。</div></div>'
            )

    effects = []
    if review.get("approve_to"):
        names = "、".join(
            f"{t['name']}（{t['id']}）" for t in review["approve_to"]
        )
        effects.append(f"通过 → 进入 {names}")
    if review.get("reject_to"):
        names = "、".join(f"{t['name']}（{t['id']}）" for t in review["reject_to"])
        effects.append(f"拒绝 → 转交 {names}")
    else:
        effects.append("拒绝 → 当前成员返工（或在下方手动指定转交成员）")
    effects_html = "".join(f"<li>{esc(line)}</li>" for line in effects)

    excerpt = review.get("output_preview") or ""
    output_html = ""
    if excerpt:
        output_html = (
            '<details class="gate-output">'
            f'<summary>查看该成员本阶段输出摘要（共 {review.get("output_chars", 0)} 字，完整内容在「时间线」页签）</summary>'
            f'{copy_pre(review.get("output_excerpt") or excerpt)}</details>'
        )

    task_name_html = (
        f'<div class="gate-meta">通过后交接：{esc(review["task_name"])}</div>'
        if review.get("task_name") and review["task_name"] != "execute"
        else ""
    )

    return f"""\
<div class="gate-review">
  <div class="gate-meta"><strong>审批对象：</strong>{esc(subject)}</div>
  {task_name_html}
  <div class="gate-section">
    <div class="gate-section-title">需要确认的事项</div>
    {items_html}
  </div>
  {chunks_html}
  <div class="gate-section">
    <div class="gate-section-title">决策后果</div>
    <ul class="gate-effects">{effects_html}</ul>
  </div>
  {output_html}
  {f'<div class="gate-source">{source_note}</div>' if source_note else ''}
</div>"""



def render_task_detail(
    task: Task,
    agents: dict[str, AgentDefinition],
    context_md: str,
    task_project_dir: str = "",
    current_project_dir: str = "",
) -> str:
    short_id = task.id[:10]
    task_project_dir = task_project_dir or task.metadata.get("project_dir", "")
    is_current_project = not task_project_dir or task_project_dir == current_project_dir
    task_id_js = json.dumps(task.id).replace("<", "\\u003c")
    task_project_dir_js = json.dumps(
        task_project_dir or current_project_dir,
        ensure_ascii=False,
    ).replace("<", "\\u003c")
    show_live = is_current_project and task.status in (
        TaskStatus.in_progress,
        TaskStatus.awaiting_gate,
        TaskStatus.awaiting_human_input,
    )
    paused_now = bool((task.metadata or {}).get("paused"))

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
    pipeline_name = (task.metadata or {}).get("pipeline") or ""
    if pipeline_name:
        pipeline_html = (
            f'<a href="/pipelines?edit={esc(pipeline_name)}" '
            f'title="查看流水线 {esc(pipeline_name)}">{esc(pipeline_name)}</a>'
        )
    else:
        pipeline_html = (
            '<span style="color:var(--text-dim);" '
            'title="该任务未记录流水线（可能是命令行创建）">未记录</span>'
        )

    desc_html = render_markdown(task.description)
    if not desc_html:
        desc_html = '<span style="color:var(--text-dim);">-</span>'
    desc_style = "margin-top:6px;"
    if len(task.description or "") > 1200:
        desc_style += (
            "max-height:420px;overflow-y:auto;border:1px solid var(--border);"
            "border-radius:6px;padding:10px 14px;background:var(--surface2);"
        )

    chunk_total = int(task.metadata.get("chunks_total") or 0)
    chunk_done = int(task.metadata.get("chunks_done") or 0)
    if task.metadata.get("_chunk_split") == "running":
        chunk_overview_html = (
            '<span style="color:var(--text-dim);">正在按需求文档拆分工作块…</span>'
        )
    elif chunk_total:
        chunk_pct = round(chunk_done * 100 / chunk_total)
        chunk_overview_html = (
            f'<a href="#chunks" style="text-decoration:none;color:var(--text);">'
            f'{chunk_done}/{chunk_total}（{chunk_pct}%）</a>'
            '<div class="chunk-progress" style="max-width:320px;margin-top:6px;'
            'margin-bottom:0;">'
            f'<div class="chunk-progress-bar" style="width:{chunk_pct}%"></div></div>'
        )
    else:
        chunk_overview_html = '<span style="color:var(--text-dim);">未拆分</span>'

    meta = f"""\
<div class="card">
  <div class="meta-row">
    <div class="meta-item"><span class="meta-label">标识:</span> {esc(task.id)}</div>
    <div class="meta-item"><span class="meta-label">状态:</span> <span id="taskStatusBadge">{badge(display_status(task))}</span></div>
    <div class="meta-item"><span class="meta-label">代理:</span> {agent_tip(task.current_agent_id or '-')}</div>
    <div class="meta-item"><span class="meta-label">流水线:</span> {pipeline_html}</div>
    <div class="meta-item"><span class="meta-label">项目目录:</span> {esc(task_project_dir or current_project_dir)}</div>
  </div>
  <div class="meta-row">
    <div class="meta-item"><span class="meta-label">创建时间:</span> {fmt_time(task.created_at)}</div>
    <div class="meta-item"><span class="meta-label">更新时间:</span> {fmt_time(task.updated_at)}</div>
    <div class="meta-item"><span class="meta-label">阶段数:</span> {len(task.stages)}</div>
  </div>
  <div class="meta-row">
    <div class="meta-item" style="flex:1;">
      <span class="meta-label">工作块:</span>
      {chunk_overview_html}
    </div>
  </div>
  <div style="margin-top:12px;">
    <span class="meta-label">描述:</span>
    <div class="md-body" style="{desc_style}">{desc_html}</div>
  </div>
  {f'<div style="margin-top:8px;font-size:12px;color:var(--text-dim);">来源会议: <strong>{esc(task.metadata.get("source_meeting_topic", ""))}</strong>' + (f' · <a href="/meeting">查看会议</a>' if task.metadata.get("source_meeting_id") else '') + '</div>' if task.metadata.get("source_meeting_id") else ''}
</div>"""

    # ── Artifact files (written during this run) ──────────────────────
    files_card = ""
    files_root = task_project_dir or current_project_dir
    if files_root and (task.stages or task.status in (
        TaskStatus.completed,
        TaskStatus.failed,
        TaskStatus.cancelled,
        TaskStatus.approved,
        TaskStatus.rejected,
    )):
        artifacts = collect_task_files(files_root, task)
        if artifacts:
            rows = []
            for item in artifacts:
                rel = item["path"]
                abs_path = str((Path(files_root) / rel).resolve())
                rows.append(
                    f'<div class="artifact-row">'
                    f'<div class="artifact-main">'
                    f'<code class="artifact-path" title="{esc(abs_path)}">'
                    f'{esc(rel)}</code>'
                    f'<div class="artifact-desc">{esc(item.get("description") or "")}</div>'
                    f'</div>'
                    f'<span class="artifact-size">{format_size(item["size"])}</span>'
                    f'<button class="btn btn-sm" data-copy="{esc(abs_path)}" '
                    f'onclick="copyText(this)">复制路径</button>'
                    f'</div>'
                )
            body_html = f'<div class="artifact-list">{"".join(rows)}</div>'
        else:
            body_html = (
                '<div class="artifact-empty">'
                '未检测到本次任务期间修改的项目文件。'
                '</div>'
            )
        files_card = f"""\
<div class="card artifact-card">
  <h3 style="display:flex;align-items:center;gap:8px;">
    产物文件
    <span style="font-size:12px;font-weight:400;color:var(--text-dim);">
      本次任务期间新增 / 修改{(' · ' + str(len(artifacts)) + ' 个') if artifacts else ''}
    </span>
  </h3>
  {body_html}
</div>"""

    # ── Gate action panel ─────────────────────────────────────────────
    gate_actions = ""
    if is_current_project and task.status == TaskStatus.awaiting_gate:
        gate_review_html = _render_gate_review(
            build_gate_review(
                task, agents, task_project_dir or current_project_dir
            )
        )
        target_options = []
        for aid, ag in agents.items():
            if getattr(ag, "type", "agent") != "agent":
                continue
            label = ag.name or aid
            if ag.name and ag.name != aid:
                label = f"{ag.name}（{aid}）"
            target_options.append(
                f'<option value="{esc(aid)}">{esc(label)}</option>'
            )
        target_select = ""
        if target_options:
            target_select = f"""\
    <div class="form-group">
      <label>拒绝后转交给</label>
      <select class="gate-target">
        <option value="">自动（识别原因中的成员名 → 流水线配置 → 当前成员返工）</option>
        {''.join(target_options)}
      </select>
      <div style="font-size:12px;color:var(--text-dim);margin-top:4px;">
        自动模式会先从拒绝原因里识别成员名；识别不到时按流水线的
        on_reject 配置，最后回退到当前成员返工。也可以直接指定成员。
      </div>
    </div>"""
        gate_actions = f"""\
<div class="card" style="border-color:var(--orange);">
  <h3 style="color:var(--orange);"><span class="live-dot" style="background:var(--orange);"></span>等待审批</h3>
  {gate_review_html}
  <div style="margin-top:12px;">
    <div class="form-group">
      <label>原因 (拒绝时必填)</label>
      <input class="gate-reason" type="text" placeholder="可选原因...">
    </div>{target_select}
    <button class="btn btn-green" onclick="gateAction('approve', this)">通过</button>
    <button class="btn btn-red" style="margin-left:8px;" onclick="gateAction('reject', this)">拒绝</button>
  </div>
</div>"""

    # ── Human input panel ─────────────────────────────────────────────
    human_input_panel = ""
    if is_current_project and task.status == TaskStatus.awaiting_human_input:
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
      <label>您的回复</label>
      <textarea class="human-input-field" rows="4" placeholder="输入您的回复..."></textarea>
    </div>
    <button class="btn btn-primary" onclick="submitHumanInput(this)">提交回复</button>
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
  {f'<p style="font-size:12px;color:var(--text-dim);">任务所属项目：{esc(task_project_dir)}</p>' if task_project_dir and not is_current_project else ''}
  <div style="margin-top:12px;">
    <div class="form-group">
      <label for="restartStage">从阶段重启</label>
      <select id="restartStage">
        <option value="">默认 (自动检测)</option>
        {stage_options}
      </select>
    </div>
    <div class="form-group">
      <label for="restartNote">补充说明 / 需要确认的内容（会作为长期指令传给成员）</label>
      <textarea id="restartNote" rows="3" placeholder="例如：说明本次重跑的范围与要求；保留已完成部分，只补未完成项。"></textarea>
    </div>
    <button class="btn btn-green" onclick="restartTask()">重启</button>
  </div>
</div>"""

    # ── Human confirmation card ───────────────────────────────────────
    # A stalled/failed task often ends with the agent asking something like
    # "this needs a human decision".  Put that question and a reply box at the
    # very top of the overview tab so it is obvious where to answer.
    confirm_statuses = {
        TaskStatus.in_progress,
        TaskStatus.awaiting_gate,
        TaskStatus.awaiting_human_input,
        TaskStatus.stalled,
        TaskStatus.failed,
        TaskStatus.rejected,
    }
    confirm_snippet = ""
    confirm_card = ""
    if task.status in confirm_statuses:
        pending = task.metadata.get("_human_input_pending") or {}
        snippet_label = ""
        snippet_text = ""
        questions = pending.get("questions") or []
        if questions:
            snippet_label = "代理正在等待你的回答"
            snippet_text = "\n".join(str(q) for q in questions)
        elif task.stages:
            last = task.stages[-1]
            tail = (last.output_text or "").strip()
            if tail and task.status in (
                TaskStatus.stalled,
                TaskStatus.failed,
                TaskStatus.rejected,
            ):
                snippet_label = (
                    f"最后一条回复（{last.agent_id} · 阶段 {last.stage_number}）"
                )
                snippet_text = tail[-900:]
        if snippet_text:
            confirm_snippet = (
                f'<div style="margin-top:10px;font-size:12px;color:var(--text-dim);">'
                f'{esc(snippet_label)}</div>'
                '<div style="margin-top:6px;max-height:220px;overflow-y:auto;'
                'white-space:pre-wrap;background:var(--surface2);'
                'border-left:3px solid var(--orange);border-radius:0 6px 6px 0;'
                'padding:10px 14px;font-size:13px;line-height:1.6;">'
                f'{esc(snippet_text)}</div>'
            )

        confirm_active = task.status in (
            TaskStatus.in_progress,
            TaskStatus.awaiting_gate,
            TaskStatus.awaiting_human_input,
        )
        if confirm_active and paused_now:
            confirm_hint = (
                "任务已暂停。填写后会先记录这条说明，点击「继续」后在下一个"
                "阶段边界生效；说明会长期保留，后续成员也会看到。"
            )
            confirm_btn = "发送插话"
        elif confirm_active:
            confirm_hint = (
                "任务正在运行。填写后点击「发送插话」，当前成员会立即停下并带着"
                "这条说明重新作答；说明会长期保留，后续成员也会看到。"
            )
            confirm_btn = "发送插话（立即生效）"
        else:
            confirm_hint = (
                "填写需要人工确认或补充的内容，点击「带说明重启」后任务会从"
                "合适阶段继续；说明会作为长期指令传给所有成员。"
            )
            confirm_btn = "带说明重启"
        confirm_card = f"""\
<div class="card confirm-card" style="border-color:var(--orange);">
  <h3 style="color:var(--orange);"><span class="live-dot" style="background:var(--orange);"></span>人工确认 / 补充说明</h3>
  <div style="margin-top:4px;font-size:13px;color:var(--text-dim);">{confirm_hint}</div>
  {confirm_snippet}
  <div class="form-group" style="margin-top:12px;">
    <label for="confirmNote">需要确认或补充的内容</label>
    <textarea id="confirmNote" rows="4" placeholder="例如：确认按现有结论交付；补充尚未覆盖的要求；在最终回复中列出仍需人工确认的事项。"></textarea>
  </div>
  <button class="btn btn-primary" onclick="submitConfirm(this)">{confirm_btn}</button>
</div>"""
        confirm_active_js = "true" if confirm_active else "false"
    else:
        confirm_active_js = "false"

    # ── Live progress panel ───────────────────────────────────────────
    live_panel = ""
    if show_live:
        panel_title = (
            "流水线已暂停" if paused_now
            else "流水线运行中" if task.status == TaskStatus.in_progress
            else "等待人工输入" if task.status == TaskStatus.awaiting_human_input
            else "等待审批"
        )
        # Member roster — mirrors the resident sessions ("启动中" → "常驻中").
        roster_palette = [
            '#e06c75', '#61afef', '#98c379', '#e5c07b', '#c678dd',
            '#56b6c2', '#d19a66', '#7f9cf5', '#f78c6c', '#4ec9b0',
        ]
        roster_cards = []
        for index, member in enumerate(
            a for a in agents.values() if getattr(a, "type", "agent") == "agent"
        ):
            color = roster_palette[index % len(roster_palette)]
            roster_cards.append(
                f'<div class="roster-card" data-agent-id="{esc(member.id)}" '
                f'data-color="{color}" data-state="waiting" '
                f'style="border-left-color:{color};">'
                f'<div class="r-name" style="color:{color};">'
                f'{esc(member.name or member.id)}</div>'
                f'<div class="r-meta">{esc(member.runtime or "")}</div>'
                f'<div class="r-state"><span class="r-dot"></span>'
                f'<span class="r-label">待启动</span></div>'
                f'</div>'
            )
        roster_html = (
            '<div id="liveRoster" class="live-roster">'
            + "".join(roster_cards) + "</div>"
            if roster_cards else ""
        )
        live_panel = f"""\
<div class="card" style="border-color:var(--accent);">
  <h3 id="liveTitle"><span class="live-dot"></span>{panel_title}</h3>
  <div class="progress-bar" style="margin-top:8px;"><div class="fill" id="progressFill" style="width:0%"></div></div>
  <div id="liveStatus" style="margin-top:8px;font-size:13px;color:var(--text-dim);"></div>
  {roster_html}
  <div id="livePlaceholder" style="margin-top:12px;font-size:13px;color:var(--text-dim);">等待代理输出…</div>
  <div id="liveStream" class="live-stream"></div>
  <div class="live-now running" id="liveNow">
    <span class="now-dot" id="liveNowDot"></span>
    <span class="now-agent" id="liveNowAgent">当前成员</span>
    <span class="now-state" id="liveNowState">等待输出</span>
    <span class="now-timer" id="liveNowTimer">⏱ 0s</span>
  </div>
  <div class="human-speak" style="margin-top:14px;">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
      <span class="human-speak-avatar">我</span>
      <div>
        <div style="font-weight:600;font-size:14px;">随时插话</div>
        <div style="font-size:12px;color:var(--text-dim);">立即打断当前成员并重新回答；要求同时持续影响后续阶段与重启</div>
      </div>
    </div>
    <textarea id="interjectInput" rows="3" placeholder="纠正方向、补充要求、提出疑问…（Enter 发送，Shift+Enter 换行）"></textarea>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-top:8px;gap:12px;">
      <span id="interjectHint" style="font-size:12px;color:var(--text-dim);"></span>
      <button class="btn btn-primary btn-sm" id="interjectBtn" onclick="sendInterjection()">发送插话</button>
    </div>
  </div>
  <button class="btn btn-red btn-sm" style="margin-top:12px;" id="cancelBtn" onclick="cancelRunningTask()">停止</button>
</div>"""

    # ── Stage timeline ────────────────────────────────────────────────
    # Human notes live in the timeline, not permanently in the live stream.
    # After a full restart the task begins a new run: notes applied in the
    # previous run are kept in metadata (they still feed the prompt) but are
    # no longer shown as events of this run.
    def _iso(value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None

    run_started = _iso((task.metadata or {}).get("run_started_at") or "")
    interject_log = [
        entry
        for entry in (task.metadata.get("interjection_log") or [])
        if isinstance(entry, dict)
        and entry.get("text")
        and not is_pure_nudge(entry.get("text", ""))
        and (
            run_started is None
            or _iso(entry.get("at") or "") is None
            or _iso(entry.get("at") or "") >= run_started
        )
    ]

    def timeline_note(entry: dict) -> str:
        when = fmt_time(entry.get("at") or "")
        return (
            '<div class="timeline-note">'
            '<div class="timeline-note-head">'
            '<span class="tn-who">我</span>'
            '<span class="tn-title">人类插话</span>'
            f'<span class="tn-time">{esc(when)}</span>'
            '</div>'
            f'<div class="tn-text">{esc(entry.get("text", ""))}</div>'
            '</div>'
        )

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
        notes_html = "".join(
            timeline_note(entry)
            for entry in interject_log
            if entry.get("stage_number") == s.stage_number
        )

        items.append(
            f'<div class="timeline-item {status_class}">'
            f'<div><strong>阶段 {s.stage_number}</strong> · '
            f'{agent_tip(s.agent_id)}'
            f'{gate_info}{retry_info}{duration_html}</div>'
            f'<div style="font-size:12px;color:var(--text-dim);">'
            f'{fmt_time(s.started_at)} → {fmt_time(s.finished_at)}</div>'
            f'<div style="font-size:13px;margin-top:4px;color:var(--text-dim);">{output_preview}</div>'
            f'{notes_html}'
             f'<details><summary>完整输入</summary>{copy_pre(s.input_text)}</details>'
             f'<details><summary>完整输出</summary>{copy_pre(s.output_text)}</details>'
            f'</div>'
        )

    # Notes recorded before per-stage attribution existed (or applied between
    # stages) still belong in the timeline.
    recorded_stages = {s.stage_number for s in task.stages}
    for entry in interject_log:
        if entry.get("stage_number") in recorded_stages:
            continue
        items.append(
            '<div class="timeline-item">'
            f'<div><strong>人类插话</strong> · {agent_tip(entry.get("agent_id", ""))}</div>'
            f'{timeline_note(entry)}'
            '</div>'
        )

    if not items:
        timeline = '<div class="timeline" id="liveTimeline"><div class="empty-state" style="padding:24px;">暂无记录阶段。</div></div>'
    else:
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
    chunk_owner_json = json.dumps(
        [
            {"id": aid, "label": (a.name or aid)}
            for aid, a in agents.items()
        ],
        ensure_ascii=False,
    )
    chunks_section = f"""\
<div id="chunksPanel">
  <div class="chunk-head">
    <div>
      <div class="chunk-title">工作块</div>
      <div id="chunkProgressText" class="chunk-sub">加载中…</div>
    </div>
    <div class="chunk-actions">
      <button class="btn btn-sm" onclick="splitChunks()" title="按需求文档重新拆分">重新拆分</button>
      <button class="btn btn-sm" onclick="completeAllChunks()">全部完成</button>
      <button class="btn btn-sm" onclick="resetChunks()">重置</button>
      <button class="btn btn-sm btn-red" onclick="clearChunks()">清空</button>
    </div>
  </div>
  <div class="chunk-progress"><div id="chunkProgressBar" class="chunk-progress-bar" style="width:0%"></div></div>
  <div class="chunk-add">
    <input id="newChunkDesc" type="text" placeholder="新工作块描述…（Enter 添加）" style="flex:1;">
    <input id="newChunkOwner" type="text" placeholder="负责人（可选）" style="width:130px;">
    <input id="newChunkPhase" type="text" placeholder="阶段（可选）" style="width:110px;">
    <button class="btn btn-sm btn-primary" onclick="addChunk()">添加</button>
  </div>
  <div id="chunkSplitState" class="chunk-split-state" style="display:none;"></div>
  <div id="chunkList"><div class="empty-state" style="padding:16px;font-size:13px;">加载工作块中…</div></div>
</div>
<script>
const CHUNK_STATUS = {{pending:'待处理', in_progress:'进行中', done:'已完成', blocked:'已阻塞'}};
const CHUNK_OWNERS = {chunk_owner_json};
const TASK_STATUS = '{esc(task.status.value)}';
const TASK_ACTIVE = ['in_progress','awaiting_gate','awaiting_human_input'].includes(TASK_STATUS);
// Whether this render includes the live-output panel + SSE stream. A page
// rendered while the task was inactive has neither, so navigating to #output
// only switches tabs and would stay empty until a manual refresh.
const LIVE_RENDERED = {"true" if show_live else "false"};
let chunkCache = [];

function openLiveOutput(taskUrl) {{
  if (LIVE_RENDERED) {{
    window.location.assign(taskUrl + '#output');
    return;
  }}
  // Force a real navigation (different query string), not just a hash change:
  // the server must re-render the page with the live panel and open the SSE
  // connection. Without this the user had to press F5 to see the pipeline.
  const sep = taskUrl.includes('?') ? '&' : '?';
  window.location.assign(
    taskUrl + sep + 'view=live&t=' + Date.now() + '#output'
  );
}}

function ownerSelectHtml(c) {{
  const parts = ['<option value="">未分配</option>'];
  let matched = false;
  for (const owner of CHUNK_OWNERS) {{
    const selected = c.owner === owner.id;
    if (selected) matched = true;
    parts.push(
      `<option value="${{chunkEscape(owner.id)}}" ${{selected ? 'selected' : ''}}>` +
      `${{chunkEscape(owner.label)}}（${{chunkEscape(owner.id)}}）</option>`
    );
  }}
  if (c.owner && !matched) {{
    parts.push(
      `<option value="${{chunkEscape(c.owner)}}" selected>` +
      `${{chunkEscape(c.owner)}}（未定义）</option>`
    );
  }}
  return `<select class="chunk-owner-select" title="负责人" ` +
    `onchange="updateChunkOwner('${{c.id}}', this.value)">${{parts.join('')}}</select>`;
}}

function chunkEscape(value) {{
  return String(value == null ? '' : value).replace(/[&<>"']/g, ch => ({{
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }})[ch]);
}}

function chunkProgressHtml(p) {{
  const el = document.getElementById('chunkProgressText');
  if (el) el.textContent = p.total
    ? `已完成 ${{p.done}}/${{p.total}}（${{p.percent}}%）· 进行中 ${{p.in_progress}} · 阻塞 ${{p.blocked}}`
    : '暂无工作块，可手动添加或按需求文档拆分';
  const bar = document.getElementById('chunkProgressBar');
  if (bar) bar.style.width = `${{p.total ? p.percent : 0}}%`;
}}

function chunkRow(c) {{
  const owner = c.owner || '未分配';
  const note = c.note ? `<div class="chunk-note" title="备注">${{chunkEscape(c.note)}}</div>` : '';
  return `
    <div class="chunk-item" data-id="${{c.id}}" data-owner="${{chunkEscape(owner)}}">
      <span class="chunk-id">${{c.id}}</span>
      <select onchange="updateChunk('${{c.id}}', this.value)" class="chunk-status chunk-status-${{c.status}}">
        <option value="pending" ${{c.status==='pending'?'selected':''}}>待处理</option>
        <option value="in_progress" ${{c.status==='in_progress'?'selected':''}}>进行中</option>
        <option value="done" ${{c.status==='done'?'selected':''}}>已完成</option>
        <option value="blocked" ${{c.status==='blocked'?'selected':''}}>已阻塞</option>
      </select>
      <span class="chunk-desc">${{chunkEscape(c.description)}}${{note}}</span>
      ${{ownerSelectHtml(c)}}
      <span class="chunk-ops">
        <button class="chunk-op chunk-op-run" title="开始处理（唤醒负责人）" onclick="startChunk('${{c.id}}')">▶</button>
        <button class="chunk-op" title="编辑描述" onclick="editChunk('${{c.id}}')">✎</button>
        <button class="chunk-op" title="上移" onclick="moveChunk('${{c.id}}', -1)">↑</button>
        <button class="chunk-op" title="下移" onclick="moveChunk('${{c.id}}', 1)">↓</button>
        <button class="chunk-op chunk-op-del" title="删除" onclick="removeChunk('${{c.id}}')">✕</button>
      </span>
    </div>`;
}}

function renderChunks(chunks) {{
  chunkCache = chunks || [];
  const el = document.getElementById('chunkList');
  if (!el) return;
  if (!chunkCache.length) {{
    el.innerHTML = '<div class="empty-state" style="padding:16px;font-size:13px;">暂无工作块。点击「重新拆分」按需求文档自动生成。</div>';
    return;
  }}
  const groups = new Map();
  for (const c of chunkCache) {{
    const key = c.owner || '未分配';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(c);
  }}
  let html = '';
  for (const [owner, items] of groups) {{
    const done = items.filter(c => c.status === 'done').length;
    html += `<div class="chunk-group-head"><span>${{chunkEscape(owner)}}</span><span class="chunk-group-count">${{done}}/${{items.length}}</span></div>`;
    html += items.map(chunkRow).join('');
  }}
  el.innerHTML = html;
}}

async function loadChunks() {{
  try {{
    const chunks = await apiFetch('/api/tasks/{esc(task.id)}/chunks');
    renderChunks(chunks);
    try {{
      const meta = await apiFetch('/api/tasks/{esc(task.id)}/chunks/summary');
      chunkProgressHtml(meta.progress || {{}});
    }} catch(e) {{}}
  }} catch(e) {{}}
}}

async function addChunk() {{
  const input = document.getElementById('newChunkDesc');
  const desc = (input.value || '').trim();
  if (!desc) return;
  const owner = (document.getElementById('newChunkOwner').value || '').trim();
  const phase = (document.getElementById('newChunkPhase').value || '').trim();
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/chunks', {{method:'POST', body:JSON.stringify({{description:desc, owner, phase}})}});
    input.value = '';
    loadChunks();
  }} catch(e) {{ alert('添加失败：' + e.message); }}
}}

document.getElementById('newChunkDesc')?.addEventListener('keydown', (e) => {{
  if (e.key === 'Enter') addChunk();
}});

async function updateChunk(chunkId, status) {{
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/chunks/' + chunkId, {{method:'PATCH', body:JSON.stringify({{status}})}});
    loadChunks();
  }} catch(e) {{ alert('更新失败：' + e.message); }}
}}

async function updateChunkOwner(chunkId, owner) {{
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/chunks/' + chunkId, {{
      method:'PATCH', body:JSON.stringify({{owner}})
    }});
    loadChunks();
  }} catch(e) {{
    alert('设置负责人失败：' + e.message);
    loadChunks();
  }}
}}

async function startChunk(chunkId) {{
  const chunk = chunkCache.find(c => c.id === chunkId) || {{}};
  if (!chunk.owner) {{
    alert('请先在负责人下拉框中选择成员，再开始处理。');
    return;
  }}
  const taskUrl = '/tasks/{esc(task.id)}';
  if (TASK_ACTIVE) {{
    // Already running: the chunk will be picked up by the current/next stage.
    // Jump to the live output so the user can watch (and interject if needed).
    showToast('任务正在运行，正在打开实时输出');
    openLiveOutput(taskUrl);
    return;
  }}
  if (!confirm(`将任务启动/重启到「${{chunk.owner}}」阶段，并优先处理 ${{chunkId}}？`)) return;
  setChunkSplitState(`正在唤醒 ${{chunk.owner}} 处理 ${{chunkId}}…`);
  try {{
    const result = await apiFetch(
      '/api/tasks/{esc(task.id)}/chunks/' + chunkId + '/start',
      {{method:'POST', body:JSON.stringify({{owner: chunk.owner}})}}
    );
    setChunkSplitState(result.message || '已开始处理');
    // Wait until the backend flips the task to an active state, then open the
    // live-output tab (full navigation with an explicit hash, which is more
    // reliable than setting location.hash and calling reload()).
    const wait = await waitForTaskActive({task_id_js}, 30000);
    if (wait.active) {{
      openLiveOutput(taskUrl);
      return;
    }}
    setChunkSplitState(
      '任务尚未进入运行状态（当前：' + (wait.status || '未知') +
      '），请稍后重试或在时间线查看错误。', true
    );
  }} catch(e) {{
    if (e && e.status === 409 && e.detail && e.detail.code === 'task_active') {{
      showToast('任务正在运行，正在打开实时输出');
      openLiveOutput(taskUrl);
      return;
    }}
    setChunkSplitState('启动失败：' + e.message, true);
  }}
}}

async function waitForTaskActive(taskId, timeoutMs) {{
  const active = ['in_progress', 'awaiting_gate', 'awaiting_human_input'];
  const deadline = Date.now() + (timeoutMs || 20000);
  let last = '';
  while (Date.now() < deadline) {{
    await new Promise(r => setTimeout(r, 500));
    try {{
      const r = await apiFetch('/api/tasks/' + encodeURIComponent(taskId));
      last = (r && r.status) || last;
      if (active.includes(last)) return {{active: true, status: last}};
    }} catch (e) {{}}
  }}
  return {{active: false, status: last}};
}}

async function editChunk(chunkId) {{
  const current = (chunkCache.find(c => c.id === chunkId) || {{}}).description || '';
  const next = prompt('修改工作块描述', current);
  if (next === null || !next.trim() || next === current) return;
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/chunks/' + chunkId, {{method:'PATCH', body:JSON.stringify({{description:next.trim()}})}});
    loadChunks();
  }} catch(e) {{ alert('修改失败：' + e.message); }}
}}

async function removeChunk(chunkId) {{
  if (!confirm('确定删除 ' + chunkId + ' 吗？')) return;
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/chunks/' + chunkId, {{method:'DELETE'}});
    loadChunks();
  }} catch(e) {{ alert('删除失败：' + e.message); }}
}}

async function moveChunk(chunkId, delta) {{
  const ids = chunkCache.map(c => c.id);
  const index = ids.indexOf(chunkId);
  const target = index + delta;
  if (index < 0 || target < 0 || target >= ids.length) return;
  ids.splice(target, 0, ids.splice(index, 1)[0]);
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/chunks/reorder', {{method:'POST', body:JSON.stringify({{order:ids}})}});
    loadChunks();
  }} catch(e) {{ alert('排序失败：' + e.message); }}
}}

async function completeAllChunks() {{
  if (!chunkCache.length || !confirm('把所有工作块标记为已完成？')) return;
  for (const c of chunkCache) {{
    if (c.status !== 'done') {{
      try {{
        await apiFetch('/api/tasks/{esc(task.id)}/chunks/' + c.id, {{method:'PATCH', body:JSON.stringify({{status:'done'}})}});
      }} catch(e) {{}}
    }}
  }}
  loadChunks();
}}

async function resetChunks() {{
  if (!confirm('把所有未完成的工作块重置为待处理？')) return;
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/chunks/reset', {{method:'POST', body:JSON.stringify({{keep_done:true}})}});
    loadChunks();
  }} catch(e) {{ alert('重置失败：' + e.message); }}
}}

async function clearChunks() {{
  if (!confirm('确定清空全部工作块？此操作不可撤销。')) return;
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/chunks?confirm=1', {{method:'DELETE'}});
    loadChunks();
  }} catch(e) {{ alert('清空失败：' + e.message); }}
}}

async function splitChunks() {{
  if (!confirm('按需求文档重新拆分工作块？已完成的块和人工添加的块会保留。')) return;
  setChunkSplitState('正在按需求文档拆分工作块…（可能需要一两分钟）');
  const taskId = '{esc(task.id)}';
  try {{
    const result = await apiFetch('/api/tasks/' + taskId + '/chunks/split', {{
      method:'POST', body:JSON.stringify({{replace:true, keep_done:true}})
    }});
    if (result && result.status === 'failed') {{
      setChunkSplitState('拆分失败：' + (result.error || '未知错误'), true);
      return;
    }}
    setChunkSplitState('已生成 ' + (result.created || 0) + ' 个工作块', false);
    setTimeout(() => setChunkSplitState(''), 4000);
    loadChunks();
  }} catch(e) {{
    setChunkSplitState('拆分失败：' + e.message, true);
  }}
}}

function setChunkSplitState(text, isError) {{
  const state = document.getElementById('chunkSplitState');
  if (!state) return;
  if (!text) {{ state.style.display = 'none'; state.textContent = ''; return; }}
  state.style.display = 'block';
  state.textContent = text;
  state.classList.toggle('chunk-split-error', !!isError);
}}

document.addEventListener('DOMContentLoaded', loadChunks);
</script>"""

    # ── Fix form ──────────────────────────────────────────────────────
    fix_section = ""
    if is_current_project and task.status in (TaskStatus.completed, TaskStatus.failed):
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
    # Tab: Overview (meta + artifact files + action panels + fix)
    tab_overview = (
        f"{meta}\n{confirm_card}\n{files_card}\n{gate_actions}\n"
        f"{human_input_panel}\n{restart_panel}\n{fix_section}"
    )

    # Tab: Live Output (only visible when task is active)
    if show_live:
        # Approval / human-input panels belong on the tab that is open by
        # default while the task runs, otherwise the user has to hunt for
        # them on the overview tab.  A waiting gate also shows the artifacts
        # produced so far, so the reviewer knows what they are approving.
        gate_files = (
            f"{files_card}\n" if task.status == TaskStatus.awaiting_gate else ""
        )
        tab_output = f"{gate_actions}\n{gate_files}{human_input_panel}\n{live_panel}"
    else:
        tab_output = '<div class="empty-state" style="padding:24px;">任务当前未在运行。</div>'

    # Default tab
    default_tab = "tab-2" if show_live else "tab-1"

    # ── Top control bar (pause / stop) ────────────────────────────────
    active_statuses = {
        TaskStatus.in_progress,
        TaskStatus.awaiting_gate,
        TaskStatus.awaiting_human_input,
    }
    controls_bar = ""
    if is_current_project and task.status in active_statuses:
        if task.status == TaskStatus.awaiting_gate:
            state_label = "等待审批"
        elif task.status == TaskStatus.awaiting_human_input:
            state_label = "等待人工输入"
        else:
            state_label = "任务运行中"
        if paused_now:
            state_label = "已暂停"
        dot_color = "var(--orange)" if (paused_now or task.status != TaskStatus.in_progress) else "var(--green)"
        pause_label = "继续" if paused_now else "暂停"
        controls_bar = f"""\
<div class="task-controls" id="taskControls">
  <span class="live-dot" id="taskControlDot" style="background:{dot_color};"></span>
  <strong id="taskControlState">{state_label}</strong>
  <span class="task-controls-agent" id="taskControlAgent">{esc(task.current_agent_id or '')}</span>
  <span style="flex:1;"></span>
  <button class="btn btn-sm" id="pauseBtn" onclick="toggleTaskPause()">{pause_label}</button>
  <button class="btn btn-sm btn-red" id="stopBtn" onclick="stopTaskQuick()">停止</button>
</div>"""

    body = f"""\
<style>
.human-speak {{
  position: relative;
  margin: 14px 0 0;
  padding: 14px 16px 12px;
  border: 1px solid var(--border);
  border-left: 4px solid var(--accent);
  border-radius: 10px;
  background: linear-gradient(180deg, rgba(88, 166, 255, 0.09), rgba(88, 166, 255, 0) 70%), var(--surface);
  transition: border-color .2s, box-shadow .2s;
}}
.human-speak-avatar {{
  width: 28px; height: 28px; flex: none;
  display: flex; align-items: center; justify-content: center;
  border-radius: 50%; background: var(--accent); color: #fff;
  font-size: 13px; font-weight: 700;
  box-shadow: 0 2px 6px rgba(88, 166, 255, .35);
}}
.human-speak textarea {{
  width: 100%; min-height: 76px; max-height: 32vh; resize: vertical;
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 12px; color: var(--text);
  font-size: 14px; line-height: 1.6; font-family: inherit;
  transition: border-color .15s, box-shadow .15s;
}}
.human-speak textarea::placeholder {{ color: var(--text-dim); }}
.human-speak textarea:focus {{
  border-color: var(--accent); outline: none;
  box-shadow: 0 0 0 3px rgba(88, 166, 255, .15);
}}
.human-speak .btn:disabled {{ opacity: .5; cursor: not-allowed; }}

/* ── Top control bar: pause / stop ── */
.task-controls {{
  position: sticky; top: 52px; z-index: 40;
  display: flex; align-items: center; gap: 10px;
  margin: 10px 0 14px; padding: 10px 14px;
  background: var(--surface); border: 1px solid var(--border);
  border-left: 4px solid var(--green); border-radius: 8px;
  box-shadow: 0 2px 10px rgba(0,0,0,.18);
}}
.task-controls.is-paused {{ border-left-color: var(--orange); }}
.task-controls-agent {{ font-size: 12px; color: var(--text-dim); }}
.task-controls .btn {{ flex: none; }}

/* ── Pending approval checklist ── */
.gate-review {{
  margin-top: 12px; padding: 14px 16px;
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: 8px;
}}
.gate-meta {{ font-size: 13px; color: var(--text-dim); margin-bottom: 4px; }}
.gate-meta strong {{ color: var(--text); }}
.gate-section {{ margin-top: 12px; }}
.gate-section-title {{
  font-size: 12px; font-weight: 700; letter-spacing: .04em;
  color: var(--orange); text-transform: uppercase; margin-bottom: 6px;
}}
.gate-items {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }}
.gate-item {{
  display: flex; align-items: flex-start; gap: 8px;
  font-size: 13px; line-height: 1.6;
  padding: 6px 10px; border-radius: 6px; background: var(--surface);
}}
.gate-item-mark {{
  flex: none; font-size: 11px; font-weight: 700; margin-top: 2px;
  min-width: 42px;
}}
.gate-item-conclusion {{ border-left: 3px solid var(--cyan); }}
.gate-item-done {{ opacity: .75; }}
.gate-item-done .gate-item-mark {{ min-width: 12px; }}
.gate-chunks {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }}
.gate-chunks li {{ font-size: 13px; line-height: 1.5; }}
.gate-chunk-status {{ color: var(--text-dim); font-size: 12px; }}
.gate-more {{ color: var(--text-dim); }}
.gate-effects {{ margin: 0; padding-left: 18px; font-size: 13px; line-height: 1.7; color: var(--text-dim); }}
.gate-output {{ margin-top: 12px; font-size: 13px; }}
.gate-output summary {{ cursor: pointer; color: var(--text-dim); }}
.gate-source {{ margin-top: 10px; font-size: 12px; color: var(--text-dim); }}
.gate-source code {{ background: var(--surface); padding: 1px 5px; border-radius: 4px; }}
.gate-empty {{ font-size: 13px; color: var(--text-dim); }}

/* ── Live streaming blocks (meeting-style) ── */
.live-stream {{
  margin-top: 10px;
  max-height: 62vh;
  overflow-y: auto;
  padding-right: 4px;
  display: none;
}}
.live-stream.has-blocks {{ display: block; }}
.live-block {{
  border-left: 4px solid var(--border);
  border-radius: 4px;
  padding: 8px 12px;
  margin: 10px 0;
}}
.live-block .live-head {{
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  font-weight: 600; font-size: 13px; margin-bottom: 4px;
}}
.live-head .live-tag {{
  font-size: 11px; font-weight: 500; padding: 1px 7px; border-radius: 10px;
  background: rgba(255,255,255,.06); border: 1px solid var(--border); color: var(--text-dim);
}}
.live-head .live-state {{ font-size: 11px; font-weight: 500; color: var(--text-dim); }}
.live-head .live-timer {{
  font-size: 11px; font-weight: 600; color: var(--orange);
  font-variant-numeric: tabular-nums;
  padding: 1px 7px; border-radius: 10px;
  background: rgba(210,153,34,.12); border: 1px solid rgba(210,153,34,.35);
}}
.live-block .live-body {{
  display: block; white-space: pre-wrap; word-break: break-word;
  font-size: 13px; line-height: 1.65;
}}
.live-block .live-think {{
  margin: 4px 0 6px; border: 1px dashed var(--border);
  border-radius: 4px; padding: 4px 8px;
}}
.live-block .live-think summary {{
  cursor: pointer; font-size: 12px; color: var(--text-dim); user-select: none;
}}
.live-block .live-think pre {{
  margin: 6px 0 0; font-size: 12px; line-height: 1.5;
  white-space: pre-wrap; word-break: break-word; color: var(--text-dim);
  max-height: 320px; overflow-y: auto;
}}
.live-block .live-think.live-actions {{ border-style: solid; }}
.live-block .live-think.live-actions pre {{ max-height: 200px; color: var(--text); }}
.live-block.live-note {{
  border-left-style: dashed; border-left-width: 3px;
  background: rgba(88,166,255,.08);
  font-size: 13px; color: var(--text-dim);
}}
.live-block.live-note strong {{ color: var(--accent); }}
.live-block.live-note.live-note-pending {{
  border-left-color: var(--orange);
  background: rgba(210,153,34,.09);
}}
.live-block.live-note.live-note-applied {{
  border-left-color: var(--green);
  background: rgba(63,185,80,.09);
}}
.live-note-head {{
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  margin-bottom: 5px;
}}
.live-note-who {{
  width: 22px; height: 22px; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  background: rgba(88,166,255,.22); color: var(--accent);
  font-size: 11px; font-weight: 700; flex: none;
}}
.live-note-title {{ font-weight: 600; font-size: 13px; color: var(--text); }}
.live-note-tag {{
  font-size: 11px; padding: 1px 8px; border-radius: 10px;
  border: 1px solid var(--border); background: rgba(255,255,255,.05);
  color: var(--text-dim); white-space: nowrap;
}}
.live-note-tag.pending {{
  color: var(--orange); border-color: rgba(210,153,34,.45);
  background: rgba(210,153,34,.12);
}}
.live-note-tag.applied {{
  color: var(--green); border-color: rgba(63,185,80,.45);
  background: rgba(63,185,80,.12);
}}
.live-note-time {{
  margin-left: auto; font-size: 11px; color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}}
.live-note-text {{ white-space: pre-wrap; word-break: break-word; color: var(--text); line-height: 1.6; }}
.live-note-meta {{ margin-top: 5px; font-size: 11.5px; color: var(--text-dim); }}
.live-block.live-error {{ border-left-color: var(--red); background: rgba(248,81,73,.08); }}
.live-block.live-error .live-body {{ color: var(--red); }}

/* ── "Now" bar: current member + timer, sticks to the bottom of the viewport ── */
.live-now {{
  position: sticky;
  bottom: 10px;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  padding: 9px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: rgba(22, 27, 34, .93);
  backdrop-filter: blur(6px);
  box-shadow: 0 8px 22px rgba(0, 0, 0, .35);
  font-size: 12px;
}}
.live-now .now-dot {{
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--accent); flex: none;
}}
.live-now.running .now-dot {{ animation: now-pulse 1.4s ease-in-out infinite; }}
.live-now.done .now-dot {{ background: var(--green); }}
.live-now.failed .now-dot {{ background: var(--red); }}
@keyframes now-pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: .3; }} }}
.live-now .now-agent {{
  font-weight: 600; color: var(--text);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  max-width: 42%;
}}
.live-now .now-state {{
  color: var(--text-dim); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis;
}}
.live-now .now-timer {{
  flex: none;
  font-weight: 600; color: var(--orange);
  font-variant-numeric: tabular-nums;
  padding: 2px 8px; border-radius: 10px;
  background: rgba(210,153,34,.12); border: 1px solid rgba(210,153,34,.35);
  white-space: nowrap;
}}

/* ── Member roster: resident-session startup states ── */
.live-roster {{
  display: flex; flex-wrap: wrap; gap: 8px;
  margin-top: 10px;
}}
.live-roster .roster-card {{
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-left: 4px solid var(--border);
  border-radius: 6px;
  background: var(--surface2);
  min-width: 118px;
}}
.live-roster .roster-card[data-state="running"] {{ border-color: var(--accent); }}
.live-roster .r-name {{ font-weight: 600; font-size: 12px; }}
.live-roster .r-meta {{ font-size: 11px; color: var(--text-dim); }}
.live-roster .r-state {{
  display: flex; align-items: center; gap: 5px;
  font-size: 11px; color: var(--text-dim); margin-top: 3px;
}}
.live-roster .r-dot {{
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--text-dim); flex: none;
}}
.live-roster [data-state="starting"] .r-dot {{ background: var(--accent); animation: now-pulse 1s ease-in-out infinite; }}
.live-roster [data-state="starting"] .r-state {{ color: var(--accent); }}
.live-roster [data-state="running"] .r-dot {{ background: var(--accent); animation: now-pulse 1s ease-in-out infinite; }}
.live-roster [data-state="running"] .r-state {{ color: var(--accent); }}
.live-roster [data-state="ready"] .r-dot {{ background: var(--green); }}
.live-roster [data-state="ready"] .r-state {{ color: var(--green); }}
.live-roster [data-state="failed"] .r-dot {{ background: var(--red); }}
.live-roster [data-state="failed"] .r-state {{ color: var(--red); }}

/* ── Task artifacts (files written during the run) ── */
.artifact-list {{ display: flex; flex-direction: column; gap: 6px; margin-top: 10px; }}
.artifact-row {{
  display: flex; align-items: center; gap: 10px;
  padding: 7px 10px; border: 1px solid var(--border);
  border-radius: 6px; background: var(--surface2);
}}
.artifact-main {{ flex: 1; min-width: 0; }}
.artifact-path {{
  display: block;
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 12.5px; color: var(--accent);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}}
.artifact-desc {{
  margin-top: 3px; font-size: 12px; color: var(--text-dim);
  line-height: 1.45;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}}
.artifact-size {{ flex: none; font-size: 11px; color: var(--text-dim); }}
.artifact-empty {{ margin-top: 8px; font-size: 12px; color: var(--text-dim); }}
</style>

<h1>Task {esc(short_id)}</h1>
{controls_bar}

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
const FORCE_OUTPUT = {"true" if (show_live and task.status in (TaskStatus.awaiting_gate, TaskStatus.awaiting_human_input)) else "false"};
function setTabFromHash() {{
  const hash = location.hash.replace('#', '');
  if (FORCE_OUTPUT && (!hash || hash === 'overview')) {{
    // Approval / human-input panels live on the live-output tab: never send
    // the user back to the overview while the task is waiting for them.
    document.getElementById('tab-2').checked = true;
    history.replaceState(null, '', '#output');
    return;
  }}
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
async function gateAction(action, trigger) {{
  const scope = trigger && trigger.closest ? trigger.closest('.card') : null;
  const field = scope ? scope.querySelector('.gate-reason') : document.getElementById('gateReason');
  const reason = field?.value?.trim() || '';
  if (action === 'reject' && !reason) {{ showToast('拒绝时需提供原因', 'error'); return; }}
  let target = '';
  if (action === 'reject') {{
    const picker = scope ? scope.querySelector('.gate-target') : null;
    target = picker?.value || '';
  }}
  const payload = {{reason: reason || undefined}};
  if (target) payload.target_agent_id = target;
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/' + action, {{method:'POST', body:JSON.stringify(payload)}});
    showToast(action === 'reject' && target
      ? '已拒绝并转交给 ' + target
      : '任务' + action + '已处理');
    location.hash = 'output';
    setTimeout(() => location.reload(), 600);
  }} catch(e) {{}}
}}

/* Human input */
async function submitHumanInput(trigger) {{
  const scope = trigger && trigger.closest ? trigger.closest('.card') : null;
  const field = scope ? scope.querySelector('.human-input-field') : document.getElementById('humanInput');
  const response = field?.value?.trim();
  if (!response) {{ showToast('请输入回复', 'error'); return; }}
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/human-input', {{method:'POST', body:JSON.stringify({{response}})}});
    showToast('回复已提交');
    location.hash = 'output';
    setTimeout(() => location.reload(), 600);
  }} catch(e) {{}}
}}

/* Restart */
async function restartTask() {{
  const stageVal = document.getElementById('restartStage')?.value;
  const note = (document.getElementById('restartNote')?.value || '').trim();
  try {{
    await restartTaskInProject(
      {task_id_js},
      {task_project_dir_js},
      stageVal ? parseInt(stageVal) : null,
      note,
    );
  }} catch(e) {{}}
}}

/* Human confirmation (works while running and after a stall) */
async function submitConfirm(btn) {{
  const el = document.getElementById('confirmNote');
  const text = (el?.value || '').trim();
  if (!text) {{ showToast('请先填写需要确认或补充的内容', 'error'); return; }}
  const label = btn ? btn.textContent : '';
  if ({confirm_active_js}) {{
    if (btn) {{ btn.disabled = true; btn.textContent = '发送中...'; }}
    try {{
      await apiFetch('/api/tasks/{esc(task.id)}/interject', {{
        method: 'POST', body: JSON.stringify({{ text }})
      }});
      el.value = '';
      showToast('已发送，当前成员会立即结合这条说明重新作答');
    }} catch (e) {{
      showToast('发送失败：' + e.message, 'error');
    }} finally {{
      if (btn) {{ btn.disabled = false; btn.textContent = label; }}
    }}
    return;
  }}
  if (!confirm('将带着这条说明重启任务，确定？')) return;
  if (btn) {{ btn.disabled = true; btn.textContent = '重启中...'; }}
  try {{
    await restartTaskInProject(
      {task_id_js},
      {task_project_dir_js},
      null,
      text,
    );
  }} catch(e) {{
    showToast('重启失败：' + e.message, 'error');
  }} finally {{
    if (btn) {{ btn.disabled = false; btn.textContent = label; }}
  }}
}}

/* Interjection */
async function sendInterjection() {{
  const el = document.getElementById('interjectInput');
  const btn = document.getElementById('interjectBtn');
  const hint = document.getElementById('interjectHint');
  const text = el?.value?.trim();
  if (!text) {{ showToast('请输入插话内容', 'error'); return; }}
  if (btn) {{ btn.disabled = true; btn.textContent = '发送中...'; }}
  // Show the note immediately; the SSE 'queued' event is deduplicated by id.
  const clientId = 'ij-' + Date.now().toString(36) + '-' +
    Math.random().toString(36).slice(2, 8);
  const pendingNode = window.addInterjection
    ? window.addInterjection(text, {{
        state: 'queued',
        clientId,
        immediate: !!(document.getElementById('liveNow') &&
          document.getElementById('liveNow').classList.contains('running'))
      }})
    : null;
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/interject', {{
      method: 'POST', body: JSON.stringify({{ text, client_id: clientId }})
    }});
    el.value = '';
    if (hint) hint.textContent = '已发送，当前成员会立即结合这条插话重新回答';
    showToast('插话已发送');
  }} catch (e) {{
    if (hint) hint.textContent = '';
    if (window.removeInterjection) window.removeInterjection(pendingNode);
  }} finally {{
    if (btn) {{ btn.disabled = false; btn.textContent = '发送插话'; }}
  }}
}}
document.getElementById('interjectInput')?.addEventListener('input', (e) => {{
  const hint = document.getElementById('interjectHint');
  if (hint && !hint.textContent.includes('已') && !hint.textContent.includes('正在')) {{
    hint.textContent = e.target.value.length ? e.target.value.length + ' 字' : '';
  }}
}});
// Enter sends the note; Shift+Enter keeps a newline.  Ignore Enter while an
// IME composition is still open so Chinese input is not submitted mid-word.
document.getElementById('interjectInput')?.addEventListener('keydown', (e) => {{
  if (e.key !== 'Enter' || e.shiftKey || e.isComposing || e.keyCode === 229) return;
  e.preventDefault();
  sendInterjection();
}});

/* Cancel */
async function cancelRunningTask() {{
  if (!confirm('停止此任务？当前成员正在生成的内容会被丢弃，任务标记为「已取消」。')) return;
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/cancel', {{method:'POST', body:'{{}}'}});
    showToast('已请求停止');
    setTimeout(() => location.reload(), 1000);
  }} catch(e) {{}}
}}

/* Top control bar: pause / resume / stop */
let TASK_PAUSED = {"true" if paused_now else "false"};
let LAST_LIVE_AGENT = '';
const TASK_BADGE_ORIGINAL = (() => {{
  const el = document.getElementById('taskStatusBadge');
  return el ? el.innerHTML : '';
}})();
function renderTaskControl() {{
  const bar = document.getElementById('taskControls');
  if (bar) {{
    const dot = document.getElementById('taskControlDot');
    const state = document.getElementById('taskControlState');
    const btn = document.getElementById('pauseBtn');
    const waitLabel = TASK_STATUS === 'awaiting_gate'
      ? '等待审批'
      : (TASK_STATUS === 'awaiting_human_input' ? '等待人工输入' : '');
    if (dot) {{
      dot.style.background = TASK_PAUSED
        ? 'var(--orange)'
        : (waitLabel ? 'var(--orange)' : 'var(--green)');
    }}
    if (state) {{
      state.textContent = TASK_PAUSED ? '已暂停' : (waitLabel || '任务运行中');
    }}
    if (btn) btn.textContent = TASK_PAUSED ? '继续' : '暂停';
    bar.classList.toggle('is-paused', TASK_PAUSED);
  }}
  // The meta card at the top of the page carries its own status badge;
  // keep it in sync without requiring a reload.
  const badgeEl = document.getElementById('taskStatusBadge');
  if (badgeEl) {{
    badgeEl.innerHTML = TASK_PAUSED
      ? '<span class="badge badge-paused">已暂停</span>'
      : TASK_BADGE_ORIGINAL;
  }}
}}
renderTaskControl();
async function toggleTaskPause() {{
  const btn = document.getElementById('pauseBtn');
  if (btn) btn.disabled = true;
  const wantPause = !TASK_PAUSED;
  try {{
    const r = await apiFetch(
      '/api/tasks/{esc(task.id)}/' + (wantPause ? 'pause' : 'unpause'),
      {{method:'POST', body:'{{}}'}}
    );
    // The server broadcasts task_paused/task_resumed *before* the HTTP
    // response is sent, so the SSE handler usually ran already. Assign the
    // authoritative result instead of flipping the local flag, otherwise the
    // two would cancel each other out and the button would look unchanged
    // until a manual refresh.
    TASK_PAUSED = r && r.status ? r.status === 'paused' : wantPause;
    renderTaskControl();
    showToast(r.message || (TASK_PAUSED ? '已暂停' : '已继续'));
  }} catch(e) {{
    showToast('操作失败：' + e.message, 'error');
  }} finally {{
    if (btn) btn.disabled = false;
  }}
}}
async function stopTaskQuick() {{
  const btn = document.getElementById('stopBtn');
  if (!confirm('停止此任务？当前成员正在生成的内容会被丢弃，任务标记为「已取消」。')) return;
  if (btn) btn.disabled = true;
  try {{
    await apiFetch('/api/tasks/{esc(task.id)}/cancel', {{
      method:'POST', body:JSON.stringify({{reason:'用户从控制条停止'}})
    }});
    showToast('已请求停止');
    setTimeout(() => location.reload(), 800);
  }} catch(e) {{
    showToast('停止失败：' + e.message, 'error');
    if (btn) btn.disabled = false;
  }}
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
        interject_history: list[dict[str, str]] = []
        _log = (task.metadata or {}).get("interjection_log") or []
        _seen: set[str] = set()
        for _entry in _log:
            if not isinstance(_entry, dict):
                continue
            _text = str(_entry.get("text") or "").strip()
            if not _text or is_pure_nudge(_text):
                continue
            _at = _iso(_entry.get("at") or "")
            if run_started is not None and _at is not None and _at < run_started:
                # From a previous run: still applied to prompts, but not an
                # event of the current run.
                continue
            interject_history.append({
                "text": _text,
                "state": "applied",
                "agent_id": str(_entry.get("agent_id") or ""),
                "at": str(_entry.get("at") or ""),
            })
            _seen.add(_text)
        for _entry in (task.metadata or {}).get("interjections") or []:
            _text = str(_entry).strip()
            if not _text or _text in _seen or is_pure_nudge(_text):
                continue
            interject_history.append({
                "text": _text, "state": "applied", "agent_id": "", "at": "",
            })
            _seen.add(_text)
        for _entry in (task.metadata or {}).get("pending_interjections") or []:
            _text = str(_entry).strip()
            if not _text or _text in _seen or is_pure_nudge(_text):
                continue
            interject_history.append({
                "text": _text, "state": "pending", "agent_id": "", "at": "",
            })
        interject_history_js = json.dumps(interject_history, ensure_ascii=False).replace("</", "<\\/")
        sse_js = f"""\
<script>
(function() {{
  const es = new EventSource('/api/tasks/{esc(task.id)}/events');
  const statusEl = document.getElementById('liveStatus');
  const streamEl = document.getElementById('liveStream');
  const placeholderEl = document.getElementById('livePlaceholder');
  const titleEl = document.getElementById('liveTitle');
  const nowEl = document.getElementById('liveNow');
  const nowDotEl = document.getElementById('liveNowDot');
  const nowAgentEl = document.getElementById('liveNowAgent');
  const nowStateEl = document.getElementById('liveNowState');
  const nowTimerEl = document.getElementById('liveNowTimer');
  const nowBar = {{ startedAt: null, finishedAt: null }};
  const taskId = '{esc(task.id)}';
  const rosterEl = document.getElementById('liveRoster');
  const INTERJECT_HISTORY = {interject_history_js};
  let currentBlock = null;
  const activeBlocks = new Set();
  const AGENT_COLORS = ['#e06c75', '#61afef', '#98c379', '#e5c07b', '#c678dd',
                        '#56b6c2', '#d19a66', '#7f9cf5', '#f78c6c', '#4ec9b0'];
  const agentColorMap = {{}};
  let agentColorSeq = 0;
  // Seed colors from the server-rendered roster so member chips and output
  // blocks always use the same color for the same agent.
  if (rosterEl) {{
    rosterEl.querySelectorAll('[data-agent-id]').forEach(el => {{
      const id = el.getAttribute('data-agent-id');
      const color = el.getAttribute('data-color');
      if (id && color && !agentColorMap[id]) agentColorMap[id] = color;
    }});
    agentColorSeq = Object.keys(agentColorMap).length;
  }}

  function agentColor(agentId, name) {{
    const key = agentId || ('name:' + (name || ''));
    if (!agentColorMap[key]) {{
      agentColorMap[key] = AGENT_COLORS[agentColorSeq % AGENT_COLORS.length];
      agentColorSeq += 1;
    }}
    return agentColorMap[key];
  }}
  function agentLabel(name, runtime) {{
    const n = String(name || '');
    const r = String(runtime || '');
    if (!r || r.toLowerCase() === n.toLowerCase()) return n;
    return n + '（' + r + '）';
  }}
  function colorTint(hex, alpha) {{
    const m = /^#([0-9a-f]{{6}})$/i.exec(hex || '');
    if (!m) return 'var(--surface)';
    const n = parseInt(m[1], 16);
    return 'rgba(' + ((n >> 16) & 255) + ', ' + ((n >> 8) & 255) + ', ' + (n & 255) + ', ' + alpha + ')';
  }}
  function escapeHtml(value) {{
    return String(value || '').replace(/[&<>"']/g, ch => ({{
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }})[ch]);
  }}
  function scrollStream() {{
    if (streamEl) streamEl.scrollTop = streamEl.scrollHeight;
  }}
  /* ── Human interjections, rendered inline in the live stream ── */
  const pendingInterjections = [];
  const drawnInterjectionIds = new Set();
  function interjectClock(at) {{
    if (at === '') return '';
    const d = at ? new Date(at) : new Date();
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString('zh-CN', {{ hour12: false }});
  }}
  function agentDisplayName(agentId) {{
    if (!agentId) return '';
    if (rosterEl) {{
      const card = rosterEl.querySelector('[data-agent-id="' + agentId + '"] .r-name');
      if (card && card.textContent.trim()) return card.textContent.trim();
    }}
    return agentId;
  }}
  function interjectNode(text, opts) {{
    const state = opts.state === 'applied' ? 'applied' : 'pending';
    const agentName = opts.agentId ? agentDisplayName(opts.agentId) : '';
    const note = document.createElement('div');
    note.className = 'live-block live-note live-note-' + state;
    if (opts.clientId) note.dataset.interjectId = opts.clientId;
    note.dataset.interjectText = text;
    note.dataset.interjectAt = opts.at || '';
    note.dataset.interjectAgent = opts.agentId || '';
    if (state === 'applied') note.dataset.applied = '1';
    note.innerHTML =
      '<div class="live-note-head">' +
        '<span class="live-note-who">我</span>' +
        '<span class="live-note-title">人类插话</span>' +
        '<span class="live-note-tag ' + state + '">' +
          (state === 'applied'
            ? '已生效' + (agentName ? ' → ' + escapeHtml(agentName) : '')
            : '已发送 · 等待成员响应') +
        '</span>' +
        '<span class="live-note-time">' +
          escapeHtml(interjectClock(opts.at)) + '</span>' +
      '</div>' +
      '<div class="live-note-text">' + escapeHtml(text) + '</div>' +
      '<div class="live-note-meta">' +
        (state === 'applied'
          ? '当前成员正在结合这条插话重新回答；本条随后归入时间线。'
          : (opts.immediate
              ? '正在打断当前成员，请稍候…'
              : '当前没有正在回答的成员，这条要求会在下一次启动时生效。')) +
      '</div>';
    return note;
  }}
  function addInterjection(text, opts) {{
    opts = opts || {{}};
    const clean = String(text || '').trim();
    if (!streamEl || !clean) return null;
    if (opts.clientId) {{
      if (drawnInterjectionIds.has(opts.clientId)) return null;
      drawnInterjectionIds.add(opts.clientId);
    }}
    const note = interjectNode(clean, opts);
    if (placeholderEl) placeholderEl.style.display = 'none';
    streamEl.appendChild(note);
    streamEl.classList.add('has-blocks');
    if (opts.state !== 'applied') {{
      pendingInterjections.push({{
        text: clean, node: note, clientId: opts.clientId || ''
      }});
    }}
    scrollStream();
    return note;
  }}
  function applyInterjection(text, agentId, at) {{
    const clean = String(text || '').trim();
    if (!clean) return null;
    const idx = pendingInterjections.findIndex(item => item.text === clean);
    if (idx >= 0) {{
      const item = pendingInterjections.splice(idx, 1)[0];
      const replacement = interjectNode(clean, {{
        state: 'applied', agentId: agentId, at: at, clientId: item.clientId
      }});
      item.node.replaceWith(replacement);
      scrollStream();
      return replacement;
    }}
    return addInterjection(clean, {{
      state: 'applied', agentId: agentId, at: at
    }});
  }}
  function removeInterjection(node) {{
    if (!node) return;
    const idx = pendingInterjections.findIndex(item => item.node === node);
    if (idx >= 0) pendingInterjections.splice(idx, 1);
    if (node.dataset && node.dataset.interjectId) {{
      drawnInterjectionIds.delete(node.dataset.interjectId);
    }}
    node.remove();
  }}
  function addTimelineNote(text, agentId, at) {{
    const el = document.getElementById('liveTimeline');
    const clean = String(text || '').trim();
    if (!el || !clean) return;
    const empty = el.querySelector('.empty-state');
    if (empty) empty.remove();
    const agentName = agentId ? agentDisplayName(agentId) : '';
    const item = document.createElement('div');
    item.className = 'timeline-item';
    item.innerHTML =
      '<div><strong>人类插话</strong>' +
        (agentName ? ' · ' + escapeHtml(agentName) : '') + '</div>' +
      '<div class="timeline-note">' +
        '<div class="timeline-note-head">' +
          '<span class="tn-who">我</span>' +
          '<span class="tn-title">人类插话</span>' +
          '<span class="tn-time">' + escapeHtml(interjectClock(at)) + '</span>' +
        '</div>' +
        '<div class="tn-text">' + escapeHtml(clean) + '</div>' +
      '</div>';
    el.appendChild(item);
  }}
  /* Once the member has replied, its note leaves the live stream and is
     preserved in the timeline tab instead. */
  function settleInterjections(agentId) {{
    if (!streamEl) return;
    Array.from(streamEl.querySelectorAll('.live-note')).forEach(note => {{
      const applied = note.dataset.applied === '1';
      const noteAgent = note.dataset.interjectAgent || '';
      if (agentId) {{
        if (applied) {{
          if (noteAgent && noteAgent !== agentId) return;
        }} else if (noteAgent !== agentId) {{
          return;
        }}
      }}
      addTimelineNote(
        note.dataset.interjectText || '',
        noteAgent,
        note.dataset.interjectAt || ''
      );
      removeInterjection(note);
    }});
  }}
  /* Applied notes from earlier page loads live in the timeline already;
     only still-queued notes belong in the live stream. */
  INTERJECT_HISTORY.forEach(item => {{
    if (item.state === 'applied') return;
    addInterjection(item.text, {{
      state: 'queued',
      agentId: item.agent_id || '',
      at: item.at || '',
    }});
  }});
  window.addInterjection = addInterjection;
  window.removeInterjection = removeInterjection;
  window.settleInterjections = settleInterjections;
  /* ── Completion summary: status + generated file paths ── */
  function formatBytes(size) {{
    let value = Number(size) || 0;
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while (value >= 1024 && i < units.length - 1) {{ value /= 1024; i++; }}
    return (i === 0 ? Math.round(value) : value.toFixed(1)) + ' ' + units[i];
  }}
  function artifactHtml(files) {{
    if (!files || !files.length) {{
      return '<div style="margin-top:6px;color:var(--text-dim);">' +
        '未检测到本次任务期间修改的项目文件。</div>';
    }}
    let html = '<div style="margin-top:10px;font-weight:600;">生成 / 修改的文件（' +
      files.length + '）</div><div class="artifact-list" style="margin-top:6px;">';
    files.forEach(f => {{
      html += '<div class="artifact-row"><div class="artifact-main">' +
        '<code class="artifact-path" title="' + escapeHtml(f.path) + '">' +
        escapeHtml(f.path) + '</code>' +
        '<div class="artifact-desc">' + escapeHtml(f.description || '') +
        '</div></div>' +
        '<span class="artifact-size">' + formatBytes(f.size) + '</span></div>';
    }});
    return html + '</div>';
  }}
  async function appendTaskSummary(d, failed) {{
    if (placeholderEl) placeholderEl.style.display = 'none';
    let files = [];
    try {{
      const res = await fetch('/api/tasks/' + encodeURIComponent(taskId) + '/files');
      if (res.ok) {{
        const data = await res.json();
        files = data.files || [];
      }}
    }} catch (err) {{}}
    const block = document.createElement('div');
    block.className = 'live-block' + (failed ? ' live-error' : '');
    const head = failed
      ? '<div class="live-head" style="color:var(--red);">&#10007; 任务失败</div>'
      : '<div class="live-head" style="color:var(--green);">&#10003; 任务完成</div>';
    let body = '<div class="live-body" style="display:block;">';
    body += '<div>共 ' + (d.total_stages || 0) + ' 个阶段 · 用时 ' +
      formatElapsed(nowBar.startedAt ? ((nowBar.finishedAt || Date.now()) - nowBar.startedAt) : 0) + '</div>';
    if (d.error) {{
      body += '<div style="color:var(--red);margin-top:4px;">' +
        escapeHtml(d.error) + '</div>';
    }}
    body += artifactHtml(files);
    body += '</div>';
    block.innerHTML = head + body;
    streamEl.appendChild(block);
    streamEl.classList.add('has-blocks');
    scrollStream();
  }}
  function formatElapsed(ms) {{
    const secs = Math.max(0, Math.floor(ms / 1000));
    if (secs < 60) return secs + 's';
    const mins = Math.floor(secs / 60);
    const rest = secs % 60;
    if (mins < 60) return mins + 'm ' + String(rest).padStart(2, '0') + 's';
    const hours = Math.floor(mins / 60);
    return hours + 'h ' + String(mins % 60).padStart(2, '0') + 'm';
  }}
  function updateElapsed(state) {{
    if (!state || !state.timerEl) return;
    const end = state.finishedAt || Date.now();
    const text = formatElapsed(end - state.startedAt);
    state.timerEl.textContent = '⏱ ' + text;
    if (state.details && state.details.style.display !== 'none') {{
      state.summary.textContent = '思考过程（' + state.thinkingLen + ' 字 · ' + text + '）';
    }}
  }}
  /* ── Sticky "now" bar: mirrors the current member's state + timer ── */
  function updateNowTimer() {{
    if (!nowTimerEl) return;
    if (!nowBar.startedAt) {{ nowTimerEl.textContent = '⏱ 0s'; return; }}
    const end = nowBar.finishedAt || Date.now();
    nowTimerEl.textContent = '⏱ ' + formatElapsed(end - nowBar.startedAt);
  }}
  function setNowState(text) {{
    if (nowStateEl && text != null) nowStateEl.textContent = text;
  }}
  function runningStateText(text) {{
    // A cooperative pause lets the current reply finish. Output arriving in
    // that window is real, but the task is already paused — never claim the
    // member is "thinking" without qualification.
    return TASK_PAUSED ? '已暂停 · 当前成员收尾中…' : text;
  }}
  function startNow(agentText, text, startedAt, color) {{
    if (nowAgentEl && agentText) nowAgentEl.textContent = agentText;
    setNowState(text || '思考中…');
    nowBar.startedAt = startedAt || Date.now();
    nowBar.finishedAt = null;
    if (nowDotEl && color) nowDotEl.style.background = color;
    if (nowEl) nowEl.classList.remove('done', 'failed');
    if (nowEl) nowEl.classList.add('running');
    updateNowTimer();
  }}
  function finishNow(text, cls) {{
    if (nowBar.startedAt && !nowBar.finishedAt) nowBar.finishedAt = Date.now();
    setNowState(text);
    if (nowEl) {{
      nowEl.classList.remove('running');
      if (cls) nowEl.classList.add(cls);
    }}
    updateNowTimer();
  }}
  /* ── Member roster: resident session states ── */
  const ROSTER_LABELS = {{
    waiting: '待启动', starting: '启动中…', ready: '常驻中',
    running: '运行中…', paused: '已暂停', failed: '启动失败', cancelled: '已取消'
  }};
  function rosterCard(agentId) {{
    if (!rosterEl) return null;
    let found = null;
    const wanted = String(agentId || '');
    rosterEl.querySelectorAll('[data-agent-id]').forEach(el => {{
      if (!found && el.getAttribute('data-agent-id') === wanted) found = el;
    }});
    return found;
  }}
  function setRosterState(agentId, state, label) {{
    const card = rosterCard(agentId);
    if (!card) return;
    // A paused task has no running member. Resident-status polling and late
    // stage events may still report "running"; never show that contradiction.
    if (TASK_PAUSED && state === 'running') {{
      state = 'paused';
      label = label || ROSTER_LABELS.paused;
    }}
    card.setAttribute('data-state', state);
    const lbl = card.querySelector('.r-label');
    if (lbl) lbl.textContent = label || ROSTER_LABELS[state] || state;
  }}
  function failStartingRoster() {{
    if (!rosterEl) return;
    rosterEl.querySelectorAll('[data-state="starting"]').forEach(card => {{
      setRosterState(card.getAttribute('data-agent-id'), 'failed');
    }});
  }}
  function applyPausedRoster() {{
    // The current member must not be shown as "running" or "resident" while
    // the task is paused at a boundary.
    if (TASK_PAUSED && LAST_LIVE_AGENT) {{
      setRosterState(LAST_LIVE_AGENT, 'paused');
    }}
  }}
  async function loadResidentStatus() {{
    if (!rosterEl) return;
    try {{
      const res = await fetch('/api/tasks/' + encodeURIComponent(taskId) + '/residents');
      if (!res.ok) return;
      const data = await res.json();
      const statuses = data.statuses || {{}};
      Object.keys(statuses).forEach(aid => setRosterState(aid, statuses[aid]));
      applyPausedRoster();
    }} catch (err) {{}}
  }}
  loadResidentStatus();
  setInterval(() => {{
    activeBlocks.forEach(state => updateElapsed(state));
    updateNowTimer();
  }}, 1000);
  function markPreviousDone() {{
    if (currentBlock && !currentBlock.finishedAt) {{
      currentBlock.finishedAt = Date.now();
      updateElapsed(currentBlock);
      const duration = formatElapsed(currentBlock.finishedAt - currentBlock.startedAt);
      currentBlock.stateEl.textContent = '已完成 · ' + currentBlock.outputLen +
        ' 字 · ' + duration;
      activeBlocks.delete(currentBlock);
      if (currentBlock.timerEl) currentBlock.timerEl.style.display = 'none';
      finishNow('已完成', 'done');
    }}
  }}
  function createBlock(d, startedAt) {{
    if (placeholderEl) placeholderEl.style.display = 'none';
    const color = agentColor(d.agent_id, d.agent_name);
    const block = document.createElement('div');
    block.className = 'live-block';
    block.style.borderLeftColor = color;
    block.style.background = colorTint(color, 0.07);

    const head = document.createElement('div');
    head.className = 'live-head';
    head.style.color = color;
    const nameEl = document.createElement('span');
    nameEl.textContent = agentLabel(d.agent_name || d.agent_id, d.runtime);
    const tagEl = document.createElement('span');
    tagEl.className = 'live-tag';
    tagEl.textContent = '阶段 ' + d.stage_number;
    const stateEl = document.createElement('span');
    stateEl.className = 'live-state';
    const thinkingLabel = TASK_PAUSED ? '已暂停 · 收尾中…' : '思考中…';
    stateEl.textContent = thinkingLabel;
    const timerEl = document.createElement('span');
    timerEl.className = 'live-timer';
    timerEl.textContent = '⏱ 0s';
    head.appendChild(nameEl);
    head.appendChild(tagEl);
    head.appendChild(stateEl);
    head.appendChild(timerEl);

    const actions = document.createElement('details');
    actions.className = 'live-think live-actions';
    actions.style.display = 'none';
    const actionsSummary = document.createElement('summary');
    actionsSummary.textContent = '操作记录（0 条）';
    const actionsPre = document.createElement('pre');
    actions.appendChild(actionsSummary);
    actions.appendChild(actionsPre);

    const details = document.createElement('details');
    details.className = 'live-think';
    details.style.display = 'none';
    const summary = document.createElement('summary');
    summary.textContent = '思考过程（0 字）';
    const pre = document.createElement('pre');
    details.appendChild(summary);
    details.appendChild(pre);

    const body = document.createElement('span');
    body.className = 'live-body';

    block.appendChild(head);
    block.appendChild(actions);
    block.appendChild(details);
    block.appendChild(body);
    streamEl.appendChild(block);
    streamEl.classList.add('has-blocks');

    currentBlock = {{
      el: block, nameEl: nameEl, tagEl: tagEl, stateEl: stateEl,
      timerEl: timerEl, startedAt: startedAt || Date.now(), finishedAt: null,
      actions: actions, actionsSummary: actionsSummary, actionsPre: actionsPre,
      actionCount: 0, details: details, summary: summary, pre: pre,
      body: body, thinkingLen: 0, outputLen: 0, meta: d
    }};
    activeBlocks.add(currentBlock);
    updateElapsed(currentBlock);
    startNow(
      agentLabel(d.agent_name || d.agent_id, d.runtime),
      thinkingLabel,
      currentBlock.startedAt,
      color
    );
    scrollStream();
    return currentBlock;
  }}
  function appendThinking(chunk) {{
    if (!currentBlock || !chunk) return;
    currentBlock.thinkingLen += chunk.length;
    if (currentBlock.details.style.display === 'none') {{
      currentBlock.details.style.display = '';
      currentBlock.details.open = true;
    }}
    currentBlock.pre.textContent += chunk;
    updateElapsed(currentBlock);
    setNowState(runningStateText('思考中…'));
    scrollStream();
  }}
  function appendProcess(chunk) {{
    if (!currentBlock || !chunk) return;
    const narrations = String(chunk).split(/\\n{{2,}}/).map(s => s.trim()).filter(Boolean);
    narrations.forEach(narration => {{
      if (currentBlock.pre.textContent) currentBlock.pre.textContent += '\\n\\n';
      currentBlock.pre.textContent += narration;
      const bodyText = currentBlock.body.textContent.replace(/\\s+$/, '');
      if (bodyText.endsWith(narration)) {{
        currentBlock.body.textContent = bodyText.slice(0, bodyText.length - narration.length);
      }}
    }});
    if (narrations.length) {{
      currentBlock.thinkingLen = currentBlock.pre.textContent.length;
      currentBlock.details.style.display = '';
      currentBlock.details.open = true;
      updateElapsed(currentBlock);
      setNowState(runningStateText('思考中…'));
    }}
    scrollStream();
  }}
  function appendAction(chunk) {{
    if (!currentBlock || !chunk) return;
    const text = String(chunk);
    currentBlock.actionCount += text.split('\\n').filter(line => line.trim()).length;
    if (currentBlock.actions.style.display === 'none') {{
      currentBlock.actions.style.display = '';
      currentBlock.actions.open = true;
    }}
    currentBlock.actionsPre.textContent += text + (text.endsWith('\\n') ? '' : '\\n');
    currentBlock.actionsSummary.textContent = '操作记录（' + currentBlock.actionCount + ' 条）';
    setNowState(runningStateText('调用工具…'));
    scrollStream();
  }}
  function appendOutput(chunk) {{
    if (!currentBlock || !chunk) return;
    if (currentBlock.stateEl && (currentBlock.stateEl.textContent === '思考中…'
        || currentBlock.stateEl.textContent === '已暂停 · 收尾中…')) {{
      currentBlock.stateEl.textContent = TASK_PAUSED ? '已暂停 · 收尾中…' : '生成中…';
    }}
    setNowState(runningStateText('生成中…'));
    currentBlock.outputLen += chunk.length;
    currentBlock.body.textContent += chunk;
    scrollStream();
  }}
  function finalizeBlock(d, extra) {{
    if (!currentBlock) return;
    const state = currentBlock;
    if (!state.outputLen && d && d.output_preview) {{
      state.body.textContent = d.output_preview;
      state.outputLen = d.output_preview.length;
    }}
    if (!state.finishedAt) {{
      state.finishedAt = Date.now();
      activeBlocks.delete(state);
    }}
    updateElapsed(state);
    const duration = formatElapsed(state.finishedAt - state.startedAt);
    state.stateEl.textContent = (extra || ('已完成 · ' + state.outputLen + ' 字')) +
      ' · ' + duration;
    if (state.timerEl) state.timerEl.style.display = 'none';
    finishNow(extra || ('已完成 · ' + state.outputLen + ' 字'), 'done');
    scrollStream();
  }}

  es.addEventListener('resident_starting', (e) => {{
    statusEl.textContent = '正在并行启动流水线成员的常驻会话...';
    setNowState('启动成员会话…');
    let d = {{}};
    try {{ d = JSON.parse(e.data); }} catch (err) {{}}
    (d.agents || []).forEach(aid => setRosterState(aid, 'starting'));
  }});
  es.addEventListener('resident_ready', (e) => {{
    const d = JSON.parse(e.data);
    statusEl.textContent = (d.name || d.agent_id) + ' 已就绪并常驻';
    setRosterState(d.agent_id, 'ready');
  }});
  es.addEventListener('resident_failed', (e) => {{
    const d = JSON.parse(e.data);
    statusEl.textContent = '成员会话启动失败: ' + (d.error || '');
    setNowState('成员会话启动失败');
    failStartingRoster();
  }});

  /* First frame on every (re)connect: rebuild the current view. */
  es.addEventListener('snapshot', (e) => {{
    let d = {{}};
    try {{ d = JSON.parse(e.data); }} catch (err) {{ return; }}
    const st = String(d.status || '');
    if (st === 'completed' || st === 'failed' || st === 'cancelled') {{
      es.close();
      if (titleEl) {{
        titleEl.innerHTML = st === 'completed'
          ? '<span style="color:var(--green);">&#10003;</span> 已完成'
          : (st === 'failed'
              ? '<span style="color:var(--red);">&#10007;</span> 失败'
              : '已停止');
      }}
      return;
    }}
    TASK_PAUSED = !!d.paused;
    renderTaskControl();
    LAST_LIVE_AGENT = d.agent_id || LAST_LIVE_AGENT;
    const startedMs = d.started_at ? Date.parse(d.started_at) : NaN;
    const display = d.agent_name || d.agent_id || '';
    if (st === 'in_progress' && d.paused) {{
      // Paused at a stage boundary: nothing is "thinking". Never rebuild a
      // running block here; the member is blocked until the user resumes.
      if (d.agent_id) setRosterState(d.agent_id, 'paused');
      applyPausedRoster();
      if (currentBlock && !currentBlock.finishedAt) {{
        currentBlock.stateEl.textContent = '已暂停 · 收尾中…';
        currentBlock.finishedAt = Date.now();
        updateElapsed(currentBlock);
      }}
      titleEl.innerHTML = '<span class="live-dot" style="background:var(--orange);"></span>流水线已暂停';
      statusEl.innerHTML = '阶段 ' + (d.stage_number || 1) + ': <strong>' +
        escapeHtml(display) + '</strong> — 已暂停，等待继续';
      if (nowAgentEl && display) {{
        nowAgentEl.textContent = agentLabel(display, d.runtime);
      }}
      setNowState('已暂停，等待继续');
      nowBar.startedAt = null;
      nowBar.finishedAt = null;
      if (nowTimerEl) nowTimerEl.textContent = '';
      if (nowDotEl) nowDotEl.style.background = 'var(--orange)';
      if (d.chunks_total) {{
        const cp = document.getElementById('chunkProgressText');
        if (cp) cp.textContent = '已完成 ' + d.chunks_done + '/' + d.chunks_total;
      }}
    }} else if (st === 'in_progress' && d.stage_running && d.agent_id) {{
      setRosterState(d.agent_id, 'running');
      if (!currentBlock) {{
        createBlock({{
          agent_id: d.agent_id,
          agent_name: display,
          runtime: d.runtime || '',
          stage_number: d.stage_number || 1
        }}, isNaN(startedMs) ? Date.now() : startedMs);
      }} else if (!isNaN(startedMs)) {{
        currentBlock.startedAt = startedMs;
        updateElapsed(currentBlock);
        nowBar.startedAt = startedMs;
        updateNowTimer();
      }}
      titleEl.innerHTML = '<span class="live-dot"></span>流水线运行中';
      statusEl.innerHTML = '<span class="live-dot"></span>阶段 ' +
        (d.stage_number || 1) + ': <strong>' + escapeHtml(display) + '</strong>';
      setNowState('思考中…');
      if (d.chunks_total) {{
        const cp = document.getElementById('chunkProgressText');
        if (cp) cp.textContent = '已完成 ' + d.chunks_done + '/' + d.chunks_total;
      }}
    }} else if (st === 'in_progress') {{
      if (isNaN(startedMs) === false && !nowBar.startedAt) {{
        nowBar.startedAt = startedMs;
        updateNowTimer();
      }}
      setNowState('等待代理输出…');
    }}
  }});

  es.addEventListener('stage_start', (e) => {{
    const d = JSON.parse(e.data);
    // A snapshot may have already rebuilt this in-flight stage after a reload;
    // do not create a duplicate block for the same turn.
    const sameTurn = currentBlock && currentBlock.meta &&
      currentBlock.meta.stage_number === d.stage_number &&
      currentBlock.meta.agent_id === d.agent_id &&
      !currentBlock.finishedAt;
    if (!sameTurn) {{
      markPreviousDone();
      createBlock(d);
    }}
    setRosterState(d.agent_id, 'running');
    titleEl.innerHTML = TASK_PAUSED
      ? '<span class="live-dot" style="background:var(--orange);"></span>流水线已暂停'
      : '<span class="live-dot"></span>流水线运行中';
    statusEl.innerHTML = '<span class="live-dot"></span>阶段 ' + d.stage_number +
      ': <strong>' + escapeHtml(d.agent_name || d.agent_id) + '</strong>';
    setNowState(runningStateText('思考中…'));
  }});
  es.addEventListener('stage_thinking', (e) => {{
    const d = JSON.parse(e.data);
    appendThinking(d.text);
  }});
  es.addEventListener('tool_process', (e) => {{
    const d = JSON.parse(e.data);
    appendProcess(d.text);
  }});
  es.addEventListener('tool_action', (e) => {{
    const d = JSON.parse(e.data);
    appendAction(d.text);
  }});
  es.addEventListener('stage_output', (e) => {{
    const d = JSON.parse(e.data);
    appendOutput(d.text);
  }});
  es.addEventListener('tool_call', (e) => {{
    const d = JSON.parse(e.data);
    appendAction('· ' + (d.tool || 'unknown') + ' — ' +
      (d.title || '') + ' (' + (d.status || '?') + ')');
  }});
  es.addEventListener('stage_complete', (e) => {{
    const d = JSON.parse(e.data);
    setRosterState(d.agent_id, TASK_PAUSED ? 'paused' : 'ready');
    let extra = '已完成 · ' + (currentBlock ? currentBlock.outputLen : 0) + ' 字';
    if (d.gate_result) extra += ' · 审批: ' + d.gate_result;
    finalizeBlock(d, extra);
    settleInterjections(d.agent_id);
    statusEl.innerHTML = '阶段 ' + d.stage_number + ': <strong>' +
      escapeHtml(d.agent_id) + '</strong> — ' + escapeHtml(d.gate_result || '完成');
    if (TASK_PAUSED) setNowState('已暂停，等待继续');
  }});
  es.addEventListener('turn_complete', (e) => {{
    const d = JSON.parse(e.data);
    setRosterState(d.agent_id, TASK_PAUSED ? 'paused' : 'ready');
    finalizeBlock(d, '第' + d.round + '轮 · 已完成 · ' +
      (currentBlock ? currentBlock.outputLen : 0) + ' 字');
    settleInterjections(d.agent_id);
    if (TASK_PAUSED) setNowState('已暂停，等待继续');
  }});
  es.addEventListener('interjection_queued', (e) => {{
    const d = JSON.parse(e.data);
    addInterjection(d.text, {{
      state: 'queued', clientId: d.client_id || '', at: d.at || undefined,
      immediate: d.immediate
    }});
    const hint = document.getElementById('interjectHint');
    if (hint) hint.textContent = d.immediate
      ? '已发送，正在打断当前成员…'
      : '已发送，下一次启动时生效';
  }});
  es.addEventListener('interjection_applied', (e) => {{
    const d = JSON.parse(e.data);
    const label = agentDisplayName(d.agent_id) || d.agent_id || '';
    const notes = Array.isArray(d.notes)
      ? d.notes
      : (d.notes ? [String(d.notes)] : []);
    notes.forEach(text => applyInterjection(text, d.agent_id, d.at || undefined));
    const hint = document.getElementById('interjectHint');
    if (hint) hint.textContent = '已生效 → ' + (label || '当前成员');
    showToast('插话已生效 · ' + (label || '当前成员'));
  }});
  es.addEventListener('stage_interrupted', (e) => {{
    const d = JSON.parse(e.data);
    finalizeBlock(d, '被人类插话打断');
    createBlock({{
      agent_id: d.agent_id,
      agent_name: d.agent_name || d.agent_id,
      runtime: d.runtime || '',
      stage_number: d.stage_number
    }});
    statusEl.innerHTML = '<span class="live-dot"></span>阶段 ' + d.stage_number +
      ': <strong>' + escapeHtml(d.agent_name || d.agent_id) +
      '</strong> — 已收到插话，重新回答';
    setNowState('已收到插话，重新回答…');
  }});
  es.addEventListener('chunk_split_started', (e) => {{
    const d = JSON.parse(e.data);
    setChunkSplitState('正在按需求文档拆分工作块…（' + (d.source || '') + '）');
  }});
  es.addEventListener('chunk_split_done', (e) => {{
    const d = JSON.parse(e.data);
    setChunkSplitState('');
    if (d.progress && typeof chunkProgressHtml === 'function') chunkProgressHtml(d.progress);
    if (typeof loadChunks === 'function') loadChunks();
  }});
  es.addEventListener('chunk_split_failed', (e) => {{
    const d = JSON.parse(e.data);
    setChunkSplitState('拆分失败：' + (d.error || '未知错误'), true);
  }});
  es.addEventListener('chunk_warning', (e) => {{
    const d = JSON.parse(e.data);
    const ids = (d.remaining || []).join(', ');
    if (typeof showToast === 'function') showToast('仍有未完成工作块：' + ids);
  }});
  es.addEventListener('chunk_update', () => {{
    if (typeof loadChunks === 'function') loadChunks();
  }});
  es.addEventListener('pipeline_resuming', (e) => {{
    titleEl.innerHTML = '<span class="live-dot"></span>恢复中...';
    statusEl.innerHTML = '加载下一个代理...';
    finishNow('加载下一个代理…');
  }});
  es.addEventListener('human_input_waiting', () => {{
    es.close();
    location.hash = 'output';
    setTimeout(() => location.reload(), 800);
  }});
  es.addEventListener('gate_waiting', () => {{
    es.close();
    location.hash = 'output';
    setTimeout(() => location.reload(), 800);
  }});
  es.addEventListener('task_paused', () => {{
    TASK_PAUSED = true;
    renderTaskControl();
    if (titleEl) {{
      titleEl.innerHTML = '<span class="live-dot" style="background:var(--orange);"></span>流水线已暂停';
    }}
    const pausedAgent = (currentBlock && currentBlock.meta && currentBlock.meta.agent_id)
      || LAST_LIVE_AGENT;
    if (pausedAgent) setRosterState(pausedAgent, 'paused');
    applyPausedRoster();
    if (currentBlock && !currentBlock.finishedAt) {{
      currentBlock.stateEl.textContent = '已暂停 · 收尾中…';
      setNowState('已暂停 · 当前成员收尾中…');
    }} else {{
      setNowState('已暂停，等待继续');
      nowBar.startedAt = null;
      nowBar.finishedAt = null;
      if (nowTimerEl) nowTimerEl.textContent = '';
    }}
    if (nowDotEl) nowDotEl.style.background = 'var(--orange)';
    showToast('任务已暂停（当前成员完成后不再进入下一阶段）');
  }});
  es.addEventListener('task_resumed', () => {{
    TASK_PAUSED = false;
    renderTaskControl();
    if (titleEl) titleEl.innerHTML = '<span class="live-dot"></span>流水线运行中';
    const resumedAgent = (currentBlock && currentBlock.meta && currentBlock.meta.agent_id)
      || LAST_LIVE_AGENT;
    if (resumedAgent) setRosterState(resumedAgent, 'running');
    if (currentBlock && !currentBlock.finishedAt) {{
      currentBlock.stateEl.textContent = '思考中…';
    }}
    setNowState('继续执行…');
    showToast('任务已继续');
  }});
  es.addEventListener('task_complete', (e) => {{
    const d = JSON.parse(e.data);
    es.close();
    markPreviousDone();
    settleInterjections('');
    finishNow('全部完成', 'done');
    showToast('流水线已完成');
    titleEl.innerHTML = '<span style="color:var(--green);">&#10003;</span> 已完成';
    document.getElementById('cancelBtn').style.display = 'none';
    appendTaskSummary(d, false);
  }});
  es.addEventListener('task_cancelled', () => {{ es.close(); setTimeout(() => location.reload(), 800); }});
  es.addEventListener('task_failed', (e) => {{
    es.close();
    markPreviousDone();
    settleInterjections('');
    const d = JSON.parse(e.data);
    finishNow('失败', 'failed');
    showToast('流水线失败: ' + (d.error || ''), 'error');
    titleEl.innerHTML = '<span style="color:var(--red);">&#10007;</span> 失败';
    document.getElementById('cancelBtn').style.display = 'none';
    appendTaskSummary(d, true);
  }});
  es.onerror = () => {{
    // EventSource reconnects on its own; the next connection replays a
    // snapshot, so do NOT close here or the page needs a manual refresh.
    if (statusEl) statusEl.textContent = '连接中断，正在重连…';
  }};
}})();
</script>"""

    return layout(
        f"任务 {short_id}",
        f"{body}\n{js}\n{sse_js}",
        active="tasks",
        breadcrumbs=[("看板", "/"), (f"任务 {short_id}", None)],
    )
