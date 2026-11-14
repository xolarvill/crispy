# 本地营销方法包

日期：2026-11-14。目标：参照 SEO workbench，用本地方法索引支持 agent 按目标选择和组合技能。页面分区与用户优先级以 [覆盖文档](marketing-skills-research.md) 为准。

## 第一批覆盖

| 用户分区 | 本地入口 | 交付与边界 |
| --- | --- | --- |
| 记忆与知识 | brand-context | 已确认产品事实、品牌表达、证据与待确认变更分开维护 |
| 记忆与知识 | market-research | 可追溯研究发现及适用范围；研究不自动覆盖品牌知识 |
| 生产 | social-content | 渠道内容、内容日历和衍生关系；可直接处理局部改稿 |
| 生产 | email-campaign | 单封邮件、EDM 批次或序列设计；不建立发送服务或完整 CRM |
| 生产 | creative-video | 产品视频简报、脚本、分镜及已有视频分析；媒体证据有覆盖边界 |
| 生产 | content-review | 内容详情中的事实/品牌/交付检查；不替人批准 |
| 数据 | performance-review | 指标口径核对、表现诊断及带证据的行动建议，兼顾广告复盘 |
| 数据 | conversion-review | 同口径漏斗与路径诊断；修改页面时交给生产任务 |
| 工作流 | publication-handoff | 准备排期与渠道交付包；实际发布依赖已存在且已获授权的连接 |

公共基础通过共享上下文、产物版本、来源和身份关联实现，不增加宽泛的公共基础技能。营销规划、完整活动管理、社区倡导、持续生命周期自动化和独立实验管理仍未实现；欢迎/购后/召回邮件可以作为内容设计任务。广告账户操作、SEO 深度审计、静态图像生成及落地页构建使用当前会话适用的专门工具/技能，本包不假称提供所有执行器。

## 复用与来源对照

| 来源 | 本次处理 | 后续用途 |
| --- | --- | --- |
| 本地 SEO workbench | 参考其任务索引、来源登记和目标驱动选择的组织模式；不复制技能正文 | 比较其中 social-content、email-sequence、paid-ads 等方法；保留其真实上游归属 |
| crispy personas | 保留现有运行职责；技能作为可由编码 agent 读取的方法，未改变 persona 版本或 stage registry | product/planning persona 的上下文与 brand-context、market-research 分工；copy/video persona 的制作职责与内容技能关联 |
| Anthropic Marketing | 已引入 3 项 | 活动策划、品牌审阅、活动复盘 |
| Corey marketingskills | 已引入 4 项 | 社媒内容、邮件序列、广告变体、实验设计 |
| Resend skills | 已引入 2 项 | 邮件模板与质量检查 |
| BlackTwist social-media-skills | 已引入 3 项 | 内容日历、内容复用、社媒表现分析 |
| Agency Agents / alirezarezvani | 候选；未导入 | 平台角色、需求获取及分析补缺 |
| Composio | 连接候选；未导入 | 有具体执行需求时验证许可、账户和工具覆盖 |
| aronhy/marketing-skill-hub | 专项评估线索；没有复制、翻译、改写其 Markdown 或导入配套资产 | 相关任务由本地原创技能处理；其技能正文为 CC BY-NC 4.0，不作为可直接商用的 vendor |

13 个本地方法入口通过任务索引发现。其中 8 个入口按具体任务链接 12 份上游技能正文。原文、配套文件和许可证保存在 third_party/marketing；逐文件来源、固定提交及指纹见 [来源清单](local-skills-inventory.json)。更新时从同一来源提取目标版本、审查差异与随附许可，更新提交及指纹后执行 check。refresh 只更新本地原创文件。

## 发现与执行

仓库指令引导编码 agent 查阅 `skills/README.md`，读取适用的技能后按当前目标工作。索引不是白名单或必经流程，局部请求不强制重做研究或完整规划。

本包不自动注册到全局 Codex 配置，也不安装隐藏技能副本。FastAPI/PipelineWorker 仍使用原运行时；没有新增自动选路服务、MCP gateway、数据库写入或平台连接。后续运行时集成需要单独实现并验证，不以“文件已存在”冒充已上线。

来源登记保存在 `docs/local-skills-inventory.json`，`scripts/skill_inventory.py check` 检查文件集合、身份和指纹；有意编辑后用 `refresh` 更新记录再检查。文本指纹先将 CRLF 统一为 LF，以便跨平台 checkout；其他文件按原始字节计算。新增材料先显式登记身份，refresh 不自动将未知文件标成原创。Git 历史记录逐层变更。结构检查不证明任务效果；代表性任务和审读结论见 [验收记录](local-skills-validation.md)。
