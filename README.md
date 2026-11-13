# aqm

**用 YAML 组建 AI Agent 团队。不写编排代码，不接 API SDK。**

多个 AI Agent 通过**显式队列**传递任务，通过**多轮会话**讨论直到共识，通过**质量门**互相审查。你定义一次流水线，CLI 和 Web 控制台都能跑。

```text
 [你] ──输入──► [规划] ──► [实现] ──► [审查] ──批准──► [交付]
                          ▲            │         │
                          └── 拒绝 ────┘         └── 需要你决定 ──► [人工审批]

 另一种用法：多个 Agent 先开会，你当主持人、随时插话
 [架构] ⇄ [实现] ⇄ [测试] ⇄ [你]
```

- 当前版本：**v1.3.1**（`aqm --version`）
- Python：**>= 3.11**（实测 3.14.4）
- 许可：MIT

---

## 目录

- [为什么用 aqm](#为什么用-aqm)
- [核心概念](#核心概念)
- [安装](#安装)
  - [前置要求](#1-前置要求)
  - [安装 aqm](#2-安装-aqm)
  - [安装运行时 CLI](#3-安装运行时-cli)
  - [权限与安全](#4-权限与安全)
  - [验证安装](#5-验证安装)
- [5 分钟快速开始](#5-分钟快速开始)
- [Web 控制台](#web-控制台)
- [核心功能](#核心功能)
- [工作块（把大任务拆成可勾选的小块）](#工作块把大任务拆成可勾选的小块)
- [会议模式](#会议模式)
- [配置参考：`.aqm/config.yaml`](#配置参考aqmconfigyaml)
- [CLI 参考](#cli-参考)
- [流水线 YAML 参考](#流水线-yaml-参考)
- [HTTP API 与 SSE](#http-api-与-sse)
- [故障排查](#故障排查)
- [开发与测试](#开发与测试)
- [目录结构](#目录结构)

---

## 为什么用 aqm

单个 Agent 用**同一套偏见**写代码又审查代码，很难发现自己的盲点。

aqm 给你一个**团队**：每个成员有独立角色、独立提示词、可以跑在不同的 LLM 上。质量门负责拒绝不合格输出，会话节点让成员在动手前先辩论，人工审批让你在关键节点掌握决定权。

```yaml
# .aqm/pipelines/default.yaml —— 一个 YAML 就是一个团队
agents:
  - id: developer
    name: 实现工程师
    runtime: claude
    system_prompt: "实现下面的需求：\n{{ input }}"
    handoffs:
      - to: reviewer

  - id: reviewer
    name: 代码审查员
    runtime: opencode                # 换一个模型，容易发现不同的问题
    system_prompt: "审查安全性、边界条件和可维护性：\n{{ input }}"
    gate:
      type: llm
      prompt: "这份实现可以进入交付吗？"
      max_retries: 3                 # 不合格自动打回，最多重试 3 次
    handoffs:
      - { to: writer, condition: on_approve }
      - { to: developer, condition: on_reject }

  - id: writer
    name: 交付文档
    runtime: kilo
    system_prompt: "根据上下文写交付说明：\n{{ input }}"
    gate:
      type: human                    # 交给你最终确认
```

要点：

| 能力 | 说明 |
|---|---|
| 显式队列 | 每个 Agent 一个队列，任务按阶段推进，不会"聊丢" |
| 多运行时 | Claude / Gemini / Codex / OpenCode / Kilo / Cline，可混用 |
| 质量门 | LLM 自动评审或人工审批，拒绝后自动返工或转交 |
| 多轮会话 | 流水线内可插入 `type: session` 节点做圆桌讨论 |
| 独立会议 | 不跑流水线也能开会，产出需求文档后一键转开发任务 |
| 工作块 | 大任务自动拆分为可勾选的小块，跨阶段、跨重启都不丢 |
| Web 控制台 | 实时输出、审批、插话、暂停/继续/停止、模型选择 |
| 可共享 | 流水线可从注册表拉取、发布、指定版本 |

更细的设计说明见 [`docs/concepts.md`](docs/concepts.md) 和 [`docs/spec.md`](docs/spec.md)。

---

## 核心概念

| 概念 | 含义 |
|---|---|
| **任务（Task）** | 一次运行的实体，ID 形如 `T-3A7F2B`。状态：`pending / in_progress / awaiting_gate / awaiting_human_input / approved / rejected / completed / failed / cancelled / stalled` |
| **阶段（Stage）** | 一个 Agent（或会话节点）执行一轮，写入 `stages[]` 与 `context.md` |
| **交接（Handoff）** | 阶段结束后把任务交给谁，支持固定目标、扇出多目标、按条件分流、Agent 自选 |
| **质量门（Gate）** | 阶段后的检查：`llm`（模型评审）或 `human`（人工审批） |
| **会话节点（Session）** | 多个 Agent 分轮讨论直到共识，可嵌在流水线中 |
| **会议（Meeting）** | 独立于流水线的多 Agent 讨论，有自己的共享文件与需求文档 |
| **工作块（Chunk）** | 任务内部的可勾选工作单元，形如 `C-001`，可被 Agent 或人推进 |
| **上下文（Context）** | 每个任务目录下的 `context.md` / `transcript.md`，按策略裁剪后喂给下一个 Agent |
| **常驻会话** | 同一 Agent 在一个任务里复用同一个 CLI 会话，减少冷启动与重读上下文 |
| **运行时（Runtime）** | 实际执行推理的 CLI：`claude`、`gemini`、`codex`、`opencode`、`kilo`、`cline` |

项目目录结构：

```text
<项目>/
└── .aqm/
    ├── config.yaml            # 项目配置（语言、超时、工作块、自动恢复…）
    ├── pipelines/             # 多条流水线，一条一个 YAML
    │   └── default.yaml
    ├── tasks/<task-id>/       # 每个任务的产物
    │   ├── context.md
    │   ├── transcript.md
    │   ├── chunks.json
    │   └── chunks.directives.log
    ├── meetings/<meeting-id>/ # 每个会议独立的共享文件
    │   ├── meeting.md
    │   └── REQUIREMENTS.md
    └── registry/              # 本地流水线注册表
```

---

## 安装

### 1. 前置要求

| 依赖 | 版本要求 | 说明 |
|---|---|---|
| Python | `>= 3.11` | 实测 3.14.4 |
| Node.js + npm | Node 20+ / npm 10+ | 仅用于安装各 Agent CLI；实测 Node 24.15.0 + npm 11.12.1 |
| Git | 任意较新版本 | 拉取/发布流水线；建议项目本身用 git 管理 |
| 终端 | Windows PowerShell / macOS zsh / Linux bash | 全平台可用 |

> **建议**：把 aqm 和所有 Agent CLI 装在**同一个项目专用目录**里。所有运行时都会直接读写项目目录并执行命令，隔离能让出问题时更容易回滚。

### 2. 安装 aqm

```bash
git clone https://github.com/aqm-framework/aqm.git
cd aqm
python -m venv ../venv

# Windows
..\venv\Scripts\activate
# macOS / Linux
# source ../venv/bin/activate

pip install -e ".[serve,dev]"
aqm --version
```

输出：

```text
aqm, version 1.3.1
```

可选依赖：

| extra | 包含 | 何时需要 |
|---|---|---|
| `serve` | `fastapi`、`uvicorn` | 使用 Web 控制台 `aqm serve` |
| `dev` | `pytest`、`pytest-asyncio` | 跑测试 / 二次开发 |

最小安装（只用 CLI）也可以只 `pip install -e .`，但 `aqm serve` 需要 `serve` extra。

### 3. 安装运行时 CLI

aqm 本身不调用任何模型 API，它**驱动本地 CLI**。至少装一个：

| 运行时 | 安装命令 | 可执行文件 | 登录 / 认证 | 实测版本 |
|---|---|---|---|---|
| `claude` | `npm i -g @anthropic-ai/claude-code` | `claude` | 首次运行 `claude` 按提示登录（或设置 `ANTHROPIC_API_KEY`） | 2.1.317 |
| `gemini` | `npm i -g @google/gemini-cli` | `gemini` | 运行 `gemini` 完成 Google 账号授权 | 0.68.0 |
| `codex` | `npm i -g @openai/codex` | `codex` | `codex login` | 0.167.x |
| `opencode` | `npm i -g @opencode/cli` | `opencode` | `opencode auth login` | 2.2.5 |
| `kilo` | `npm i -g @kilocode/cli` | `kilo` | `kilo auth login`（或用 `AGNES_API_KEY` / `SENSENOVA_API_KEY` 走免费模型） | 7.8.18 |
| `cline` | `npm i -g cline` | `cline` | 首次运行 `cline` 按提示配置 Provider | 3.0.74 |

**两个容易装错的包：**

- `opencode` 有两个 npm 包。aqm 使用 **`@opencode/cli`（2.x）**，它会调用 `opencode run --auto --format json`。旧包 `opencode-ai`（1.x）参数不兼容，装错会表现为"Agent 无输出"。
- Cline 请装 **`cline`（3.x）**，二进制名是 `cline`。`@cline/cli` 是实验包，二进制名是 `clite`，aqm 识别不到。

检查是否装好：

```bash
claude --version
opencode --version
kilo --version
cline --version
```

实测可用的一套分工（四个成员跑不同运行时，互相容易发现问题）：

| 角色 | 运行时 | 说明 |
|---|---|---|
| 分析 / 规划 | `claude` | 结构化推理稳定，适合先出方案 |
| 实现 | `opencode` | 代码改动快，支持流式工具动作 |
| 审查 | `cline` | 读本地 Provider 配置，适合做第二意见 |
| 长文交付 | `kilo` | 写文档/大文件慢，`timeouts.kilo` 建议 1800s |

Windows 提示：

- `opencode` / `kilo` / `cline` 装好后是可执行的 `.cmd` / `.ps1` shim。如果 PowerShell 报"禁止运行脚本"，执行一次 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`。
- npm 全局目录（`%APPDATA%\npm`）必须在 `PATH` 里，否则 aqm 会报 `CLI not found`。
- 所有命令都要在**已激活 venv** 的终端里执行，或者用完整路径 `D:\path\to\venv\Scripts\aqm.exe`。

### 4. 权限与安全

aqm 会以**自动批准 / 跳过权限询问**的方式调用每个 CLI，因此 Agent 可以直接改文件、跑命令。这是自动化流水线的前提，也意味着你必须信任运行位置。

| 运行时 | aqm 实际附加的权限参数 |
|---|---|
| `claude` | `--print --dangerously-skip-permissions`（流式会话用 `--permission-mode acceptEdits`） |
| `gemini` | `--auto` |
| `codex` | `exec … --auto` |
| `opencode` | `run … --auto` |
| `kilo` | `run … --auto --pure` |
| `cline` | `--json --auto-approve --yolo` |

补充说明：

- **文件权限**：Agent 需要项目目录的读写权限（它会直接改文件、建目录）。
- **命令执行**：Agent 会调用 shell 跑测试、构建、git 等，确保这些命令在 `PATH` 里。
- **网络**：需要访问对应模型服务；kilo / cline 还会访问各自网关与 Provider。
- **不需要管理员 / root 权限**，也不建议用管理员账户运行流水线。

- **Claude 首次会在 `~/.claude.json` 里记录项目信任**。aqm 会自动写入 `hasTrustDialogAccepted`，避免非交互模式下被"是否信任此目录"卡住。
- **API Key 不由 aqm 保存**。Cline 的 Key 存在 Cline 自己的配置里，aqm 只读取 Provider 与模型 ID；Kilo 的额度与登录同样由它自己管理。
- **强烈建议**：`git init` 并先提交一次，再让 Agent 动手。所有 Agent 的改动都在你的 git 里可审查、可回滚。
- 不要把个人主目录、含密钥的目录直接交给流水线。

### 5. 验证安装

```bash
aqm --version          # aqm, version 1.3.1
aqm init --path .      # 初始化 .aqm/（交互式选择模板 / 注册表 / AI 生成）
aqm validate           # 校验流水线 YAML
aqm agents             # 打印成员与交接图
```

`aqm init` 会创建：

```text
.aqm/pipelines/default.yaml   # 示例流水线
.aqm/tasks/                   # 任务目录
```

并把它设为默认流水线。

---

## 5 分钟快速开始

```bash
# 1. 准备一个空项目
mkdir demo && cd demo
git init
aqm init --path .

# 2. 看一眼团队
aqm agents

# 3. 跑一个任务
aqm run "给这个项目加一个 /health 接口，返回版本号"

# 4. 查看状态（也可用 aqm list）
aqm status T-XXXXXX

# 5. 在浏览器里看实时输出
aqm serve --port 8000
# 打开 http://127.0.0.1:8000
```

常用后续操作：

```bash
aqm list --filter awaiting_gate    # 等我审批的任务
aqm approve T-XXXXXX -r "通过"      # 审批通过
aqm reject  T-XXXXXX -r "缺少边界测试，请补充"   # 拒绝并返工
aqm fix T-XXXXXX "登录按钮颜色改深一点"          # 带着上下文追加需求
aqm restart T-XXXXXX --from-stage 2             # 从第 2 阶段重跑
aqm cancel T-XXXXXX                            # 停止任务
```

---

## Web 控制台

```bash
aqm serve --port 8000 --host 127.0.0.1
# 也可以指定项目：aqm --project E:\path\to\project serve --port 8000
```

### 页面地图

| 路径 | 页面 | 内容 |
|---|---|---|
| `/` | 概览 | 状态统计（含「已暂停」）、最近任务、任务快速创建 |
| `/meeting` | 会议 | 创建/编辑会议、实时发言、需求文档、转开发任务 |
| `/tasks` | 任务 | 列表、筛选、批量停止/删除、流水线列 |
| `/tasks/{id}` | 任务详情 | 5 个页签（见下） |
| `/agents` | 代理 | 交接关系图、成员详情 |
| `/pipelines` | 流水线 | 列表、可视化编辑器、YAML 编辑器、模型选择 |
| `/registry` | 注册表 | 搜索、拉取、发布、版本 |
| `/validate` | 校验 | 按 JSON Schema 校验流水线，可开严格模式（警告视为错误） |

左栏顺序：**概览 → 会议 → 任务 → 代理 → 流水线 → 注册表 → 校验**。会议排在任务之前。

页面内容区最大 1400px 宽，宽屏下不会拉得过散。

### 任务详情的 5 个页签

| 页签 | 内容 |
|---|---|
| **概览** | 状态徽章、阶段列表、审批/输入面板、重启面板、插话框 |
| **实时输出** | 当前成员流式输出、思考（可折叠）、工具动作、成员状态、读秒 |
| **时间线** | 每个阶段完整输出、门结果、审批记录 |
| **上下文** | `context.md` 的实际内容，按上下文策略裁剪后的样子 |
| **工作块** | 工作块列表、进度、负责人、▶ 逐块启动、编辑器 |

运行中的任务，**审批与人工输入固定显示在「实时输出」页**，不需要来回切页签。

### 实时输出

- 每个成员一张状态卡片：`等待中 / 思考中 / 输出中 / 完成 / 失败 / 已跳过`。
- 思考过程默认折叠，点开才展开；正文与思考分开渲染。
- 工具动作（读写文件、执行命令）以紧凑行展示，不淹没正文。
- SSE 建连后会先收到一帧 `snapshot`，因此刚打开页面就能看到当前阶段、当前成员和已产出的文本，不会出现"空白等半天"。
- 顶部有吸顶状态条：当前阶段、成员、已用时间、暂停/继续/停止按钮。
- 任务结束时展示**生成的文件路径与说明**；交付物按项目语言命名（见[交付物文件名](#交付物文件名)）。

### 暂停 / 继续 / 停止

| 操作 | 行为 |
|---|---|
| **暂停** | 协作式：当前成员先把正在生成的这条回复写完，然后在阶段/轮次边界阻塞。已产出的内容不丢。 |
| **继续** | 从暂停点恢复，不重复已完成的工作块。 |
| **停止** | 立即中断当前成员，任务置为 `cancelled`，同时清除暂停。 |

暂停状态会持久化到 `metadata.paused`，并且在**所有页面**统一显示为「已暂停」——概览统计、最近任务、任务列表筛选、详情页徽章都不会再出现「运行中 / 思考中」。

暂停期间：

- 实时输出显示「已暂停 · 收尾中…」；
- 不再新建「思考中」块，也不再显示"运行中成员"；
- 若服务重启，暂停状态会从元数据恢复，继续仍停在正确的阶段。

### 人工插话（运行中）

在实时输出页下方的输入框直接输入：

- **Enter 发送**，Shift+Enter 换行（兼容中文输入法，不会在选词时误发）。
- 插话会**立即打断当前成员的这一轮**，把说明追加进提示词后让**同一个成员重新作答**，而不是排队等下一阶段。
- 插话卡片在回复完成后移出实时输出区，保留在时间线里，避免"旧插话反复出现"。
- 任务重启后，时间线只显示本轮（按 `run_started_at` 划分）的插话。

### 新建任务与模型选择

- 新建任务必须**手动选择流水线**（默认停在「选择流水线」，未选会提示）。
- 优先级、起始代理、参数可在表单里覆盖。
- 代理表单里选「运行时」后会加载该运行时的**模型目录**：
  - 选项字符串与对应 CLI 的 `--model` 语法完全一致，**原样透传**；
  - 选「默认模型」= 不传 `--model`，交给该 CLI 自己的默认值；
  - 切换运行时会清空该代理已有的 `cli_flags` 和 `model`，避免把上一个运行时的参数带过去。
- 会议页的每个成员也能单独选模型。

模型目录来源：

| 运行时 | 来源 |
|---|---|
| `claude` | `~/.claude/settings.json` 的 `model` 与 `ANTHROPIC_*` 环境变量、`CLAUDE_CODE_SUBAGENT_MODEL` |
| `opencode` | `opencode models` 输出、`~/.config/opencode/opencode.json`、旧版 `~/.opencode/config.json` |
| `kilo` | `kilo models` 输出；取不到时用内置兜底列表（`agnes/*`、`sensenova/*`、`kilo/~*`） |
| `cline` | `~/.cline/data/settings/providers.json` 里的 Provider 与模型 |
| `gemini` / `codex` | 无目录，直接在输入框手填 |

对应接口：

```bash
GET /api/meetings/models?runtime=opencode
# {"runtime":"opencode",
#  "models":[{"value":"opencode/deepseek-v4.1-flash-free","label":"..."}],
#  "default":"opencode/...","source":"cli"}
```

### 项目切换与服务重启

侧栏底部显示当前项目路径，可切换项目、初始化新项目。切换后需要重启服务（页面会给出提示并轮询重启状态）。

---

## 核心功能

### 多运行时

```yaml
agents:
  - id: a
    runtime: claude      # 也可以 gemini / codex / opencode / kilo / cline
    model: claude-sonnet-4-6
    cli_flags: ["--verbose"]   # 原样透传给该 CLI
```

- `cli_flags` 是**逃生口**：CLI 新增的参数不必等 aqm 支持，直接写进去。
- `model` 为空时使用该 CLI 的默认模型。
- 同名运行时实例在一个流水线内复用。

### 会话节点（流水线内多轮讨论）

会话节点让多个成员**分轮讨论**直到共识，然后结果流向下一阶段：

```yaml
agents:
  - id: design_review
    type: session
    participants: [architect, frontend, security]
    turn_order: round_robin        # 或 moderator
    max_rounds: 5
    consensus:
      method: vote                 # 或 moderator_decides
      keyword: "VOTE: AGREE"
      require: all                 # 或 majority
      require_chunks_done: false
    summary_agent: architect
    handoffs:
      - to: implementer
```

| 共识方式 | 行为 |
|---|---|
| `vote` | 每个成员在输出里写关键词；`all` / `majority` 满足即达成共识 |
| `moderator_decides` | 只有 `summary_agent` 能宣布共识 |

会话会生成 `transcript.md`。可以自由组合：`批量阶段 → 会话 → 批量阶段`。

### 会议模式

独立于流水线的轻量协作：多个成员围绕**同一个共享文件**讨论，一个会议一个目录，有独立的历史与需求文档。支持人类主持、随时插话、会后一键转开发任务。

```bash
aqm meeting -t "API 设计评审" -a claude,kilo,opencode -r 2 --timeout 300
aqm serve   # → /meeting 图形化创建和管理
```

完整说明见 [会议模式](#会议模式)。

### 常驻会话

同一 Agent 在一个任务内复用同一个 CLI 会话，而不是每个阶段重新启动：

- 减少冷启动、减少重复读取上下文；
- 阶段之间会话进程若已退出，aqm 会**自动重建**再继续，并把真实退出原因写进日志；
- 只有重建失败才报错，错误信息会说明是哪个成员、哪条命令、退出码是多少。

关闭方式：

```yaml
pipeline:
  resident_agents: false
```

### 超时

超时是**空闲超时**，不是总时长：只要还在思考、输出、调工具、报进度，计时就重置。长时间卡死才会触发。

```yaml
timeouts:
  claude: 600
  gemini: 600
  codex: 600
  opencode: 600
  kilo: 1800      # 大项目写入慢，给足时间
  cline: 600
```

会议另用 `--timeout`（每个成员每轮，默认 300s）；Web 表单上限 3600s。

### 运行时重试

区分两种重试：

| 类型 | 配置 | 处理什么 |
|---|---|---|
| 质量重试 | `gate.max_retries` | 输出被质量门拒绝后返工 |
| 运行时重试 | `retry` | CLI 缺失、超时、上下文溢出等执行错误 |

```yaml
agents:
  - id: writer
    retry:
      max_retries: 2
      backoff: 10
      fallback_context_strategy: last_only
```

`max_retries: 0`（默认）表示不重试。

### 上下文策略

上下文会越滚越大。`context_strategy` 控制每个阶段能看到什么：

| 策略 | 内容 |
|---|---|
| `none` | 不给历史，只看本次输入 |
| `last_only` | 只看上一阶段输出 |
| `own` | 只看该成员自己之前阶段的输出 |
| `shared` | 只看共享上下文 |
| `both` | 共享上下文 + 自己的历史（默认） |

`context_window: 3` 控制最近几个阶段保留全文，更早的会被摘要/截断。

### 质量门

```yaml
gate:
  type: llm                 # llm | human
  prompt: "这份实现可以进入交付吗？"
  model: claude-sonnet-4-6  # 可选，评审用模型
  max_retries: 3
```

- 项目级默认值写在 `.aqm/config.yaml` 的 `gate:` 下（`model` / `system_prompt` / `timeout`），Agent 级配置优先。
- LLM 门要求评审模型输出 JSON：`{"decision":"approved|rejected","reason":"..."}`；解析失败时会退回关键词判断，实在判断不了就拒绝（保守）。
- `human` 门会把任务置为 `awaiting_gate`，等你审批。

#### 审批面板会列出具体待确认事项

等待审批时，面板不只给一个原因输入框，而是明确写出「批什么」：

- **审批对象**：阶段编号、成员名称与 ID、本阶段任务名；
- **需要确认的事项**：优先使用门配置的 `gate.prompt`（按行/列表项拆成清单）；没配置时从该阶段输出自动提取——结论行、`- [ ]` / `- [x]` 检查项、Markdown 章节标题；
- **未完成工作块**：列出阻塞 / 进行中 / 待处理的块（阻塞优先），点击跳到「工作块」页签；
- **决策后果**：通过后进入谁、拒绝后转交给谁；
- **输出摘要**：折叠展示，完整内容在「时间线」。

对应接口：

```bash
GET /api/tasks/{task_id}/gate
# {"task_id":"...","status":"awaiting_gate",
#  "gate":{"stage_number":3,"agent_name":"评审与合规官",
#          "items":[{"text":"成员自述审核结论：通过","state":"conclusion"}],
#          "chunks":{"done":20,"total":30,"pending":[...]},
#          "approve_to":[{"id":"writer"}],
#          "reject_to":[{"id":"engineer"}]}}
```

SSE 的 `gate_waiting` 事件带同一个 `gate` 字段。

#### 拒绝后转交给谁

优先级从高到低：

1. 审批时手动选的 **「拒绝后转交给」**；
2. 从拒绝原因里识别成员名（如「请**工程师**补充样本」）；
3. 流水线的 `on_reject` 交接配置；
4. 回到当前成员返工。

拒绝原因会完整写进目标成员的输入，并附带上一版输出。

### 人工输入

Agent 可以主动向你要信息：

```yaml
agents:
  - id: deployer
    human_input:
      enabled: true
      mode: on_demand        # before | on_demand | both
      prompt: "请提供目标环境的访问方式"
```

- `before`：成员运行前总是先问你；
- `on_demand`：成员在输出里写 `HUMAN_INPUT: <问题>` 时才问；
- `both`：两者都启用。

任务进入 `awaiting_human_input`，你在 Web 的实时输出页回答，或：

```bash
aqm human-input T-XXXXXX "使用测试环境，只读权限"
```

### 交接路由

```yaml
handoffs:
  - to: reviewer                      # 固定目标
  - to: "qa, docs"                    # 扇出：并行给多个成员
    condition: always
  - to: deployer
    condition: on_approve
  - to: developer
    condition: on_reject
  - to: security
    condition: auto                   # 由 Agent 在输出里写 HANDOFF: security 决定
```

条件支持：`always` / `on_approve` / `on_reject` / `on_pass` / `auto` / 表达式。

### 回复语言

CLI 自带的系统提示是英文，模型容易在旁白和工具说明里夹英文。可以在项目里钉死回复语言：

```yaml
language: 简体中文
```

这条规则会作为硬指令追加到每个阶段的提示词里：思考、工具说明、旁白、最终答复都用该语言。

临时改语言也可以：在插话里写「请用英文回答」，识别到的语言会覆盖项目默认值。

### 交付物文件名

```yaml
deliverable_naming: chinese     # auto（默认）| chinese | english
```

- `auto`：跟随 `language`——中文项目用中文文件名，英文项目用英文文件名；
- `chinese` / `english`：强制指定；
- **只约束最终交付物**（报告、文档、交付说明），临时脚本和中间文件不受限制，避免破坏工具链。

---

## 工作块（把大任务拆成可勾选的小块）

大任务最容易"跑到一半不知道做到哪了"。工作块把任务拆成 `C-001`、`C-002`… 的可勾选单元，跨阶段、跨重启都保留。

### 自动拆分

任务启动前，aqm 依次查找需求文档：

1. `task.metadata["requirements_path"]`（会议转任务时写入）；
2. `<项目>/REQUIREMENTS.md`；
3. `<项目>/docs/REQUIREMENTS.md`；
4. 都没有则回退到任务描述。

文档超过 **800 字**时调用模型拆分（默认最多 **30 块**），提示词要求覆盖验收标准、不重复、带阶段标签。拆分失败不会阻塞任务，只是没有工作块。

```yaml
chunks:
  enabled: true
  auto_split: true
  auto_split_required: false     # true = 拆分失败就中止任务
  auto_instructions: true        # 把指令协议写进提示词
  enforce: retry                 # off | warn | retry | fail
  retry_limit: 2
  owner_filter: true             # 每个成员只看自己负责的块
  auto_claim: true               # 成员开工自动认领
  max_chunks: 30
  min_description_chars: 800
  requirements_max_chars: 24000
  splitter:                      # 拆分用哪个模型
    runtime: ""
    model: ""
    timeout: 180
```

也可以从流水线 YAML 预置：

```yaml
agents:
  - id: engineer
    chunks:
      enabled: true
      initial:
        - "实现登录接口"
        - { description: "补单元测试", phase: "测试" }
```

### 指令协议

Agent 在输出里写这些行即可推进工作块（大小写不敏感）：

```text
CHUNK_START: C-003
CHUNK_DONE: C-003 C-004
CHUNK_BLOCK: C-005 等待 API 契约确认
CHUNK_REMOVE: C-006
CHUNK_ADD: 实现导出页面 [owner=writer] [phase=交付]
```

- 指令行会从展示输出里剥离，原文追加到 `chunks.directives.log`，方便排查。
- 状态：`pending / in_progress / done / blocked`。
- **同一任务最多一个「进行中」**：开工前会释放上一阶段残留，所有终止路径统一释放，服务启动时清扫全部残留。

### 未完成时怎么办（`enforce`）

| 值 | 行为 |
|---|---|
| `off` | 不检查 |
| `warn` | 只提示 |
| `retry` | **默认**。带剩余清单让该成员重跑，最多 `retry_limit`（默认 2）次 |
| `fail` | 直接失败 |

### Web 里怎么用

- 「工作块」页签显示进度条、负责人、阶段、状态、备注。
- 点 ▶「开始处理」：任务空闲时逐块唤醒负责人；启动后**自动跳到实时输出页**（整页导航，不需要手动刷新）。
- 拆分/重拆会保留已完成块和人工添加块。
- 重拆按钮、增删改、排序、重置都在可视化编辑器里。

### CLI

```bash
aqm chunks list   T-ABC123
aqm chunks add    T-ABC123 "实现导出" --owner writer --phase 交付
aqm chunks start  T-ABC123 C-003 --owner writer
aqm chunks done   T-ABC123 C-003
aqm chunks block  T-ABC123 C-005 "等待接口契约"
aqm chunks remove T-ABC123 C-006
aqm chunks reset  T-ABC123 --keep-done
aqm chunks clear  T-ABC123 --keep-done --yes
aqm chunks import T-ABC123 REQUIREMENTS.md --owner engineer --phase 实现
aqm chunks split  T-ABC123 --replace --keep-done
```

---

## 会议模式

会议是独立于流水线的协作空间，适合"先把方案吵清楚，再决定怎么开发"。

### 创建会议

**CLI：**

```bash
aqm meeting \
  -t "支付模块设计评审" \
  -d "目标是确定对账流程与失败补偿策略" \
  -a claude,kilo,opencode \
  -r 2 \
  --timeout 300
```

| 参数 | 含义 |
|---|---|
| `-t, --topic` | 会议主题（必填） |
| `-d, --description` | 议程 / 描述，会写进共享文件头部 |
| `-a, --agents` | 运行时，逗号分隔或重复传（`claude` / `opencode` / `kilo` / `cline`） |
| `-r, --rounds` | 讨论轮数 |
| `--timeout` | 每个成员每轮超时（秒） |
| `-f, --shared-file` | 共享文件名（默认 `meeting.md`，仍会存到该会议自己的目录） |
| `--cline-model` | 该 Cline Provider 下的模型 ID |

**Web：** 打开 `/meeting`，填主题、描述，添加成员（运行时 + 模型 + 角色 + 系统提示），可添加 `human` 成员当主持人。

### 人类主持与随时插话

- 添加 `runtime: human` 的成员后，轮到你时会等待；你的发言写入共享文件，后续成员自动读到。
- **任何时刻**都可以在会议页下方输入框发言，不限于轮到你：
  - Enter 发送，Shift+Enter 换行；
  - 正在发言的成员本轮看不到，**下一个成员会看到**；
  - 不想说就点「跳过我的本轮」。
- 发言框在实时输出下方，随时可见。

### 运行与显示

| 行为 | 说明 |
|---|---|
| 成员配色 | 每个成员固定颜色，发言卡片一眼区分 |
| 思考折叠 | 思考过程默认折叠，点开再看 |
| 空输出判失败 | 成员输出为空按失败处理，并写明原因，不会"假装完成" |
| 超时 | 每个成员每轮独立计时；Web 表单上限 3600s |
| 明确收尾 | 结束时分完成 / 失败 / 已停止，并列出共享文件与需求文档路径 |

### 共享文件与目录

```text
.aqm/meetings/<meeting-id>/
├── meeting.md          # 讨论记录（含主题/描述头部）
└── REQUIREMENTS.md     # 会后的需求文档
```

修改会议主题或描述会覆盖 `meeting.md` 头部，并加并发锁避免两个写入打架。每个会议独立一份文件，同名也不会互相覆盖（老版本放在项目根目录的文件会自动迁移）。

### 从会议到开发任务

1. 会议完成（或手动点「整理需求」），主持人按模板生成 `REQUIREMENTS.md`：

   ```markdown
   # 项目需求文档
   > 来源会议：…
   > 生成时间：…

   ## 1. 背景与目标
   ## 2. 范围（包含 / 不包含）
   ## 3. 功能需求
   ## 4. 非功能需求
   ## 5. 验收标准
   ## 6. 未决问题
   ```

2. 你可以直接在页面里编辑并保存。
3. 选一条流水线（默认停在「选择流水线」，未选会提示），点「转为开发任务」。
4. 任务描述会带上需求文档路径和全文，并写入 `metadata.requirements_path`，之后自动拆分工作块时就优先用它。
5. **同一个会议不会重复建任务**：已有运行中的任务返回 409；已建过任务也会拦一次，确需重建要显式确认。

---

## 配置参考：`.aqm/config.yaml`

```yaml
# 默认流水线（不指定 --pipeline 时使用）
default_pipeline: default

pipeline:
  max_stages: 20          # 单任务最多阶段数，防止死循环
  resident_agents: true   # 复用常驻 CLI 会话

# 项目级质量门默认值
gate:
  model: claude-sonnet-4-6
  system_prompt: |
    你是严格的质量评审员。只输出 JSON：
    {"decision":"approved" 或 "rejected","reason":"..."}
  timeout: 120

# 每个运行时的空闲超时（秒）
timeouts:
  claude: 600
  gemini: 600
  codex: 600
  opencode: 600
  kilo: 1800
  cline: 600

# 所有成员默认回复语言
language: 简体中文

# 交付物文件名：auto | chinese | english
deliverable_naming: chinese

# 工作块
chunks:
  enabled: true
  auto_split: true
  auto_split_required: false
  auto_instructions: true
  enforce: retry
  retry_limit: 2
  owner_filter: true
  auto_claim: true
  max_chunks: 30
  min_description_chars: 800
  splitter:
    runtime: ""
    model: ""
    timeout: 180

# Web 服务：重启后自动恢复上次运行中的任务
server:
  auto_resume: true
```

环境变量：

| 变量 | 作用 |
|---|---|
| `AQM_DISABLE_AUTO_RESUME=1` | 关闭服务启动时的任务自动恢复 |
| `AQM_CLAUDE_THINKING_TOKENS=1024` | Claude 思考预算（`0` 关闭）；默认 512，范围 0–32000 |
| `ANTHROPIC_API_KEY` | Claude 走 API Key 而非 OAuth 登录时使用 |

自动恢复的防崩溃设计：同一阶段在 10 分钟内最多自动恢复 3 次；超过上限任务保持 `stalled` 并写明原因，等你手动处理。

---

## CLI 参考

全局选项：

```bash
aqm --version
aqm -v --help
aqm --project /path/to/project <command>
```

| 命令 | 说明 |
|---|---|
| `aqm init [--path DIR]` | 初始化 `.aqm/`。交互式三选一：默认模板 / 从注册表拉取 / AI 生成 |
| `aqm validate [PATH] [--pipeline NAME] [--strict]` | 按 JSON Schema 校验流水线 |
| `aqm agents [--pipeline NAME]` | 列出成员并打印交接图 |
| `aqm run INPUT [--agent ID] [-p k=v] [--priority ...] [--parallel] [--pipeline NAME]` | 运行流水线 |
| `aqm fix TASK_ID INPUT [--agent ID] [-p k=v]` | 基于已有任务追加需求，继承上下文 |
| `aqm status [TASK_ID]` | 查询任务状态 |
| `aqm list [--filter STATUS]` | 列出任务；状态可取 `pending / in_progress / awaiting_gate / awaiting_human_input / approved / rejected / completed / failed / cancelled / stalled` |
| `aqm context TASK_ID` | 打印该任务的 `context.md` |
| `aqm priority TASK_ID {critical\|high\|normal\|low}` | 调整优先级 |
| `aqm approve TASK_ID [-r REASON]` | 通过人工门 |
| `aqm reject TASK_ID -r REASON` | 拒绝人工门（原因必填） |
| `aqm human-input TASK_ID RESPONSE` | 回答 Agent 的人工输入请求 |
| `aqm cancel TASK_ID [-r REASON]` | 取消任务 |
| `aqm restart TASK_ID [--from-stage N] [-P NAME] [-p k=v]` | 从指定阶段重启（`failed / completed / stalled / cancelled`） |
| `aqm chunks ...` | 工作块管理，见上文 |
| `aqm meeting ...` | 运行会议 |
| `aqm serve [--port N] [--host H]` | 启动 Web 控制台 |
| `aqm pipeline create/default/delete/edit/list/versions` | 多条流水线管理 |
| `aqm search [QUERY] [--repo R] [--offline]` | 搜索注册表 |
| `aqm pull NAME[@version] [--repo R] [--offline]` | 拉取流水线到 `.aqm/pipelines/` |
| `aqm publish [--name N] [--description D] [--version V] [--local]` | 发布流水线到注册表 |

常用示例：

```bash
# 多流水线
aqm pipeline list
aqm pipeline create code-review --template
aqm pipeline default code-review
aqm validate --pipeline code-review --strict

# 从注册表拿一条现成的
aqm search code-review
aqm pull code-review@1.2.0

# 从失败阶段重启
aqm restart T-A3F2B1 --from-stage 3
```

---

## 流水线 YAML 参考

文件位置：`.aqm/pipelines/<name>.yaml`。旧版单文件 `.aqm/agents.yaml` 仍可作为 `default` 的回退。

```yaml
apiVersion: aqm/v0.1
entry_point: first        # first | auto（auto 用 LLM 选入口）

# 参数是「名称 -> 定义」的映射；也可以直接写默认值
params:
  target_dir:
    type: string
    description: "要修改的目录"
    required: true
  dry_run: false

# 从外部 YAML 引入可复用成员
imports:
  - from: ./shared/common.yaml
    agents: [reviewer]

agents:
  - id: planner
    name: 规划
    type: agent           # agent | session
    runtime: claude
    model: claude-sonnet-4-6
    system_prompt: |
      分析需求并给出实施计划：
      {{ input }}
    context_strategy: both
    context_window: 3
    cli_flags: ["--verbose"]
    mcp:
      - server: github
    handoffs:
      - to: implementer
```

### Agent 字段

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `id` | `string` | — | 唯一标识，交接目标用它引用 |
| `name` | `string` | `id` | 显示名 |
| `type` | `agent` \| `session` | `agent` | 会话节点需配 `participants` |
| `runtime` | `claude` \| `gemini` \| `codex` \| `opencode` \| `kilo` \| `cline` | — | `type: agent` 时必填 |
| `model` | `string` | CLI 默认 | 原样传给 `--model` |
| `system_prompt` | `string` | `""` | 支持 Jinja2，常用 `{{ input }}`、`{{ output }}` |
| `handoffs` | `list` | `[]` | 见下方交接格式 |
| `gate` | `object` | 无 | `type` / `prompt` / `model` / `max_retries` |
| `mcp` | `list` | `[]` | MCP 服务器；配置后 Claude 会走完整工具模式 |
| `cli_flags` | `list[string]` | `null` | 原样透传给 CLI |
| `context_strategy` | `none` \| `last_only` \| `own` \| `shared` \| `both` | `both` | 上下文裁剪策略 |
| `context_window` | `int` | `3` | 最近 N 个阶段保留全文，`0` = 全部 |
| `human_input` | `object` | 无 | `prompt` / `timeout` |
| `retry` | `object` | 无 | `max_retries` / `backoff` / `fallback_context_strategy` |
| `chunks` | `object` | 无 | 工作块配置（`enabled` / `initial` / `auto_split` / `enforce` / `retry_limit` / `owner_filter` / `auto_claim` / `max_chunks`） |
| `participants` | `list[string]` | `[]` | 仅 `type: session` |
| `turn_order` | `round_robin` \| `moderator` | `round_robin` | 仅 session |
| `max_rounds` | `int` | `10` | 仅 session |
| `consensus` | `object` | 无 | `method` / `keyword` / `require` / `require_chunks_done` |
| `summary_agent` | `string` | 无 | 仅 session，负责总结/宣布共识 |
| `abstract` | `bool` | `false` | 只作为 `extends` 模板，不参与执行 |
| `extends` | `string` | 无 | 继承另一个成员的字段（子覆盖父，浅合并） |

### 交接格式

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
| `task` | `string` | `""` | 交接标签，会显示在阶段名里 |
| `condition` | `string` | `always` | `always` / `on_approve` / `on_reject` / `on_pass` / `auto` / 表达式 |
| `payload` | `string` | `{{ output }}` | 传给目标成员的输入模板 |

`condition: auto` 时，成员在输出里写 `HANDOFF: agent_id`（可多行）自行决定去向；没写则回退到第一个交接。

### 质量门格式

```yaml
gate:
  type: llm
  prompt: "是否满足验收标准？"
  model: claude-sonnet-4-6
  max_retries: 3
```

```yaml
gate:
  type: human
```

### MCP 格式

```yaml
mcp:
  - server: github
  - server: filesystem
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/data"]
    env:
      LOG_LEVEL: info
```

配了 `mcp`（或 `cli_flags`）的 Claude 成员会以完整 Claude Code 模式运行，获得工具访问能力。

### 参数与导入

```yaml
params:
  target_dir:
    type: string
    description: "要修改的目录"
    required: true
    auto_detect: "分析项目结构，给出源码目录的相对路径"
  dry_run: false            # 简写：直接给默认值

imports:
  - from: ./shared/common.yaml
    agents: [reviewer]
```

在提示词里用 `${{ params.target_dir }}` 引用参数。取值优先级：命令行 `-p` > `.aqm/params.yaml` > YAML 默认值 > 交互式询问。

运行时覆盖：

```bash
aqm run "重构登录模块" -p target_dir=src/auth -p dry_run=true
```

### 复用与抽象

```yaml
agents:
  - id: base_reviewer
    abstract: true                  # 不参与执行，只作为模板
    runtime: claude
    system_prompt: "审查代码：{{ input }}"

  - id: security_reviewer
    extends: base_reviewer          # 浅合并：子字段覆盖父字段
    model: claude-opus-4-6
```

---

## HTTP API 与 SSE

所有接口由 `aqm serve` 提供，默认 `http://127.0.0.1:8000`。完整 OpenAPI 文档见 `/docs`。

### 页面

`GET /`、`/meeting`、`/tasks`、`/tasks/{id}`、`/agents`、`/pipelines`、`/registry`、`/validate`

### 任务

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/run` | 启动流水线（`description`、`agent_id`、`params`、`priority`、`pipeline`） |
| `POST` | `/api/fix` | 基于父任务追加需求 |
| `GET` | `/api/tasks` | 列表，可按 `status` 过滤、`limit` 限制 |
| `GET` | `/api/tasks/{id}` | 详情 |
| `POST` | `/api/tasks/{id}/approve` | 通过人工门 |
| `POST` | `/api/tasks/{id}/reject` | 拒绝（可带目标成员） |
| `POST` | `/api/tasks/{id}/human-input` | 回答人工输入 |
| `POST` | `/api/tasks/{id}/interject` | 运行中插话 |
| `POST` | `/api/tasks/{id}/pause` | 暂停 |
| `POST` | `/api/tasks/{id}/unpause` | 继续 |
| `POST` | `/api/tasks/{id}/cancel` | 停止 |
| `POST` | `/api/tasks/{id}/restart` | 重启（`from_stage`、`note`） |
| `POST` | `/api/tasks/{id}/priority` | 改优先级 |
| `DELETE` | `/api/tasks/{id}` | 删除 |
| `GET` | `/api/tasks/{id}/gate` | 待审批事项 |
| `GET` | `/api/tasks/{id}/residents` | 常驻会话状态 |
| `GET` | `/api/tasks/{id}/files` | 产物文件 |
| `GET` | `/api/tasks/{id}/context` | 上下文内容 |
| `GET` | `/api/tasks/{id}/events` | SSE 实时流 |

### 工作块

| 方法 | 路径 |
|---|---|
| `GET` | `/api/tasks/{id}/chunks` |
| `GET` | `/api/tasks/{id}/chunks/summary` |
| `POST` | `/api/tasks/{id}/chunks` |
| `PATCH` | `/api/tasks/{id}/chunks/{chunk_id}` |
| `DELETE` | `/api/tasks/{id}/chunks/{chunk_id}` |
| `POST` | `/api/tasks/{id}/chunks/reorder` |
| `POST` | `/api/tasks/{id}/chunks/reset` |
| `DELETE` | `/api/tasks/{id}/chunks` |
| `POST` | `/api/tasks/{id}/chunks/split` |
| `POST` | `/api/tasks/{id}/chunks/{chunk_id}/start` |

### 会议

| 方法 | 路径 |
|---|---|
| `GET` / `POST` | `/api/meetings` |
| `GET` | `/api/meetings/default-agents` |
| `GET` | `/api/meetings/models?runtime=...` |
| `GET` / `PATCH` / `DELETE` | `/api/meetings/{id}` |
| `POST` | `/api/meetings/{id}/start` / `stop` / `continue` |
| `POST` | `/api/meetings/{id}/speak` |
| `GET` | `/api/meetings/{id}/file` |
| `GET` / `POST` / `PUT` | `/api/meetings/{id}/requirements` |
| `GET` | `/api/meetings/{id}/tasks` |
| `POST` | `/api/meetings/{id}/handoff` |
| `GET` | `/api/meetings/{id}/events` |

### 流水线 / 注册表 / 校验 / 项目

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` / `POST` | `/api/pipelines` | 列表 / 创建 |
| `GET` / `PUT` / `DELETE` | `/api/pipelines/{name}` | 读取 / 覆盖 / 删除 |
| `POST` | `/api/pipelines/{name}/duplicate` | 复制 |
| `GET` | `/api/pipelines/{name}/yaml` | 原始 YAML |
| `POST` | `/api/pipelines/default` | 设为默认 |
| `GET` / `POST` | `/api/pipelines/{name}/agents` | 成员列表 / 新增 |
| `PUT` / `DELETE` | `/api/pipelines/{name}/agents/{agent_id}` | 修改 / 删除 |
| `GET` | `/api/registry/search` | 搜索注册表 |
| `GET` | `/api/registry/{name}/versions` | 版本列表 |
| `POST` | `/api/registry/pull` / `publish` | 拉取 / 发布 |
| `POST` | `/api/validate` | 校验 YAML |
| `GET` / `POST` | `/api/project` / `/api/project/switch` | 当前项目 / 切换 |
| `POST` | `/api/project/init` | 初始化新项目 |
| `GET` / `POST` | `/api/project/restart-status` / `/api/project/restart` | 重启状态 / 重启服务 |
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/events` | 全局 SSE（`task_count_update` 等） |

### 任务 SSE 事件

`snapshot`（连上即推，用于还原当前画面）、`stage_start`、`stage_output`、`stage_thinking`、`stage_complete`、`stage_interrupted`、`tool_call`、`tool_action`、`tool_process`、`turn_complete`、`resident_starting`、`resident_ready`、`resident_failed`、`gate_waiting`、`human_input_waiting`、`interjection_queued`、`interjection_applied`、`chunk_update`、`chunk_warning`、`chunk_split_started`、`chunk_split_done`、`chunk_split_failed`、`pipeline_resuming`、`task_paused`、`task_resumed`、`task_cancelled`、`task_complete`、`task_failed`。

### 会议 SSE 事件

`meeting_started`、`agents_starting`、`agent_ready`、`agent_start`、`agent_stream`、`agent_complete`、`agent_error`、`human_turn`、`human_message`、`human_skipped`、`round_start`、`round_complete`、`meeting_complete`、`requirements_started`、`requirements_ready`、`requirements_error`。

---

## 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| `Error: No such command 'runtime'` | aqm 没有 `runtime` 子命令 | 用 `aqm --version`、`aqm agents`、`aqm validate` 验证 |
| Agent 完全没有输出 | 对应 CLI 未安装或未登录 | `claude --version` 等逐个检查；重新登录 |
| opencode 无输出 / 参数报错 | 装成了旧的 `opencode-ai` | 改装 `npm i -g @opencode/cli` |
| aqm 找不到 cline | 装成了 `@cline/cli`（`clite`） | 改装 `npm i -g cline` |
| `Daily usage limit exceeded` / `Free usage limit reached` | 模型额度用尽 | 换模型或换运行时；额度重置后再继续（任务可暂停等待） |
| `Resident session ... exited` | 常驻会话进程退出（超时/崩溃） | aqm 会自动重建并继续；连续失败时看错误里的退出码与命令 |
| 任务一直「等待审批」 | 任务停在人工门，等你决定 | 打开任务详情「实时输出」或「概览」，或 `GET /api/tasks/{id}/gate` 看具体事项 |
| 工作块出现多个「进行中」 | 旧版本残留 | 重启服务会自动清扫；也可重跑任务触发阶段开工释放 |
| 页面状态不跟着按钮变 | 浏览器缓存了旧脚本 | **Ctrl+F5** 强刷 |
| 服务重启后任务自己跑起来 | `server.auto_resume: true` | 设 `AQM_DISABLE_AUTO_RESUME=1`，或把 `auto_resume` 设为 `false` |
| 任务反复自动恢复失败 | 同一阶段 10 分钟内失败 3 次 | 任务会停在 `stalled` 并写明原因，需手动修 |
| Windows 控制台中文乱码 | 代码页不是 UTF-8 | `chcp 65001`，或设 `PYTHONIOENCODING=utf-8` |
| 端口被占用 | 已有 aqm 在跑 | `Get-NetTCPConnection -LocalPort 8000`（Windows）/ `lsof -i:8000` |

---

## 开发与测试

```bash
cd aqm
pip install -e ".[serve,dev]"

# 全量测试
pytest -q

# 只跑某块
pytest tests/test_web_api.py -q
pytest tests/test_chunks.py tests/test_pause.py -q
```

测试覆盖：流水线控制流、质量门、会话、工作块、暂停/恢复、自动恢复、插话、Web API 与页面、JS 语法、Markdown 渲染、注册表、重试、上下文策略、流式输出过滤。

说明：部分用例依赖本机环境（特定 CLI 版本、网络、`/tmp` 路径、英文页面标题），在 Windows 或离线环境下可能失败，与业务逻辑无关。

改代码时的约定：

- 内联 JS 写在 Python 普通字符串里时，`\n` 必须写成 `\\n`；`tests/test_web_js_syntax.py` 会校验。
- 写文件用 `newline="\n"` 或 `write_bytes`，避免 CRLF 混入（`pipeline.py` / `tasks.py` 目前是 CRLF）。
- 前端模板改动后浏览器需强刷。

---

## 目录结构

```text
aqm/
├── aqm/
│   ├── cli.py                 # Click 命令行
│   ├── meeting.py             # 会议引擎
│   ├── core/
│   │   ├── agent.py           # YAML 模型、会话/工作块/重试配置
│   │   ├── pipeline.py        # 编排主循环、暂停/插话/检查点
│   │   ├── chunks.py          # 工作块模型与指令协议
│   │   ├── chunk_planner.py   # 需求文档 → 工作块
│   │   ├── gate.py            # LLM / 人工质量门
│   │   ├── gate_review.py     # 审批事项提取
│   │   ├── context_file.py    # 上下文存储与裁剪
│   │   ├── config.py          # .aqm/config.yaml
│   │   ├── validate.py        # Schema 校验
│   │   └── task.py            # 任务与状态
│   ├── runtime/
│   │   ├── claude.py / gemini.py / codex.py
│   │   ├── opencode.py / kilo.py / cline.py
│   │   ├── meeting_session.py # 常驻会话与 ACP 流式
│   │   ├── model_catalog.py   # 各运行时可选模型
│   │   └── stream_guard.py    # 思考复读防护
│   ├── queue/                 # file / sqlite 队列
│   └── web/
│       ├── app.py             # FastAPI 入口
│       ├── templates.py       # 布局、CSS、徽章
│       ├── api/               # tasks / meeting / pipelines / registry / validate / sse
│       └── pages/             # 概览 / 任务 / 详情 / 会议 / 代理 / 流水线 / 注册表 / 校验
├── schema/agents-schema.json  # 流水线 JSON Schema
├── docs/                      # concepts.md / spec.md
└── tests/                     # pytest
```

---

## 许可

MIT。见 [LICENSE](LICENSE)。

贡献指南见 [CONTRIBUTING.md](CONTRIBUTING.md)。
