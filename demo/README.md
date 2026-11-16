# 演示数据（demo）

这里是一次**真实运行**留下的样本，用来展示 aqm 的「会议 → 需求文档 → 任务 → 工作块」闭环，不包含任何本地测试数据。

## 内容

| 路径 | 说明 |
|---|---|
| `meeting/meta.json` | 会议状态、四个成员的配置与发言（`agents[].output` / `thinking` / `actions`） |
| `meeting/meeting.md` | 圆桌逐字稿（共享文件，主持人+成员按轮次追加） |
| `meeting/REQUIREMENTS.md` | 会议结束后自动整理出的需求文档 |
| `task/task.json` | 由该需求文档派生的任务快照：4 个阶段的输入/输出、耗时、元数据 |
| `task/chunks.json` | 任务自动拆分出的 6 个工作块（含状态、负责人、阶段） |
| `pipeline.yaml` | 演示用的四阶段流水线（架构 → 实现 → 界面 → 审核） |

## 会议概况

- 主题：**为 AQM 设计「任务导出」功能**
- 轮次：1 轮，四个成员各发言一次
- 成员：

| 成员 | 运行时 | 角色 |
|---|---|---|
| Claude | `claude` | 架构师 |
| opencode | `opencode` | 代码实现 |
| cline | `cline` | UI 设计 |
| kilo | `kilo` | 审核 |

会议要求四人分别从架构、实现、界面、风险四个角度给出方案要点，会议结束后自动生成 `REQUIREMENTS.md`。

## 任务概况

- 任务状态：`completed`
- 阶段：4 个（claude → opencode → cline → kilo）
- 工作块：6 个，全部完成
- 流水线配置：`pipeline.yaml`（与仓库里的 `.aqm/pipelines/` 同格式）

## 如何回放

把会议目录复制到你自己的项目：

```bash
mkdir -p .aqm/meetings/dd7e208a
cp -r demo/meeting/* .aqm/meetings/dd7e208a/
aqm serve            # 打开「会议」页面即可看到这条记录
```

流水线可以直接校验：

```bash
mkdir -p .aqm/pipelines
cp demo/pipeline.yaml .aqm/pipelines/demo-roundtable.yaml
aqm validate --pipeline demo-roundtable
```

任务快照（`task/task.json`、`task/chunks.json`）用于对照阅读，不需要导入。

## 脱敏说明

导出时所有绝对路径都已替换为 `<demo-project>` 或 `<path>`，不包含 API Key、Cookie 或任何本机凭据。
