# 按任务选择营销方法

从用户目标、当前店铺/产品、已有产物和证据出发，读取适用的 `SKILL.md`。复用已有且适用的上下文；只询问阻碍当前交付的缺项。可组合方法，不要求串行运行整个营销流程。

| 要做什么 | 方法 | 可交付什么 |
| --- | --- | --- |
| 整理产品知识、定位、品牌语气或事实变更 | [brand-context](brand-context/SKILL.md) | 确认事实与建议变更清单 |
| 研究市场、竞品或客户问题 | [market-research](market-research/SKILL.md) | 带来源、日期和范围的发现 |
| 写社媒、安排内容批次或一稿多用 | [social-content](social-content/SKILL.md) | 帖文、素材需求、排期草稿 |
| 写营销邮件、newsletter、欢迎/召回序列 | [email-campaign](email-campaign/SKILL.md) | 邮件内容与序列规则草稿 |
| 写产品视频、分镜或分析已有视频 | [creative-video](creative-video/SKILL.md) | 脚本、分镜、媒体观察与修改方案 |
| 核对具体内容的事实、品牌或渠道交付 | [content-review](content-review/SKILL.md) | 定位到版本和段落的检查意见 |
| 解释内容、渠道或广告表现变化 | [performance-review](performance-review/SKILL.md) | 可比指标、解释与行动建议 |
| 检查订阅/询盘/注册/购买路径 | [conversion-review](conversion-review/SKILL.md) | 漏斗诊断与改进任务 |
| 把已选内容交给发布、排期或人工导出 | [publication-handoff](publication-handoff/SKILL.md) | 版本明确的渠道交付包 |

上下文按需携带：workspace/shop、产品、目标受众、语言/市场、已确认事实与证据、产物版本、当前用户决定。不能把其他店铺的同名产品或过期研究混入当前任务。缺少产品知识时可以局部补充，不要求先运行 brand-context。

现有对象只是可选关联：`Campaign`、`PipelineRun`、`RunVariant`、`VariantAsset`、`ContentSchedule`、`GmMemory`。有真实 ID 才引用；产物可以直接在聊天交付，或按用户要求保存到任务文件。此处产物字段是交接约定，不是已上线的 API schema。

人和 agent 编辑同一份成果，修改后引用新的版本；已经批准的正文待发布时直接准备交付，不重启研究或自动重写。事实修订建议与已确认知识分开，批准、发送和发布遵循当前授权。

这些文件由编码 agent 读取使用，尚未接入后台 agent runtime 或全局技能安装。覆盖、来源和维护方式见 [设计记录](../docs/local-skills-design.md)；静态图片、HTML 邮件渲染、网站编辑等执行任务使用会话已有的专门能力。
