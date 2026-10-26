"""Dashboard page — pure overview with task stats, meeting stats, and recent activity."""

from __future__ import annotations

from aqm.core.task import Task, TaskStatus
from aqm.meeting import Meeting
from aqm.web.templates import badge, esc, fmt_time, layout


def render_dashboard(
    tasks: list[Task],
    meetings: list[Meeting] | None = None,
) -> str:
    # Task stats
    total = len(tasks)
    completed = sum(1 for t in tasks if t.status == TaskStatus.completed)
    failed = sum(1 for t in tasks if t.status == TaskStatus.failed)
    awaiting = sum(1 for t in tasks if t.status == TaskStatus.awaiting_gate)
    running = sum(1 for t in tasks if t.status == TaskStatus.in_progress)

    # Meeting stats
    meetings = meetings or []
    m_total = len(meetings)
    m_active = sum(1 for m in meetings if m.status == "running")
    m_completed = sum(1 for m in meetings if m.status == "completed")

    task_stats = f"""\
<div class="stats" id="statsCards">
  <div class="stat-card"><div class="value" id="statTotal">{total}</div><div class="label">任务总数</div></div>
  <div class="stat-card blue"><div class="value" id="statRunning">{running}</div><div class="label">运行中</div></div>
  <div class="stat-card green"><div class="value" id="statCompleted">{completed}</div><div class="label">已完成</div></div>
  <div class="stat-card red"><div class="value" id="statFailed">{failed}</div><div class="label">失败</div></div>
  <div class="stat-card orange"><div class="value" id="statAwaiting">{awaiting}</div><div class="label">等待审批</div></div>
</div>"""

    meeting_stats = f"""\
<div class="stats" style="margin-top:-12px;">
  <div class="stat-card"><div class="value">{m_total}</div><div class="label">会议总数</div></div>
  <div class="stat-card blue"><div class="value">{m_active}</div><div class="label">进行中</div></div>
  <div class="stat-card green"><div class="value">{m_completed}</div><div class="label">已完成</div></div>
</div>"""

    # Recent tasks (top 10)
    recent_tasks = tasks[:10]
    if not recent_tasks:
        recent_tasks_html = """\
<div class="empty-state">
  <div class="icon">📋</div>
  <div class="title">暂无任务</div>
  <div class="desc"><a href="/tasks">运行一条流水线来创建第一个任务。</a></div>
</div>"""
    else:
        rows = []
        for t in recent_tasks:
            desc = esc(t.description[:80])
            if len(t.description) > 80:
                desc += "..."
            agent = esc(t.current_agent_id or "-")
            p_name = t.priority.name
            rows.append(
                f'<tr>'
                f'<td><a href="/tasks/{esc(t.id)}">{esc(t.id)}</a></td>'
                f'<td>{badge(t.status.value)}</td>'
                f'<td style="font-size:12px;color:var(--text-dim);">{p_name}</td>'
                f'<td>{agent}</td>'
                f'<td title="{esc(t.description)}" style="max-width:300px;overflow:hidden;text-overflow:ellipsis;">{desc}</td>'
                f'<td style="font-size:12px;">{fmt_time(t.created_at)}</td>'
                f'</tr>'
            )
        recent_tasks_html = f"""\
<div class="card">
  <h3>最近任务 <a href="/tasks" style="font-size:12px;font-weight:normal;margin-left:8px;">查看全部 →</a></h3>
  <div class="table-wrap">
  <table>
  <thead><tr>
    <th>标识</th><th>状态</th><th>优先级</th><th>代理</th><th>描述</th><th>创建时间</th>
  </tr></thead>
  <tbody>{''.join(rows)}</tbody>
  </table>
  </div>
</div>"""

    # Recent meetings (top 5)
    recent_meetings = meetings[:5]
    if not recent_meetings:
        recent_meetings_html = """\
<div class="card">
  <h3>最近会议 <a href="/meeting" style="font-size:12px;font-weight:normal;margin-left:8px;">查看全部 →</a></h3>
  <p style="color:var(--text-dim);font-size:13px;">暂无会议记录。</p>
</div>"""
    else:
        m_rows = []
        for m in recent_meetings:
            m_status = {
                "idle": "待开始", "running": "进行中", "completed": "已完成",
                "stopped": "已停止", "error": "失败"
            }.get(m.status, m.status)
            m_badge_color = {
                "idle": "badge-pending", "running": "badge-in_progress",
                "completed": "badge-completed", "stopped": "badge-cancelled", "error": "badge-failed"
            }.get(m.status, "badge-pending")
            m_agents = ", ".join(a.name for a in m.agents)
            m_rows.append(
                f'<tr>'
                f'<td><a href="/meeting">{esc(m.topic)}</a></td>'
                f'<td><span class="badge {m_badge_color}">{m_status}</span></td>'
                f'<td style="font-size:12px;">{esc(m_agents)}</td>'
                f'<td>{m.current_round}/{m.rounds}</td>'
                f'<td style="font-size:12px;">{fmt_time(m.created_at)}</td>'
                f'</tr>'
            )
        recent_meetings_html = f"""\
<div class="card">
  <h3>最近会议 <a href="/meeting" style="font-size:12px;font-weight:normal;margin-left:8px;">查看全部 →</a></h3>
  <div class="table-wrap">
  <table>
  <thead><tr>
    <th>主题</th><th>状态</th><th>参会</th><th>轮次</th><th>创建时间</th>
  </tr></thead>
  <tbody>{''.join(m_rows)}</tbody>
  </table>
  </div>
</div>"""

    # Quick action cards
    quick_actions = """\
<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:24px;">
  <div class="card" style="margin:0;display:flex;align-items:center;gap:16px;cursor:pointer;" onclick="location.href='/tasks'">
    <div style="font-size:32px;">▶</div>
    <div>
      <div style="font-weight:600;font-size:15px;">运行流水线</div>
      <div style="font-size:12px;color:var(--text-dim);margin-top:2px;">创建新的流水线任务</div>
    </div>
  </div>
  <div class="card" style="margin:0;display:flex;align-items:center;gap:16px;cursor:pointer;" onclick="location.href='/meeting'">
    <div style="font-size:32px;">👥</div>
    <div>
      <div style="font-weight:600;font-size:15px;">创建会议</div>
      <div style="font-size:12px;color:var(--text-dim);margin-top:2px;">多代理协作讨论</div>
    </div>
  </div>
</div>"""

    js = """\
<script>
/* ── SSE real-time counters ────────────────── */
try {
  const es = new EventSource('/api/events');
  es.addEventListener('task_count_update', (e) => {
    const d = JSON.parse(e.data);
    if (d.total !== undefined) document.getElementById('statTotal')?.replaceChildren(d.total);
    if (d.running !== undefined) document.getElementById('statRunning')?.replaceChildren(d.running);
    if (d.completed !== undefined) document.getElementById('statCompleted')?.replaceChildren(d.completed);
    if (d.failed !== undefined) document.getElementById('statFailed')?.replaceChildren(d.failed);
    if (d.awaiting !== undefined) document.getElementById('statAwaiting')?.replaceChildren(d.awaiting);
  });
  es.onerror = () => {};
} catch(e) {}
</script>"""

    return layout(
        "概览",
        f"{task_stats}\n{meeting_stats}\n{quick_actions}\n{recent_tasks_html}\n{recent_meetings_html}\n{js}",
        active="dashboard",
    )
