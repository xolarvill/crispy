# 指令白板：Figma + Comfy 的混合模型

日期：2026-11-15。状态：P0 文档与服务、P1 画布、P2 的进程内工具子集均已实现（2026-11-15）；外部 MCP `boardwright` server 仍待做。上一版把白板当成「编译成 prompt 的画布」，这一版修正为：**白板是一份服务端权威、可被人和 agent 通过工具读写编辑的文档**；prompt 只是它的一个消费者。

## 0. 核心判断

Figma 和 Comfy 各自贡献了一半：

- **Figma 侧**：稳定的文档模型（scene graph、节点 ID、层级、属性、revision），人和 agent 都能读、能改、能看到彼此改了什么。
- **Comfy 侧**：一部分节点是可执行的，边表示数据流，执行前能校验、执行后能把结果写回画布。

合起来才有价值：白板不是「给 agent 传话的输入框」，而是**人和 agent 共同工作的表面**。Agent 生成的 4 个方案直接摆在画布上，人在旁边圈一处说「这个方向对，背景换掉」——下一轮从这份带批注的版本继续。这比在 trace 日志里读文字强得多。

## 1. 三层模型，别混在一起

| 层 | 职责 | 谁读谁写 |
| --- | --- | --- |
| **文档层**（Figma） | 节点、层级、位置、属性、revision、操作日志 | 人通过 UI；agent 通过工具 |
| **图执行层**（Comfy） | 带类型端口和执行语义的子图 | 执行器校验；人/agent 绑定参数 |
| **执行层** | crispy 现有的 stage / pipeline / worker | 现有代码，不改语义 |

**一条硬规则：标注边 ≠ 数据流边。**

人在图上画的箭头，绝大多数意思是「看这个」「这两个融合」「不要用那个」——是给模型看的语义，不是依赖关系。如果引擎把这条箭头当成依赖去调度，整个东西立刻崩掉。所以边必须分类型：

```ts
type BoardEdge =
  | { kind: "annotation"; from: string; to: string; label?: string }   // 人话，只给指令编译器读
  | { kind: "data"; from: string; to: string; port: string; type: "image" | "text" | "video" } // 可执行，校验类型
  | { kind: "contain"; parent: string; child: string };               // 层级
```

界面上可以用不同颜色区分（标注边虚线、数据流边实线带端口），但从数据模型开始就必须分开。

## 2. 文档模型（服务端权威）

白板不是前端本地文档，否则 agent 的工具调用没有共同的真值。后端持有：

```
board                 # 一块板：归属 run / workspace，标题，当前 revision
board_revision        # 追加式快照：revision, document JSON, created_at
board_event           # 操作日志：actor, op, payload, revision, at
```

- 写入用 `expected_revision` 做乐观并发，冲突返回 409。
- 完整快照按 revision 存（这个量级 < 1MB，比逐步回放省事）；`board_event` 只用于归因、审计和「撤销 agent 的某一次操作」。
- 快照图（PNG）是**派生品**，不是真值；按 revision 缓存到 `<media_root>/boards/<run>/r<rev>.png`。

### 节点

```json
{
  "id": "n_7f3a",
  "type": "asset",
  "parent": "f_main",
  "x": 120, "y": 80, "w": 320, "h": 420,
  "locked": false,
  "created_by": "human",
  "props": {
    "assetId": "va_...",
    "uri": "media/...",
    "role": "source",
    "mark": 1,
    "caption": "实拍主图"
  }
}
```

v1 节点类型：

| type | 用途 | 可执行 |
| --- | --- | --- |
| `frame` | 容器/分组，对应 Figma frame；agent 的产物默认写进独立 frame | 否 |
| `asset` | 图片/视频卡片，角色 source / reference / style / output | 输出端口 |
| `note` | 文字说明 | 否 |
| `region` | 锚定在某个 asset 上的区域，0–1 坐标 + 一句说明 | 否 |
| `ink` | 手绘笔迹 | 否 |
| `stage` | 绑定一个 crispy 阶段，有输入端口 | 是 |
| `generator` | 单独一次模型调用（P3 再评估） | 是 |

给每个节点一个**编号徽标**（`mark`），渲染在卡片左上角。这是编译指令时唯一的指代符号，也让多模态模型能把「①」和像素对上。

### 归因与安全

- `created_by: "human" | "agent:<name>"`，agente 写入必须带来源。
- `locked` 的节点，agent 默认不能移动/删除；强行操作需要显式 `force`，并且会进审计。
- Agent 的写操作默认只能落在它自己的 frame 里（例如 `Agent output`），碰不到人的工作区，除非人明确授权。
- 每次工具调用 = 一次 `board_event` + 一个新 revision。UI 能按 agent 回滚单次操作。

## 3. Agent 工具面（暂名 `boardwright`）

这是 Figma 侧的真正接口。命名刻意对齐 figwright，agent 的提示词和习惯可以直接迁移。

**读**

| 工具 | 作用 |
| --- | --- |
| `ping` | 连接与版本 |
| `get_board` | 整块板的节点树（可按 frame 过滤） |
| `get_selection` | 人当前选中的节点 |
| `get_node` / `get_nodes_info` | 单个/批量节点详情 |
| `scan_nodes_by_type` | 按类型扫（asset / region / stage） |
| `search_nodes` | 按文本搜索 note / caption |
| `get_edges` | 边，可按 kind 过滤 |
| `get_viewport` | 视口，用于理解「人在看哪里」 |
| `get_screenshot` | 渲染快照（验证用，不是测量用） |
| `get_annotations` | 只取标注类信息：编号、区域、箭头、说明 |

**写**

| 工具 | 作用 |
| --- | --- |
| `create_frame` / `create_text` / `create_note` | 基础节点 |
| `create_asset_node` | 把 run 素材或生成结果钉到画布，自动编号 |
| `create_region` | 在某个 asset 上建区域 + 说明 |
| `create_edge` | 建边，必须显式声明 kind |
| `set_node_props` | 改属性（caption、role、参数） |
| `move_nodes` / `resize_nodes` / `group_nodes` / `reparent_nodes` | 布局 |
| `lock_nodes` / `unlock_nodes` | 保护人的工作 |
| `delete_nodes` | 默认只删 agent 自己创建的 |
| `set_selection` | 把人的注意力引到某处（「看这个」） |

**执行**

| 工具 | 作用 |
| --- | --- |
| `bind_run` | 把 stage 节点绑定到现有 PipelineRun |
| `validate_graph` | 端口/类型/必填输入校验，返回结构化问题 |
| `run_stage` | 触发已绑定阶段的重跑 |
| `get_run_status` | 任务状态 |
| `place_assets` | 把生成结果写回画布（**agent 展示工作成果的主要方式**） |
| `batch` | 一次调用多步操作，减少往返 |

关键点：`place_assets` 比任何文字汇报都重要。agent 应该把候选图摆成 2×2 或一行，而不是在聊天里贴链接。

### 已落地（2026-11-15，进程内 + HTTP）

实际实现的是上表的一个受控子集，完整参数见 [docs/agent-board-tools.md](../agent-board-tools.md)。

- 读：`get_board`、`get_annotations`、`search_nodes`、`list_assets`、`get_screenshot`、`get_run_status`、`batch`。
- 写：`create_note`、`create_region`、`create_edge`（仅 annotation）、`place_assets`、`create_asset_node`、`set_node_props`、`move_nodes`、`delete_nodes`（仅 agent 自建节点）、`set_goal`（仅空 goal）、`create_board`、`compose`（PIL 拼图，可回传图片）。
- 每次 pass 只产出一个 revision；节点 id 由 `(stage_task_id, actor, sequence)` 决定，冲突时读取新 scene 重放，最多 3 次。
- 仍未实现：`get_selection`、`get_viewport`、锁定/解锁、resize/group/reparent、data 边、`bind_run`/`validate_graph`/`run_stage`，以及独立 MCP server。

## 4. 两个消费者，一个服务层

现有运行时是进程内的 Python，不是 MCP 客户端；README 也明确 Magpie / MCP gateway **尚未上线**（`README.md:48`）。所以必须承认两条消费路径：

```
app/services/boards.py        # 唯一真值：节点校验、revision、编译、执行绑定
   ├── in-process: compile_board() → digest + snapshot     # 现有 stage runtime 用
   ├── HTTP: /runs/{id}/board/*                            # 前端用
   ├── HTTP: POST /runs/{id}/board-docs/{board_id}/tools    # 外部 harness 单次工具调用
   └── MCP: boardwright server                             # 外部 agent 用（P2，待做）
```

- **进程内阶段**（planning、copy_image、storyboard）：拿 `digest` 文本 + 快照引用就够，不需要 MCP。这条路径 P0 就能通。
- **外部 agent**（Magpie 或人工驱动的编码 agent）：走 `boardwright`，能读能改。
- 两者调用同一套 service，避免「UI 改的」和「agent 改的」出现两套语义。

**不要只做工具不做编译**：如果只上 MCP，现有阶段运行时读不到白板，白板对主流程就是空的。

## 5. 画布底层选型（修正）

上一版推荐 Excalidraw。既然文档真值必须是我们的 scene graph、并且要支持端口和层级，Excalidraw 的元素模型（绘图优先、无层级、无类型化属性）会变成负担。修正如下：

| | React Flow `@xyflow/react` | tldraw | Excalidraw | Konva 自建 |
| --- | --- | --- | --- | --- |
| 许可 | 12.12.1 / MIT | 5.6.0 / source-available，生产需 key | 0.18.1 / MIT | 10.9.1 / MIT |
| 文档模型 | 节点 JSON，稳定 ID，支持父节点/子流程 | 自定义 shape 最灵活 | 绘图元素，无层级/端口 | 全部自建 |
| DOM 节点（图片、输入框、参数表单） | 原生 React 组件 | 支持 | 有限 | 自建 |
| 手绘/任意箭头 | 需自建 ink 层 | 内置 | 内置 | 自建 |
| 撤销、绑定、选择 | 部分自建 | 内置 | 内置 | 自建 |
| 结论 | 服务端编辑最简单 | **人这一侧最好用**，需授权评估 | 降级为纯批注层备选 | 回退方案 |

### 使用上的差别（实现之后每天都能感觉到）

| 操作 | tldraw | React Flow |
| --- | --- | --- |
| 圈一块说「这里改」 | 手绘圈、随手画，压感支持 | 只能拉矩形节点，没有笔 |
| 从卡片拉箭头 | 箭头自动吸附，移动卡片时跟着走，可加标签 | 只能从 handle 拉；要「任意位置」得加隐形 handle |
| 写一句话 | 双击任意处就地输入，便签/文字 | 文字是节点，先创建再编辑 |
| 指给人看 | 激光笔、缩放定位 | 无 |
| 框选 / 分组 / 层级 | 原生 frame、分组、多选 | 有 GroupNode 和父节点，但偏流程编排 |
| 触控板 / 触控笔 | 原生、手感好 | 鼠标优先 |
| 整体观感 | 白板 / FigJam | 节点编辑器（n8n、Comfy） |
| 端口、连线类型校验、minimap | 要自建 | 原生 |
| 撤销 / 重做 | 内置、可靠 | 自建 |

对应到三类指令：**指认**两者都行；**定位**（圈区域、手写）tldraw 明显更好；**关系**（拉箭头 + 写一句话）tldraw 更自然，React Flow 只有在「箭头就是类型化数据流」时才更好。如果白板首先是给人用的，tldraw 更合适。

### 一个容易被忽略的差别：文档权威性

- React Flow 的 nodes/edges 就是纯 JSON，**服务端可以轻松增删改**——MCP 工具直接改文档没有障碍。
- tldraw 的文档语义活在**客户端 editor** 里。服务端要改文档，官方没有 headless editor，只能二选一：
  1. 保留一个常开的客户端作代理（依赖标签页），或
  2. 自己维护一份中立文档，tldraw 只当编辑器（加映射层）。

这不是否决 tldraw，而是决定架构：**BoardScene 必须是唯一的真值，tldraw 的 store 只当视图。** 人编辑后映射回 BoardScene；agent 从服务端改 BoardScene，客户端收到新 revision 后把差异应用回 editor（tldraw 有 `getDiff`/`applyDiff`，agent 一次操作可用 `markHistoryStoppingPoint` 包成一步，人可整体撤销）。这层映射大约 3–5 天。

如果把 tldraw store 直接当真值，P1 会更快，但 P2 的 agent 服务端编辑会非常难受，而且数据被库锁死。

### 结论

如果确认白板主要是**人标注、agent 读取与摆放结果**，选 **tldraw + 中立 BoardScene**：

- 素材用 tldraw **原生 image shape**，不要用 DOM 卡片——`editor.toImage()` 是 canvas 渲染，原生图形导出可靠，DOM shape 导出容易出坑。
- 数据流边用 arrow shape 的 `meta.kind = "data"`，标注边不给 `meta`；靠颜色和线型区分，模型层仍按 kind 校验。
- `isLocked` 只管客户端，agent 端仍要服务端校验。
- 已安装的 React 19.3.1 满足 tldraw 5.6 的 `^19.2.1`，版本兼容没问题。

只有一种情况选 React Flow：确认「服务端 agent 编辑」的优先级高于人的画布手感，或者完全不能接受授权成本。

## 6. 执行语义：v1 只做绑定，不做新引擎

「Comfy 式」容易滑向一个错误的期待：以为画个图就能跑任意流程。crispy 的执行是**固定的 stage 序列**，没有「节点 = 一个可调用执行器 + 类型化输入输出」的运行时。

所以 v1 的 `stage` 节点是**绑定**，不是新建执行：

- 节点绑定已有 `StageTask`，边表示输入，`validate_graph` 检查必填输入是否连上。
- `run_stage` 调用现有的重跑服务，白板的指令作为 `instruction_board` 注入该阶段。
- 画布是执行的可视化与控制面，不是新的调度器。

真正的「任意 DAG 编排」需要节点注册表、拓扑调度、缓存、失败重试、部分重算——那是一个独立子系统，不是画布功能。要做就单独立项，别和白板捆在一起。

## 7. 编译与注入（进程内路径）

`compile_board(board, revision) -> BoardBundle`：

```json
{
  "revision": 7,
  "digest": "[INSTRUCTION BOARD r7]\nGoal: ...\n① A — source, 实拍主图\n② B — reference\n① -[annotation]-> ② : 保留模特，只取背景\nRegions:\n  ① box(0.12,0.30,0.28,0.22) — 鞋带换黑色\nFree notes:\n  - 主标题放左上留白",
  "assets": [{"node_id": "a1", "mark": 1, "asset_id": "va_1", "uri": "...", "role": "source", "box": [0, 0, 320, 420]}],
  "regions": [{"node_id": "r1", "asset_id": "a1", "asset_mark": 1, "box": [0.12, 0.30, 0.28, 0.22], "note": "鞋带换黑色"}],
  "links": [{"id": "e1", "kind": "annotation", "from": "a1", "to": "a2", "label": "保留模特，只取背景"}],
  "stage_nodes": [],
  "snapshot_uri": "assets/boards/<run>/r7.png"
}
```

注入 `build_task_input()`，只给 `planning`、`copy_image_generation`、`video_scripting`、`storyboard_image_generation`。任务被领取执行时固定 `revision`，之后改板不影响已执行任务。

`digest` 硬限制约 2000 字符；参考素材 ≤ 6；快照 ≤ 2MP。超限先截断自由文本，不丢区域批注（区域信息量最高）。

### P0 已落地（2026-11-15）

- 表：`instruction_board` / `instruction_board_revision` / `instruction_board_event`（`app/data/models.py`）。
- 契约：`app/schemas/boards.py`。严格校验 ID 唯一、`mark` 唯一、region 必须挂在 asset 上、link 两端存在、data 边必须有端口、annotation 边不得声明端口。
- 服务：`app/services/boards.py`。`save_board()` 用 `expected_revision` 做乐观并发；`compile_scene()` 生成 digest 与结构化引用；`board_context_for_run()` 是运行时唯一读取口。
- API：`GET /runs/{id}/board`、`GET /runs/{id}/boards`、`GET /runs/{id}/boards/{revision}`、`PUT /runs/{id}/board`、`POST /runs/{id}/board/compile`（只编译不保存，给前端做「Agent 将看到」预览）。
- 运行时：`_operator_instruction_block()` 把 digest 包成高优先级指令块；写入 planning / copy_image / marketplace image / video scripting / storyboard 的文本与图像 prompt。
- 追踪：阶段执行时写 `instruction_board_applied` 事件，带 revision、素材/区域/箭头数量与快照路径。
- 测试：`tests/test_instruction_boards.py`（保存/读取/版本冲突/非法场景/注入到 planning prompt）。

### P1 画布 + P2 进程内工具（2026-11-15）

- 画布：tldraw 视图 ↔ `BoardScene` 映射、多 tab、快照、编译预览、agent 来源样式与 5 秒远端刷新（有未保存修改时只提示冲突，不覆盖）。
- 阶段工具：`run_board_tool_pass()` 在阶段产物落库后运行，最多 3 轮、一次 revision；每个调用写 `tool:<name>` 事件，整轮写 `agent_edit`。`board_questions` 会暂停非 `full_auto` 的自动推进。
- 注入隔离：digest 只编译人的指令；agent 的 note/region/link 进入独立的 `agent_annotations`，agent 钉上的素材仍列出并标记 `(agent output)`，避免 agent 把自己的话当成人指令再读一遍。
- 外部入口：`POST /runs/{run_id}/board-docs/{board_id}/tools`，与阶段 pass 使用同一 `BoardToolSession`。
- 测试：`tests/test_board_tools.py`、`tests/test_board_tool_protocol.py`、`tests/test_board_tool_pass.py`、`tests/test_instruction_boards.py`。
- 已知取舍：pass 复用阶段 DB session（需要看到本阶段刚写的 `VariantAsset`）；VQA / evaluation / video_generation 只拿到工具 pass，其主 prompt 仍未消费 digest。详见 [docs/agent-board-tools.md](../agent-board-tools.md)。

## 8. 分期

| 阶段 | 内容 | 验收 | 估时 |
| --- | --- | --- | --- |
| **P0 文档与服务** ✅ | 表、节点校验、revision/event、`compile_board()`、注入、测试 | 手写一份 document JSON，planning 的 trace 里能看到 digest 和快照引用 | 已完成 2026-11-15 |
| **P1 画布** ✅ | tldraw 编辑器、素材钉入、note/region、标注边、保存、编译预览 | 人能在板上完成「指认 + 定位 + 关系」三类指令并重跑生效 | 2 周（已完成 2026-11-15） |
| **P2 Agent 工具** | `boardwright` MCP server（读/写）、`place_assets`、归因/锁定/撤销 | 外部 agent 能读出白板、把 4 张候选图摆上去、人再批注 | 1 周；进程内子集已完成 2026-11-15，MCP server 待做 |
| **P3 执行绑定** | `stage` 节点、`validate_graph`、`run_stage`、结果写回 | 画布上改输入 → 校验 → 重跑 → 产物落回画布 | 1–2 周 |
| **P4 任意 DAG（可选）** | 节点运行时、拓扑调度、缓存 | 单独立项，不作为画布的一部分 | 另议 |

P1 和 P2 的进程内子集已完成。下一步是外部 MCP `boardwright` server（复用 `POST .../tools` 与 `BoardToolSession`），以及 P3 的阶段节点执行。

## 9. 风险与待定

- **MCP gateway 未上线**：P2 需要先起一个独立的 MCP server 进程调用 FastAPI。这是真实成本，不要按「已有 Magpie」估。
- **两条消费路径的语义漂移**：digest 和工具必须从同一个 service 生成，否则 agent 看到的和模型看到的会不一致。
- **标注/数据流混淆**：模型层强制分 kind，UI 上也别用同一种箭头。
- **破坏性编辑**：agent 删/移人的节点是最大信任风险。默认锁定 + 独立 frame + 单次可撤销是底线。当前实现用「agent 只能改自己本轮创建的节点」+ 单次 revision 兜底，尚未做显式锁定。
- **写入来源与反馈回路**：agent 的 note/region 不能进入 digest 的「人指令」部分，否则会把自作主张当成人的要求反复强化。当前实现把 agent 标注拆到 `agent_annotations`。
- **阶段 pass 的 session 复用**：`run_board_tool_pass()` 复用阶段任务的 DB session，才能看到本阶段尚未提交的 `VariantAsset`。worker 单阶段串行 + SQLite 下成立；换 PostgreSQL 或并发 worker 时要重新评估。
- **工具面与注入面不完全重合**：VQA / evaluation / video_generation 拿到了工具 pass，但主 prompt 还不会读 digest；扩 `BOARD_STAGE_SCOPE_STAGES` 前要先确认这些 runtime 的 prompt 消费点。
- **画布工作量**：tldraw 省掉手绘、绑定、撤销，但自定义 shape（素材卡、阶段卡、端口）和版本迁移仍要写；React Flow 反过来，流程语义现成、白板手感要补。
- **tldraw 的许可触发条件**：SDK 把「HTTPS + 非 localhost + `NODE_ENV=production`」视为生产环境。本地 localhost 开发不触发；一旦部署到域名（**包括内部工具**）就需要 key，hobby 仅限非商用且带水印。第三方与 HN 报告约 $6k/年。这不是技术风险，是产品部署方式的决策：纯本地自用可以先跑开发模式，但别把「永久免费」写进假设。
- **双权威风险**（选 tldraw 时）：BoardScene 是唯一真值，tldraw store 只是视图。任何「顺手在 store 里改一下」的代码都会造成两份数据漂移，必须在 review 里当成硬约束。
- **待定**：白板挂在 run 上，还是也允许 workspace 级长期板？建议 v1 只挂 run。
- **待定**：现有 `BrandCreative` 的模板能否作为「frame 模板」导入白板？可以后续再做，别在 P0 处理。

## 10. 参考

- Excalidraw 编程式元素 API：https://plus.excalidraw.com/docs/sdk/excalidraw/api/excalidraw-element-skeleton
- React Flow（xyflow）：https://github.com/xyflow/xyflow
- tldraw 许可：https://tldraw.dev/community/license
- ComfyUI LiteGraph fork：https://github.com/Comfy-Org/litegraph.js
- 项目内 Magpie/MCP 方向：`README.md`
