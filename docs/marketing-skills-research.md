# Marketing workbench：开源 skills 选型与融合建议

调研日期：2026-11-01。状态：选型提案；未安装上游 skills、未接通发送/发布，也未改变现有运行时。

目标：与 agent 协作的 marketing all-in-one workbench，首批聚焦社媒和 EDM，再扩展广告、转化、社区与营销分析。当前仓库未找到 SEO workbench 的聚合实现，因此本提案按“筛选上游能力，再融入统一工作流”的意图设计，不假定两者已经有共享协议。

## 推荐组合

采用 **Anthropic Marketing 的活动与审阅框架 + Corey Haines 的营销方法 + BlackTwist 的社媒细分能力 + Resend 的邮件工程能力**。平台连接单独选型，Composio 可作为候选；Agency Agents 用作平台角色参考。

用户围绕品牌、活动、内容、发布和结果协作。Skills 在任务背后按需加载，共享品牌和活动上下文。工作台负责草稿版本、人工修改、审批、排期、执行状态与复盘。

## 候选来源

Stars 是本次访问 GitHub 页面显示的约数，可能含缓存；它只说明关注度，不代表效果已经验证。整个仓库的 Stars 也不等于其中营销子目录的使用量。

| 来源 | 关注度或可信依据 | 适合吸收的能力 | 选型意见 |
| --- | --- | --- | --- |
| [anthropics/knowledge-work-plugins · marketing](https://github.com/anthropics/knowledge-work-plugins/tree/main/marketing) | Anthropic 官方；全仓约 34.2k；营销目录 Apache-2.0 | 活动简报、内容起草、品牌审阅、竞品简报、效果报告 | P0：工作流骨架；连接器占位和宿主约定需适配 |
| [coreyhaines31/marketingskills](https://github.com/coreyhaines31/marketingskills) | 约 57.7k；MIT | `social`、`emails`、`copywriting`、`ad-creative`、`analytics`、`attribution`、`ab-testing` | P0：主要营销知识来源；按能力选取，处理旧名称迁移 |
| [resend/resend-skills](https://github.com/resend/resend-skills) | 邮件服务商官方；约 208；MIT | `react-email`、`email-best-practices`、`resend`、`resend-cli` | P0：HTML 邮件、投递实践与发送工具；完整 EDM 仍需名单、分群与流程状态 |
| [blacktwist/social-media-skills](https://github.com/blacktwist/social-media-skills) | 社媒产品方维护；MIT；本次未记录 Stars | `content-calendar-sms`、`post-writer-sms`、`content-repurposer-sms`、`performance-analyzer-sms` | P0：社媒日历与复用；重心是文字平台，视觉平台主要补 caption；发布依赖 MCP |
| [msitarzewski/agency-agents](https://github.com/msitarzewski/agency-agents) | 约 165.8k；MIT | TikTok、Instagram、LinkedIn、Reddit、小红书、微信等营销角色 | P1：角色和平台 playbook 参考；以 persona 为主，须适配为可验收任务 |
| [alirezarezvani/claude-skills · marketing-skill](https://github.com/alirezarezvani/claude-skills/tree/main/marketing-skill) | 全仓约 29.1k；根目录标注 MIT | 需求获取、产品定位、社媒分析、活动分析及辅助脚本 | P1：分析补充；README 存在旧路径与弃用提示，入库前逐项核对 |
| [ComposioHQ/awesome-claude-skills](https://github.com/ComposioHQ/awesome-claude-skills) | 约 78.5k；本次未确认统一许可证 | 社媒及 Klaviyo、Mailchimp、Brevo 等服务的自动化入口 | 连接候选：依赖 Composio/Rube、服务账户和 API 权限；逐项确认许可，不整包内置 |
| [social-media-skills/skills](https://github.com/social-media-skills/skills) | 约 226；MIT；规模较小 | 更细的平台策略、短视频、发布与分析目录 | 观察：适合补缺；本轮以目录筛选为主，尚未逐个审阅 |

## 你提到的 commerce-agents

[anthropics/commerce-agents](https://github.com/anthropics/commerce-agents) 是官方购物助手与商家后台助手的参考蓝图，约 3.4k Stars，Apache-2.0。它与电商营销有交集，但主要处理购物与商家业务。README 明确说明它是 **不持续维护、不接受贡献的参考实现**。

对 crispy 最有价值的借鉴是：agent 定义、工具契约、人工审批与应用执行的边界。它的商家写操作先暂存、再由人确认；演示并未真正下单、扣款或修改在线商品。建议作为架构参考。直接匹配营销工作的官方来源是上表中的 Marketing 插件。

## 首批能力包：12 个可验收任务

下列名称是 crispy 的拟议任务名称；右列才是上游 skill 名称。一个任务只设一个默认来源，其他来源作为参考，避免多个相似 skill 同时争夺路由。

| 优先级 | 工作台任务 | 默认来源 | 预期交付物 |
| --- | --- | --- | --- |
| P0 | 活动策划 | Anthropic `campaign-plan` | 目标、受众、渠道、时间表、交付物与指标 |
| P0 | 品牌审阅 | Anthropic `brand-review` | 针对具体版本的修改建议与审阅结果 |
| P0 | 社媒内容批次 | Corey `social` | 按平台拆分的帖文与素材需求 |
| P0 | 内容日历 | BlackTwist `content-calendar-sms` | 可编辑的排期草稿 |
| P0 | 内容复用 | BlackTwist `content-repurposer-sms` | 同一原稿的多渠道衍生内容及来源关联 |
| P0 | 邮件序列 | Corey `emails` | 欢迎、培育、购后或召回邮件及触发条件草稿 |
| P0 | 邮件模板 | Resend `react-email` | 可预览 HTML、纯文本版本和变量定义 |
| P0 | 邮件质量检查 | Resend `email-best-practices` | 模板、链接、投递前置条件的检查结果 |
| P0 | 活动复盘 | Anthropic `performance-report` | 带数据来源和观察窗口的报告 |
| P1 | 广告素材变体 | Corey `ad-creative` | 按渠道拆分的标题、正文、素材简报 |
| P1 | 营销实验设计 | Corey `ab-testing` | 假设、实验对象、指标与停止条件 |
| P1 | 社媒表现分析 | BlackTwist `performance-analyzer-sms` | 帖文/账号对比和下一批内容建议 |

已抽查 [campaign-plan](https://raw.githubusercontent.com/anthropics/knowledge-work-plugins/main/marketing/skills/campaign-plan/SKILL.md)、[social](https://raw.githubusercontent.com/coreyhaines31/marketingskills/main/skills/social/SKILL.md)、[emails](https://raw.githubusercontent.com/coreyhaines31/marketingskills/main/skills/emails/SKILL.md) 与 [content-calendar-sms](https://raw.githubusercontent.com/blacktwist/social-media-skills/main/skills/content-calendar-sms/SKILL.md) 的正文。其余条目核实到官方目录/README；尚未完成全部脚本、依赖和实际输出评测。

Corey 当前使用 `social`、`emails` 等名称；搜索结果仍常出现 `social-content`、`email-sequence`。后续应锁定上游 commit 与路径，旧名只用于迁移映射。

## 如何融合进 crispy

以下是产品与工程建议，尚未实现。

1. **共享上下文先行。** 每个 workspace 保存品牌语气、产品、受众、证据、禁用表述和渠道偏好；每个 campaign 保存目标、时间、预算与指标。将这些字段组装成上游期望的上下文，减少每个 skill 重新访谈。
2. **以活动贯穿工作。** 活动简报连接社媒帖文、视频、邮件和落地页任务；任务里可与 agent 讨论、修改并保留版本。沿用现有任务、素材、发布、数据和记忆区域，导航是否新增由实际工作流决定。
3. **Skills 输出结构化草稿。** 明确输入与交付物类型，让表格、日历、邮件预览和审阅页可直接使用；记录上游来源、版本和执行事件。`SKILL.md`、persona、工具服务分别适配，不把它们视为同一格式。
4. **执行由连接器完成。** 草稿进入发布计划后，再交给邮件或社媒服务。UI 分别显示待审批、已排期、执行中、服务已接受和失败；发送 API 接受不等于送达，日历有条目也不等于已经发布。
5. **审批绑定内容版本。** 人修改正文、受众或发送时间后重新检查审批有效性。发送记录使用幂等标识，异步回执关联到具体内容，避免重试造成重复发送。
6. **结果回到活动。** 保留 campaign/content ID、UTM、渠道回执和观察窗口。复盘结论进入可审阅记忆，区分来源事实、推断与建议。

### 社媒 MVP

路径：活动简报 → 一周内容批次 → 人工编辑/品牌审阅 → 排期 → 发布或导出 → 结果回收。

先让一个目标平台完成闭环，再增加平台。第一版同时支持文案和素材包导出，连接状态明确可见。候选 skills 覆盖多个平台，并不证明 crispy 已拥有对应平台的发布权限。

### EDM MVP

路径：活动与受众 → 邮件序列 → 模板预览 → 测试邮件 → 审批/排期 → 发送 → 退订、退信和转化结果。

需要补充联系人/受众分群、订阅状态、抑制名单、模板版本、发送批次及事件记录。名单来源和可发送状态进入数据契约。外部 EDM 平台可负责维护名单；crispy 保存引用和任务所需快照，避免第一版复制完整 CRM。

电商品牌可优先验证 Klaviyo 连接，通用 newsletter 可评估 Mailchimp/Brevo，自建邮件路径可评估 Resend。这是待验证的产品选型建议，尚未比较账户成本、地区可用性与 API 覆盖。Cold outreach 应作为独立用例，当前 EDM 首批聚焦已有订阅/客户关系。

### 与现有代码的衔接

现有 `Campaign`、`VariantAsset`、`ContentSchedule`、`AgentTraceEvent`、`GmMemory` 可作为活动关联、创意素材、排期、执行记录和学习的基础。需逐项评估 schema，不能假定其当前字段已支持跨渠道内容或邮件流程。

建议增加的基础能力是 skill 来源注册与版本锁定、统一上下文组装和输出校验。邮件任务不应为了复用现有机制而被强行转换成图像/视频 pipeline。Magpie/MCP 按 README 既定方向接入；本轮未实现 gateway 或插件。

## 实施顺序与验收

| 层次 | 必要工作 | 验收依据 |
| --- | --- | --- |
| 1. 来源与契约 | 选定首批 skills，逐个审阅正文、脚本和引用；锁定 commit；保留许可与归属；定义输入输出 | 相同品牌上下文可生成可编辑的活动、帖文和邮件草稿；每项可追溯来源 |
| 2. 协作与审核 | 品牌/活动上下文、内容版本、人工编辑、预览、版本对应的审阅 | 人能从简报一路修改并审阅完整交付物 |
| 3. 社媒闭环 | 一个渠道的授权、排期、发布、状态回收与导出 | 实际发布回执可追踪，失败可恢复且不重复发布 |
| 4. EDM 闭环 | 一个邮件服务、受众引用、订阅/抑制状态、测试发送和批次事件 | 测试与正式发送区分清楚；可观察退信/退订并避免重复发送 |
| 5. 复盘扩展 | 指标导入、实验与归因、审核后的经验沉淀 | 报告能回指原内容、数据来源和时间范围 |

每层完成后单独提交。评测应使用同一份真实品牌 brief：比较品牌一致性、事实准确性、人工修改量及交付物可用性。当前结果是来源选型，不是营销效果排名，也不是完成集成的声明。
