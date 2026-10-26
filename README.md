# aqm

**用 YAML 构建 AI Agent 团队。无需代码。无需 API Key。纯流水线。**

一个编排框架，让多个 AI Agent 通过**显式队列**传递任务——或者在**实时会话中讨论**直到达成共识。定义一次，随处运行，共享给任何人。

```
  [用户] ──输入──► [规划器] ──► [审查器] ──批准──► [设计会话] ──► [实施者]
                        ▲              │                    ┌──┬──┬──┐
                        └── 拒绝 ──────┘                    ▼  ▼  ▼  ▼  轮询
                        └── 询问用户 ──►[用户]              [架构][安全][前端]  直到共识
```

## 为什么选择 aqm？

单个 AI Agent 用**相同的偏见**写代码并审查。它无法发现自己的盲点。

aqm 给你一个**团队**——每个 Agent 有专用角色、独立提示词，可选不同 LLM。质量门自动拒绝低质量输出。会话让 Agent 在决定前辩论。

```yaml
# 一个 YAML 文件就是整个流水线
agents:
  - id: developer
    runtime: claude
    system_prompt: "实现: {{ input }}"
    handoffs: [{ to: reviewer }]

  - id: reviewer
    runtime: gemini                    # 不同 LLM 发现不同 bug
    system_prompt: "审查安全性: {{ input }}"
    gate:
      type: llm
      prompt: "这是生产就绪的吗？"
      max_retries: 3                   # 自动拒绝 → 最多重试 3 次
    handoffs:
      - { to: deployer, condition: on_approve }
      - { to: developer, condition: on_reject }

  - id: deployer
    runtime: claude
    context_strategy: none             # 节省 85% token —— 不需要上下文
    system_prompt: "部署: {{ input }}"
```

```bash
pip install aqm && aqm init && aqm run "添加 JWT 认证"
```

### 什么让 aqm 与众不同

| 问题 | 单 Agent | aqm |
|---|---|---|
| 同一 LLM 审查自己的代码 | 一种偏见，一个视角 | **跨 LLM 验证**（Claude 写，Gemini 审） |
| 没有强制质量检查 | Agent 自我说"看起来不错" | **质量门**自动拒绝并重试 |
| 上下文窗口爆炸 | 所有内容在一个对话里 | **5 种上下文策略** —— 节省 55-85% token |
| 无法标准化团队流程 | 每次运行都是临时的 | **YAML 流水线** —— 版本控制，可共享 |
| 复杂任务丢失进度 | 没有内置任务跟踪 | **分块分解** —— Agent 将工作分解为可跟踪单元 |
| API 成本高 | 按 token 计费累加 | **基于 CLI** —— 使用现有 CLI 订阅，无额外 API 费用 |
| 设置开销 | API Key，SDK，环境配置 | **零配置** —— 使用你已有的 CLI 工具 |

## 安装

### 前置要求

| 依赖 | 版本 | 用途 |
|---|---|---|
| Python | 3.11+ | 运行 aqm 框架 |
| Node.js | 18+ | 安装 LLM CLI runtime |
| npm | 随 Node.js | 包管理器 |

### 步骤 1：创建项目目录和虚拟环境

**Windows (PowerShell)**
```powershell
mkdir aqm
cd aqm
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Windows (CMD)**
```cmd
mkdir aqm
cd aqm
python -m venv .venv
.\.venv\Scripts\activate.bat
```

**macOS / Linux**
```bash
mkdir aqm
cd aqm
python3 -m venv .venv
source .venv/bin/activate
```

> 激活后命令行前面会出现 `(.venv)` 提示符。

### 步骤 2：安装 aqm

**从 PyPI 安装（推荐）**
```bash
pip install aqm
```

**从源码安装（开发用）**
```bash
git clone https://github.com/aqm-framework/aqm.git
cd aqm
pip install -e ".[dev]"
```

### 步骤 3：安装 LLM CLI Runtime

aqm 通过子进程调用 CLI 工具，无需 API Key 或 SDK 设置。选择至少一个 runtime 安装：

| Runtime | 提供商 | 安装命令 |
|---|---|---|
| `claude` | Anthropic | `npm i -g @anthropic-ai/claude-code && claude login` |
| `gemini` | Google | `npm i -g @google/gemini-cli` |
| `codex` | OpenAI | `npm i -g @openai/codex` |
| `opencode` | OpenCode | `npm i -g opencode-ai` |
| `kilo` | Kilocode | `npm i -g @kilocode/cli` |
| `cline` | Cline | `npm i -g @cline/cli` |

> 你只需为已有的 CLI 订阅付费，而不是按 token 的 API 费用。

### 步骤 4：验证安装

```bash
aqm --version          # 应输出 1.3.x
aqm runtime list       # 显示可用的 runtime
```

## 快速开始

```bash
cd my-project
aqm init                              # 交互式设置向导
aqm run "添加 JWT 认证"                # 运行流水线
aqm serve                              # Web 仪表盘在 localhost:8000
```

## 实际案例

### 案例 1：aqm 用 aqm 构建

aqm 自身的开发使用 aqm。每个功能请求都经过影响分析、实现、测试（使用真实 YAML 文件）、文档更新和代码审查——全自动。

```
[用户请求]
     │
     ▼
[影响分析器] ── 哪些文件受影响？破坏性变更？
     │
     ▼
[实施者] ── 创建功能分支，写代码 (MCP: github)
     │
     ▼
[测试器] ── pytest + 在 /tmp/ 中真实 YAML 文件 + aqm validate
     │ gate: llm
     ├─ 批准 ──► [文档更新器] ── git diff → 只更新变更部分
     └─ 拒绝  ──► [实施者] ── 修复并重试
                    │
               [代码审查器] ── 合并前质量门 (runtime: gemini)
                    │ gate: llm
                    ├─ 批准 ──► [分支管理器] ── 提交 + 合并到 main
                    └─ 拒绝  ──► [修复器] ──► [代码审查器]
```

```yaml
# .aqm/pipelines/dev.yaml (简化版)
agents:
  - id: impact_analyzer
    runtime: claude
    context_strategy: none             # 新鲜分析不需要先前上下文
    system_prompt: "分析影响: {{ input }}"
    handoffs: [{ to: implementer }]

  - id: implementer
    runtime: claude
    context_strategy: last_only        # 只需要影响报告
    mcp: [{ server: github }]
    system_prompt: "在功能分支上实现。不要提交。"
    handoffs: [{ to: tester }]

  - id: tester
    runtime: claude
    context_strategy: last_only
    system_prompt: |
      运行 pytest。创建 /tmp/aqm_test_<feature>/ 和真实 YAML 文件。
      对每个运行 aqm validate。报告 pytest 退出码 + 通过 | 失败。
    gate:
      type: llm
      prompt: |
        仅当状态=通过，退出码=0，且零回归失败时批准。
        如果存在任何失败则拒绝 —— 部分通过算作失败。
      max_retries: 2
    handoffs:
      - { to: doc_updater, condition: on_approve }
      - { to: implementer, condition: on_reject }

  - id: doc_updater
    runtime: claude
    context_strategy: last_only
    system_prompt: "运行 git diff main。只更新变更的文档部分。"
    handoffs: [{ to: code_reviewer }]

  - id: code_reviewer
    runtime: gemini                    # 跨 LLM：合并前不同视角
    context_strategy: last_only
    system_prompt: "运行 git diff main 和 pytest。合并前审查。"
    gate:
      type: llm
      max_retries: 3
    handoffs:
      - { to: branch_manager, condition: on_approve }
      - { to: fixer, condition: on_reject }

  - id: branch_manager
    runtime: claude
    context_strategy: last_only
    mcp: [{ server: github }]
    system_prompt: "审查已批准。提交变更，合并功能分支到 main。"

  - id: fixer
    runtime: claude
    context_strategy: both             # 需要拒绝原因 + 完整代码上下文
    mcp: [{ server: github }]
    system_prompt: "在功能分支上修复审查问题。重新运行 pytest。"
    handoffs: [{ to: code_reviewer }]
```

```bash
cd aqm
aqm run "为 aqm validate 添加 --strict 标志" --pipeline dev
# → 7 个 Agent，全自动：分析 → 实现 → 测试 → 文档 → 审查 → 合并
```

用这种方式添加的功能：`aqm validate --strict`、资源可用性检查、重试策略——所有都不需要手动写一个测试或文档更新。

**实测流水线时间**（实际运行，除代码审查器用 Gemini 外全部用 Claude）：

| 阶段 | 干净运行 | 带重启 |
|---|---|---|
| impact_analyzer | 6 分钟 | 3 分钟 |
| implementer | 12 分钟 | 6 分钟 |
| tester | 3 分钟 | 5 分钟 |
| doc_updater | 1.5 分钟 | 1 分钟 |
| branch_manager | 1 分钟 | 1 分钟 |
| code_reviewer | 5 分钟 | 1 分钟 |
| **总计** | **29 分钟** | **16 分钟** |

### 案例 2：架构决策会话

多个专家辩论直到他们同意——像真实的设计会议。

```yaml
agents:
  - id: architect
    runtime: claude
    system_prompt: |
      你是软件架构师。讨论: {{ input }}
      先前讨论: {{ transcript }}

  - id: security
    runtime: gemini
    system_prompt: |
      你是安全专家。关注威胁: {{ input }}
      先前讨论: {{ transcript }}

  - id: design_session
    type: session
    participants: [architect, security]
    max_rounds: 5
    consensus:
      method: vote
      keyword: "VOTE: AGREE"
      require: all
    summary_agent: architect
    handoffs: [{ to: developer }]
```

```
── 第 1 轮 ──
  [architect] JWT 用于无状态扩展。每 15 分钟轮换令牌...
  [security] 令牌撤销是薄弱环节。考虑混合方案...
── 第 2 轮 ──
  [architect] 同意 —— 带 Redis 黑名单的混合方案。VOTE: AGREE  ✓
  [security] Redis 方案可行。VOTE: AGREE  ✓
✓ 达成共识（第 2 轮）
```

### 案例 3：人在环部署

AI 做工作，但人类批准关键步骤。

```yaml
agents:
  - id: developer
    runtime: claude
    human_input:
      mode: before
      prompt: "你想要什么功能？有任何限制吗？"
    system_prompt: "构建: {{ input }}"
    handoffs: [{ to: deployer }]

  - id: deployer
    runtime: claude
    gate: { type: human }              # 流水线暂停等待人工批准
    system_prompt: "部署: {{ input }}"
```

```bash
aqm run "重构认证模块"
# → 开发者先询问你的输入
# → 编码后，流水线在部署器处暂停
aqm approve T-ABC123 -r "LGTM，部署到预发布环境"
```

## 功能特性

### 多 LLM Runtime

每个 Agent 混合提供商。Claude 写代码，Gemini 审查，Codex 测试。

```yaml
agents:
  - id: planner
    runtime: gemini
    model: gemini-2.5-flash
    system_prompt: "规划: {{ input }}"
    handoffs: [{ to: developer }]

  - id: developer
    runtime: claude
    mcp: [{ server: github }]         # 自动代码模式
    system_prompt: "实现: {{ input }}"
```

### 会话式讨论

会话节点让多个 Agent **分轮讨论**直到共识——像开会一样。

```yaml
agents:
  - id: design_review
    type: session
    participants: [architect, frontend, security]
    turn_order: round_robin           # 或: moderator
    max_rounds: 5
    consensus:
      method: vote                    # 或: moderator_decides
      keyword: "VOTE: AGREE"
      require: all                    # 或: majority
    summary_agent: architect
    handoffs: [{ to: implementer }]
```

**共识方法：**

| 方法 | 工作原理 |
|---|---|
| `vote` | 每个 Agent 在输出中包含关键词。当 `all` 或 `majority` 同意时达成共识。 |
| `moderator_decides` | 只有 `summary_agent` 能宣布共识。 |

生成 `transcript.md` 会议记录。自由混合：`批量 → 会话 → 批量`。

### 会议模式（多 Agent 协作讨论）

独立于流水线的轻量级多 Agent 协作功能。多个 Agent 围绕共享文件进行讨论，支持持久化、恢复和历史记录。

```bash
# CLI 运行会议
aqm meeting -t "API 设计评审" -a claude,kilo,opencode -r 2 --timeout 300

# Web UI
aqm serve  # → 访问 /meeting 页面，图形化创建和管理会议
```

**特性：**

| 功能 | 说明 |
|------|------|
| 共享文件协作 | 所有 Agent 读写同一个 `meeting.md`，上下文自动传递给下一个 Agent |
| 多轮讨论 | 可配置讨论轮数，每轮所有 Agent 依次发言 |
| 持久化存储 | 会议配置和状态保存到 `.aqm/meetings/`，服务器重启后自动恢复 |
| 中断恢复 | 会议中断后继续运行，已完成的 Agent 自动跳过 |
| 历史会议 | Web UI 可查看和管理所有历史会议，根据状态显示不同操作按钮 |
| 实时流式 | Web UI 通过 SSE 实时推送 Agent 发言进度 |
| 自定义 Agent | 支持自定义 Agent 角色和系统提示词 |

**CLI 参数：**

```bash
aqm meeting [OPTIONS]
  -t, --topic       会议主题 (必需)
  -d, --description 会议描述/议程
  -a, --agents      Agent 列表，逗号分隔或多次使用 (claude,opencode,kilo,cline)
  -r, --rounds      讨论轮数 (默认: 1)
  --timeout         每个 Agent 超时秒数 (默认: 300)
  -f, --shared-file 共享文件路径 (默认: meeting.md)
```

**Web UI：**

- **概览页** (`/`)：显示会议统计（总数/进行中/已完成）和最近会议列表
- **会议页** (`/meeting`)：创建会议、查看实时进度、浏览历史会议
- 会议状态按钮：待开始→"启动"、已停止→"继续"、失败→"重试"、已完成→"重新运行"

### 分块分解

将任务分解为可跟踪的工作单元。Agent 通过输出指令管理分块。

```yaml
- id: build_session
  type: session
  participants: [pm, dev]
  consensus:
    require_chunks_done: true         # 所有分块必须完成
  chunks:
    enabled: true
    initial:
      - "设置项目结构"
      - "实现认证流程"
      - "添加单元测试"
```

**Agent 指令：**
```
CHUNK_ADD: 实现拖放功能     → 添加新分块
CHUNK_DONE: C-001           → 标记分块完成
CHUNK_REMOVE: C-003         → 移除分块
```

模板变量 `{{ chunks }}` 向提示词注入状态表。存储在 `chunks.json`。

**CLI：**
```bash
aqm chunks list T-ABC123
aqm chunks add T-ABC123 "新功能"
aqm chunks done T-ABC123 C-001
aqm chunks remove T-ABC123 C-002
```

**Web API：** 在 `/api/tasks/{id}/chunks` 进行 CRUD 操作，带 SSE `chunk_update` 事件。

### 上下文策略（Token 优化）

每个 Agent 有 `context_strategy` 控制 `{{ context }}` 包含什么。通过避免冗余上下文注入节省 token。

```yaml
agents:
  - id: planner
    context_strategy: both            # 完全可见性（默认）

  - id: developer
    context_strategy: last_only       # 仅上一阶段 → 节省 55%
    context_window: 1

  - id: deployer
    context_strategy: none            # 无上下文 → 节省 85%
```

| 策略 | `{{ context }}` 包含 | Token 节省 | 使用场景 |
|---|---|---|---|
| `both`（默认） | 共享 context.md + Agent 私有笔记 | — | 完全可见性，向后兼容 |
| `shared` | 智能窗口化共享 context.md | ~相同 | 需要流水线历史的 Agent |
| `last_only` | 仅最近阶段输出 | **~55%** | 只需要上一步的 Agent |
| `own` | 仅 Agent 私有 `agent_{id}.md` | **~85%** | 有自己的笔记的专注 Agent |
| `none` | 空（不注入上下文） | **~85%** | 自包含的无需上下文的 Agent |

在 10 个 Agent 的流水线上基准测试（见 `tests/bench_token_efficiency.py`）：
```
策略      总 Token   节省
both              12,233        0%
last_only          5,504       55%
none               1,873       85%
```

### 交接路由

三种任务流策略：

```yaml
# 静态 —— 固定目标
handoffs:
  - to: reviewer
    condition: always

# 扇出 —— 并行多个目标
handoffs:
  - to: qa, docs, deploy
    condition: on_approve

# Agent 决定 —— Agent 运行时选择目标
handoffs:
  - to: "*"
    condition: auto    # Agent 在输出中包含 HANDOFF: <id>
```

**条件：** `always`、`on_approve`、`on_reject`、`on_pass`、`auto` 或表达式（`severity == critical`）

**负载变量：** `{{ output }}`、`{{ input }}`、`{{ reject_reason }}`、`{{ gate_result }}`

### 人工输入（人在环）

```yaml
agents:
  - id: planner
    human_input:
      mode: before           # Agent 运行前询问
      prompt: "你想要什么具体功能？"

  - id: developer
    human_input: true        # 简写：Agent 可通过 HUMAN_INPUT: <问题> 在执行中询问
```

**模式：**

| 模式 | 行为 |
|---|---|
| `before` | Agent 运行前总是暂停并询问用户。 |
| `on_demand` | Agent 通过输出中的 `HUMAN_INPUT: <问题>` 指令请求输入。 |
| `both` | 结合两种模式。 |

### 运行时重试

在运行时失败（超时、CLI 缺失、上下文溢出）时自动重试 Agent。这与 `gate.max_retries` 不同，后者处理质量拒绝重试。

```yaml
agents:
  - id: researcher
    retry:
      max_retries: 2              # 运行时错误最多重试 2 次
      backoff: 5                  # 重试间隔 5 秒
      fallback_context_strategy: last_only  # 重试时减少上下文
```

| 字段 | 类型 | 默认 | 描述 |
|---|---|---|---|
| `max_retries` | `int` | `0` | 运行时错误最大重试次数（0 = 不重试） |
| `fallback_context_strategy` | `string` \| `null` | `null` | 重试时的上下文策略覆盖（上下文溢出后减少 token 使用） |
| `backoff` | `int` | `0` | 重试间隔秒数 |

### 质量门（质量控制）

```yaml
gate:
  type: llm              # LLM 自动评估 → 批准/拒绝
  prompt: "这是生产就绪的吗？"
  max_retries: 3         # 拒绝 → 最多重试 3 次，然后失败

gate:
  type: human            # 暂停流水线 → aqm approve/reject
```

### 任务重启与恢复

从任何阶段恢复失败或已完成的任务——无需从头开始。

**工作原理：**
- 每个阶段前，aqm 快照所有上下文文件（context.md、Agent 笔记、转录）
- 失败时，运行时部分输出被保留
- `aqm restart` 将上下文回滚到选定阶段并重新执行

```bash
# 从失败阶段重启（自动检测）
aqm restart T-A3F2B1

# 从特定阶段重启
aqm restart T-A3F2B1 --from-stage 3

# 从头重新运行
aqm restart T-A3F2B1 --from-stage 1
```

适用于 `failed`、`completed`、`stalled` 和 `cancelled` 任务。Web 仪表盘也提供带阶段选择的重启按钮。

| 事件 | 操作 |
|-------|--------|
| 每个阶段前 | 上下文文件快照到 `snapshots/stage_N/` |
| 任务成功完成 | 所有快照清理 |
| 任务失败 | 快照保留以便重启 |
| `aqm restart --from-stage N` | 从快照恢复上下文，截断阶段，流水线恢复 |

### MCP 服务器

通过 [Model Context Protocol](https://modelcontextprotocol.io/) 赋予 Agent 现实世界能力。

```yaml
mcp:
  - server: github
  - server: filesystem
    args: ["/path/to/dir"]
  - server: custom-db
    command: node
    args: ["./mcp-server.js"]
    env: { DATABASE_URL: "postgres://..." }
```

### 参数（可移植流水线）

```yaml
params:
  model: claude-sonnet-4-20250514
  project_path:
    type: string
    required: true
    prompt: "项目根路径？"

agents:
  - id: dev
    model: ${{ params.model }}
```

**覆盖：** `aqm run "任务" --param model=claude-opus-4-6`

### 导入/继承

```yaml
imports:
  - from: ./shared/reviewers.yaml
    agents: [security_reviewer]

agents:
  - id: base_reviewer
    abstract: true
    runtime: claude
    gate: { type: llm }

  - id: code_reviewer
    extends: base_reviewer
    system_prompt: "审查代码: {{ input }}"
```

### 流水线注册表（共享与发现）

```bash
aqm search "code review"              # 查找社区流水线
aqm pull security-audit               # 安装最新版本
aqm pull security-audit@1.0.0         # 安装特定版本
aqm pipeline versions security-audit  # 列出所有版本
aqm publish --name my-pipeline        # 共享你的（自动递增）
aqm publish --version 2.0.0           # 以特定版本共享
```

流水线支持语义版本控制。每个版本独立存储在注册表中，允许团队固定特定版本或始终拉取最新。

Web 仪表盘也支持带版本下拉的版本化拉取，并提供**可视化 Agent 编辑器**用于添加、编辑和删除 Agent 而无需编写 YAML。

## CLI 参考

```bash
# 设置
aqm init                              # 交互式设置向导
aqm validate                          # 验证 agents.yaml
aqm validate --strict                 # 警告视为错误（退出码 1）
aqm agents                            # 显示 Agent 图

# 运行
aqm run "添加 JWT 认证"                # 运行默认流水线
aqm run "修复 bug" --agent bug_fixer   # 从特定 Agent 开始
aqm run "构建 API" --pipeline backend  # 命名流水线
aqm run "任务" --param model=opus      # 覆盖参数

# 管理
aqm list                              # 列出所有任务
aqm status T-ABC123                   # 任务详情
aqm cancel T-ABC123                   # 取消任务
aqm fix T-ABC123 "修复颜色"           # 带上下文跟进
aqm restart T-ABC123                  # 从失败阶段重启
aqm restart T-ABC123 --from-stage 2   # 从特定阶段重启

# 质量门与人工输入
aqm approve T-ABC123                  # 批准质量门
aqm reject T-ABC123 -r "需要测试"      # 拒绝质量门
aqm human-input T-ABC123 "响应"       # 回答 Agent 问题

# 分块
aqm chunks list T-ABC123              # 状态表
aqm chunks done T-ABC123 C-001        # 标记完成

# 流水线
aqm pipeline list                     # 列出流水线
aqm pipeline create review --ai       # AI 生成
aqm pipeline default review           # 设置默认

# 注册表
aqm search "code review"              # 搜索（显示版本）
aqm pull code-review-pipeline         # 安装最新版本
aqm pull code-review@1.0.0            # 安装特定版本
aqm pipeline versions code-review     # 列出可用版本
aqm publish --name my-pipeline        # 共享（自动递增版本）
aqm publish --version 2.0.0           # 以特定版本共享

# 仪表盘
aqm serve                             # Web UI 在 localhost:8000

# 会议
aqm meeting -t "API 设计评审"          # 运行会议
aqm meeting -t "设计讨论" -a claude,kilo  # 指定 Agent
```

## agents.yaml 参考

### 入口点（自动路由）

```yaml
entry_point: auto    # LLM 根据用户输入选择最佳 Agent
# entry_point: first  #（默认）总是从第一个 Agent 开始
```

### Agent 定义

| 字段 | 类型 | 默认 | 描述 |
|---|---|---|---|
| `id` | `string` | — | 唯一标识符（必需） |
| `name` | `string` | `""` | 显示名称（为空时从 id 自动生成） |
| `type` | `"agent"` \| `"session"` | `"agent"` | 节点类型 |
| `runtime` | `"claude"` \| `"gemini"` \| `"codex"` \| `"opencode"` \| `"kilo"` \| `"cline"` | — | `type: agent` 必需 |
| `model` | `string` | CLI 默认 | 模型覆盖 |
| `system_prompt` | `string` | `""` | Jinja2 模板：`{{ input }}`、`{{ context }}`、`{{ transcript }}`、`{{ chunks }}` |
| `context_strategy` | `"none"` \| `"last_only"` \| `"own"` \| `"shared"` \| `"both"` | `"both"` | 注入什么上下文（token 优化） |
| `context_window` | `int` | `3` | 最近阶段完整；旧阶段摘要（0 = 全部） |
| `human_input` | `boolean` \| `object` | `null` | 人在环输入（`before`、`on_demand`、`both`） |
| `retry` | `object` \| `null` | `null` | 运行时错误重试策略 —— 见下方**重试**格式 |
| `handoffs` | list | `[]` | 路由规则 —— 见下方**交接**格式 |
| `gate` | `object` | `null` | 质量门 —— 见下方**质量门**格式 |
| `mcp` | list | `[]` | MCP 服务器连接 —— 见下方**MCP**格式 |
| `cli_flags` | 字符串列表 | `null` | 额外 CLI 标志，如 `["--verbose"]` |
| `abstract` | `boolean` | `false` | 仅模板 Agent（不执行） |
| `extends` | `string` | `null` | 继承的父 Agent ID |

### 交接格式

`handoffs` 中每个项是一个对象：

```yaml
handoffs:
  - to: reviewer                    # 目标 Agent ID（必需）
    task: "review"                  # 标签（可选）
    condition: on_approve           # always | on_approve | on_reject | on_pass | auto
    payload: "{{ output }}"         # Jinja2 模板（默认：{{ output }}）

  - to: "qa, docs"                  # 逗号分隔 = 扇出到多个 Agent
    condition: always
```

| 字段 | 类型 | 默认 | 描述 |
|---|---|---|---|
| `to` | `string` | — | 目标 Agent ID，或逗号分隔用于扇出（`"qa, docs"`） |

