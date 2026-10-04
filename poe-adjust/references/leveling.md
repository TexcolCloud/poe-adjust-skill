# 开荒与成长路线（schema_version 2）

用户明确调用后，按本文件规划没有完整角色 XML 的阶段路线。已有角色的精确配装继续使用 [配装协议](build.md) 的 evaluate/export。单一路线是有先后顺序的 stages；不同方向用独立路线候选，不另建分支执行框架。

证据、usage、采购与最终复核读取 [共同证据协议](evidence.md)，目录和联网查询读取 [资料协议](data.md)。

## 从用户目标设计

确认实际起点与终点、赛季、交易或 SSF、已有物资、预算、职业/技能偏好及操作负担。用户已说过的内容直接复用；未知不自动填 false、零预算或全新开荒。模型选择攻略数量、候选数量、阶段数量和切换时机。路线可从当前角色开始，同一级可因任务、装备或宝石取得分成多段；不套固定起止等级或至少三个阶段。

职业与升华来自当前 classes；宝石、基底和连线来自当前目录。当前目标游戏版本与国服公告分开核对，不能从赛季编号猜机制版本。资料以实际版本正文、原作者具体攻略和本机数据为依据；不把作者首页或旧搬运链接当完整路线证明。路线规模按需要，不为目录查询下载不相关整套构筑。

使用 `research --gems` 读取所选 ID 的原生逐等级要求、类型、基础资源及网页取得行。任务奖励/商店分别核对章节、任务、NPC、职业和实际完成条件；可使用后续商店、小号、交易、掉落、制作或用户持有等真实来源。只读到某个早期职业行不表示其他职业永远拿不到。不得凭通用商店印象编造解锁任务。领取同一任务的多种奖励时核对选择数量与费用，不能把一行一个奖励当全部免费同时得到。

升华路径读取当前 nodes 的起点/linked；点数包括连接点。`research --trials` 只提供网页观测位置与区域等级，不证明解锁、已通关或奖励总点数。按实际已完成或计划进入条件评审 progress claim，不能从等级或一张固定奖励表推算玩家已获点数。

## 技能、物资与过渡

每组表示同一物理连线，可以多个主动共享辅助，也可以放练级宝石。use 是自由用途描述，没有 main/aura/utility 固定决策枚举。辅助未作用时如实记录；若保留练级用途，不把它算成伤害辅助。仍插着的禁用组占孔；确实未装备才写空 slot。

每颗宝石有稳定 instance_id。同一 ID 的两颗宝石需两个实例；同一颗跨阶段升级沿用实例，不能换 skill_id 后仍称同一颗。自动等级 `auto` 仅在明确请求时选择当前数据可练且角色等级可用的最大值，不代表最优、已练到或已取得。升品质、特殊等级或降级处理说明加工路径和代价，必要时另记物资。

顶层 gems 只复用目录 ID；取得依据绑定首次使用阶段的 required_items 与实例，不能用一个全局“已拥有”解释两颗同时使用。跨阶段同一实例不重复采购；提前练级/储备可列为实际已安排的位置或未装备组，并说明取得与成长条件。库存声明要匹配规格、数量；用户不必提供游戏截图来证明普通持有，报告注明按用户声明。

每阶段 bases 映射当前原生槽位到具体基底，程序核对 valid_slots 与最大孔数。基底最大容量不证明实物已经有该连数、孔色或特殊效果；实际物品需在进入条件中落实，精确影响用角色 XML 实算。没有具体基底则容量未知，不猜同类武器都一样。weapon_setup 可写自由描述，不再用五种固定布局裁决玩法。

核心物资不确定时，模型可以延后切换、等待取得、选择其他流派或安排可执行过渡。用户已备齐、接受取得后再切换的目标，不强制普通备用宝石。声称有过渡就写成具体阶段及连线、装备/属性、资源和进入条件，不能只给 fallback 名称。SSF 禁止依赖交易来自用户明确限制；“不玩召唤”的具体范围按用户意图解释，不把任何 minion 标签都自动禁用。

没有完整阶段 XML 时，只报告本机类型/等级/基础资源、基底容量、路径和来源校验。不虚构精确 DPS、血量、抗性或续航。对关键切换，需要精确数值时填写本文件的 snapshot，由 route 生成阶段 XML 并实算；最终已有角色的调整再使用 evaluate/export；目标装备若是假设，说明假设而非已有实物。手动诅咒、位移、供尸、开关光环等必须落实实际技能和操作，不隐去成本。

## 文件与调用

```powershell
python '<技能目录>/scripts/poe.py' route --invoked --file '<工作区>/route.json' --out '<工作区>/开荒路线.html'
```

route 自动分批读取当前宝石、连线、基底和完整升华目录，并核对证据。输出 HTML 及同名 `.evidence.json`。按照 pending 修订；最终 status 是 draft/scoped_result 或预算已知矛盾的 blocked。阶段是否实算以 calculation 及对应 XML/证据记录为准，路线状态不代替实算范围。用 TeleAgent 文件工具交付 HTML，简要说明范围和缺口；可解释已引用 claims，不另造一套收益数字。

先读取返回的 errors、stage_summary 和 delivery_brief。阶段 `native_calculated:true` 仅证明计算完成，最终 `status:blocked` 仍有已知机械错误；先修到 errors 消失再把 XML 作为路线配装交付。unsupported 的 diagnostic.location/expected/actual 给具体字段；不要逐次猜协议。HTML 直接显示阶段校验、已知错误、原生数值及进入条件，完整资料留 evidence.json，按需读字段。

顶层字段：

| 字段 | 内容 |
|---|---|
| schema_version / confirmed | 2 / true，先完成必要的方向问询 |
| objectives | 按实际用户要求拆分的 `{objective_id,text,evidence_refs}`；每项对应 `objective:<id>` claim，关联阶段、事实与未解决条件，详细规则见 evidence.md |
| name / goal / league / scope | 名称、真实用户目标、国服目标赛季、实际起终点与覆盖范围 |
| class_id / ascendancy | 当前目录职业 ID 与升华英文名；升华尚未定可省略 |
| game_version / version_source | 目标机制版本和实际官方公告 URL；未核实保留缺口，不自填状态 |
| trade_mode | 用户明确选择 ssf/trade，未知省略 |
| allow_minions / allow_mule | 仅在用户明确表达时填布尔值；具体召唤限制由 preference:allow_minions claim 解释 |
| budget | 已确认的 amount/currency，未知省略；与配装相同的有限非负格式 |
| gems | `{本机skill_id:{note:"可选目录备注"}}`，不再存获取/备用策略 |
| stages | 非空执行阶段数组，同级允许，等级不倒退；当前前往章节可回访 |
| evidence / claims / constraint_sources / assessment | 沿用命令协议，用户 goal/trade_mode/budget/allow_* 对应用户表达；模型方案不冒充用户约束 |
| inventory / shopping | 沿用命令协议的规格、数量、整条总价与引用 |
| risks / notes | 文字数组，补充局限和操作提醒，不用非空说明替代证据 |

每个 stage：

| 字段 | 内容 |
|---|---|
| stage_id / name | 唯一稳定 ID 与名称 |
| level | 角色等级，未知可省略/null；auto 宝石等级需要已知角色等级 |
| act / completed_act | 当前前往章节与已完成章节进度分开，正整数/非负整数；章节号不能代替具体任务完成 |
| entry_conditions | 该阶段条件 claim_id 数组，claim.subjects 包含 `stage:<stage_id>` |
| entry_checks | 可选数值/配置条件数组；每条含 check_id、locator、op、value、claim_id，见下文 |
| bases / weapon_setup | `{原生槽位:完整基底名}`；武器布局可选自由说明 |
| skill_groups | `[{name,slot,use,enabled,gems:[{instance_id,skill_id,level,quality}]}]`；level可填auto，quality默认0 |
| gear / passives / checks / transition | 各为文字或文字数组，说明目标属性、天赋顺序、可观察检查与下一段条件 |
| ascendancy_points / ascendancy_nodes | 本阶段声明可用升华点数与累计目标节点 ID；程序合并当前原生连接路径，不只数大点；留点允许 |
| resources | 费用、回复、操作与限制的文字说明；引用相应 stage/usage 判断，不作为精确实算结果 |
| required_items | 本阶段新增取得/加工需求，下面说明 |

宝石 required_items 条目：

```json
{
  "kind":"gem", "instance_id":"fireball-one", "skill_id":"Fireball",
  "level":1, "quality":0, "quantity":1,
  "acquisition":"vendor",
  "available_from_act":1, "quest":"使用 research 实际返回的任务", "npc":"实际 NPC",
  "class_ids":[4], "evidence_refs":["对应来源记录"]
}
```

以上仅展示格式，不认证 Fireball 在此职业/阶段的取得方式；必须替换成实际查询行。装备使用 kind:item/item_id/spec_sha256，服务用 kind:service/service_id，quantity 均正整数。acquisition 可表达 owned/quest/vendor/trade/drop/lab/craft 等实际来源，不强制随机来源附普通替代。

owned 需要 user_statement 记录，subject 匹配物资引用，context 包含实例对应的 instance_id 与 quantity。购入物资同时关联 inventory/shopping 的规格；不要将同一实例的首次购入费用重复列在后续阶段。quest/vendor 须对应当前页面唯一行，程序不能证明任务已做或奖励选择；由 `acquisition:<instance_id>` claim 解释进入条件、成长到所填等级、费用与限制。其他来源用实际取得的证据及同对象 claim；不能只写一句“能刷到”。

route 的 claims 可引用声明的网页/资料，以及本次 `native:catalog#/...` 下实际存在的目录字段。初次可用空 claims 得到草案，读取 evidence 文件后补精确引用。必要 subjects：`goal`、`stage:<stage_id>`、`acquisition:<instance_id>`；安排升华用 `progress:<stage_id>`，换分支用 `respec:<stage_id>`，同实例调整特殊规格用 `transformation:<stage_id>:<instance_id>`。这些是对象引用契约，具体策略由模型决定。

assessment 使用 `{goal_claim_ids:[...],scope:"实际评审范围",unresolved:[]}`；scope 不能缩掉用户已明确要求的终点。每段判断核对所列资源技能确实安排、切换前后依赖未丢、操作满足偏好，以及取得条件可执行。来源观测与模型判断分开显示；缺一条实际来源时保留相应缺口，不凭非空文字消除。

数值进入条件用 `entry_checks`：`locator` 指向本阶段原生结果，例如 `/stats/FireResist`、`/stats/ManaUnreserved` 或 `/config_values/resistancePenalty`；`op` 为 ge/le/eq；`value` 是模型结合本次目标选定的有限数值，eq 也支持同类型字符串/布尔值。`claim_id` 引用对应 `stage:<id>` 判断，说明为何选择这条门槛。没有固定抗性或等级要求，不能用其他阶段的值替代。缺实际字段为 unknown，未达到为 unmet，即使有文字理由也保留进入条件缺口，路线只能作为条件草案。

声称“补抗后切换”时，应把相应装备规格、取得方式及效果放进 snapshot 再算；尚未达到的目标保留 entry_checks.unmet，不能在结论说已解决。资源方案填 snapshot.usage 的频率、使用窗口和恢复条件；药剂单件参数可从 calculation 的原生 equipped[].flask_local 或 data 读取，但不包含角色修正、当前充能或补充事件，不能凭它证明无限覆盖。需检查当前前往章节、已完成章节和配置是在同一阶段时点。

弃用字段：version_status、shared_trials 默认值、upgrade_gems、fallback_skill_id、ascendancy_respec_reason，以及旧固定武器/用途枚举。其有效内容分别进入真实用户声明、后续阶段或有引用的 claims。旧字段不能在新 schema 静默忽略。

## 关键阶段内部 XML

模型依据本次目标识别依赖变化明显的阶段（技能/装备/资源切换等），标注 `key_transition: true`。关键阶段缺少 `snapshot` 会保留待办。阶段数量、进入等级和选择何时切换仍由模型决定，不用固定剧情模板。

`snapshot` 字段：

- `template_xml` 可选，指向真实角色模板；省略时复用当前原生职业空角色。程序生成内部 XML，不要求玩家先有角色，也不要手写最小 XML。
- `operations` 复用配装协议的 items/equip/tree/config/main_group/weapon_set。角色等级与基础技能组来自本阶段。gems 仅补计数、技能部位等状态；groups 仅补 main_active_skill/include_in_full_dps/group_count，不能在快照偷偷换掉阶段列出的宝石、等级或位置。

  例如 snapshot.operations.groups 使用 `{index:1,main_active_skill:1}`，组启用写 stage.skill_groups[].enabled；等级写 stage.level。职业/升华由路线和本阶段分配推导，不需要在快照重复填写。
- `assumptions` 为 claim_id 数组，对应 claim.subjects 含 `snapshot:<stage_id>`。说明实际假设的装备词缀/孔连、天赋、操作和何时取得；资料只证明某物品存在时，不能说用户已经拥有。
- `usage` 与配装协议相同，引用阶段实际 skill_ref；需要时先运行 route，读取该阶段 calculation.active_skills 后补全。
- `metrics` 可选指标名数组。只取实际原生输出，不在当前结果中的指标保持未知。

同一次 route 会生成阶段 XML、回读并绑定摘要，然后解析 `native:stage:<stage_id>#/stats/Life` 等实际引用。因此 stage/snapshot/resource 等相关判断可同时引用该阶段原生结果；与资源有关的 claim 还需标明 `stage:<stage_id>` 或 `snapshot:<stage_id>`，避免把另一个阶段的使用说明混入。

内部文件位于输出 HTML 同级 `<输出名>.calculations/<阶段摘要>/snapshot.xml` 和 evaluation.json；snapshot.json 保存已回读的阶段计算，输入/结果/文件摘要和实际引擎指纹一致才复用。`.state.json` 记录计算阶段与交付是否完成，失败后重跑同一 route 命令即可；损坏或失效的阶段重算，来源与判断仍重新核对。`calculation` 分开列明完整装备条件、数值、孔位/属性/天赋检查和资源缺口；没有完整输入或计算失败的阶段不生成虚构统计。现有模板遗留的装备/节点也会进入实算，核对它们是否属于本阶段。

本阶段实际装备需与 bases 一致，具体物品及规格摘要需出现在截至此阶段的 required_items 中。程序在 calculation.material_specs 返回可直接引用的 item_id/spec_sha256/quantity，不必手工计算摘要。快照是条件规划，已回读不代表装备可得、剧情完成或目标达到。HTML 和 evidence.json 的状态仍取决于各阶段与来源缺口。

所有实际基底从当前 catalog 查询，不只查武器；不能凭熟悉的名称造盔甲或靴。被引擎忽略的物品、未满足需求或丢失节点都是已知错误，不把忽略后的面板当完整假设成功。逐阶段核对环境配置和已完成进度；终点条件不能自动沿用到前面阶段。声明某任务可选奖励不代表同任务多颗奖励都免费；说明选择、商店费用和宝石成长的可执行路径。资料模板可以直接从 research 的 choices[].evidence_templates 取得，再关联具体实例和 claim。

route 同时生成 `<输出名>.review.json`，集中列出每阶段事实、物资、引用及缺口。用同一 TeleAgent 模型核对数字/对象、因果、替代方案排除、进入条件和结论范围；在原 route.assessment.review 填 token/findings/unresolved 后重跑。格式和失效规则与配装相同。改动阶段、说明、来源或本机数据后旧复核失效，不自动将旧文本绑定新数值；完整交付以 state.file_exported 为准。

物理实例的 required_items 只登记首次取得，后续沿用不重复计费。再次购买另一颗同名宝石使用新 instance_id；对旧实例升级或改造记录实际转换依据与服务/材料。已匹配的 owned 实例声明自动用于库存核对，无须在 inventory 重抄；显式 inventory 同规格记录会优先使用，但仍核对声明数量和规格。不能重复使用同一份数量凭据抵消多颗物品。
