# 判断、操作与自审（schema_version 2）

原生引用根取本次reference_contract，判断与复核沿[命令](commands.md#增量保存方案)保存。

## 来源与判断

| kind | 引用 |
|---|---|
| user_statement | 真实原话，只证明用户方向/持有声明 |
| web | 已读官方或PoEDB正文的url/excerpt；其它网站存artifact |
| artifact | 获准准确路径；JSON用view模板pointer，UTF-8文本用view模板excerpt，保留URL/查阅时间 |
| local_data | data read模板source/line/count/content_sha256，加ID/subject，程序重读 |

原生引用核XML/引擎/配置/source_revision及真实指针；阶段根限其evaluation。失效先读实际父对象/合法引用。来源可追溯仍要核内容是否支持本句；取得表不证明装备或机制。

claims按同一理由合并适用对象，含claim_id、text、subjects及实际evidence_refs/conditions/uncertainties/checks。目标用objective:<id>关联，总体由assessment.goal_claim_ids选；其它subject沿当前pending/required_subject。comparison需非空checks，以当次reference及eq/ge/le核所选阈值；普通解释用judgment。未知不填0。

解释先审验证方法：在现有findings区分证据直接证明的范围、新增前提及反例/缺口。查当次规则/明细的输入、已计因子和上限；声称必要、唯一或不存在可行项前，检验本对象与约束下的反例。有限比较只支持已测范围，未排除的选项保留未知，按实际比较取舍。再从原目标反查，即使数值成立还可能缺什么。分别核对主体、资源和窗口；联合变化限联合观察，固定快照限当次状态。来源、自审绑定或数值差不自动证明因果。

## 当前文字与数字

持久文字沿当前模板：{{fact:引用}}为完整事实句，{{value:引用}}为原始值，换算自行核公式。名称、状态、槽位取最终对象。依赖变化先修文字再review_claims；最终token只关联当前材料。

配置用configuration:candidate（路线configuration:stage:<id>）判断覆盖实际config_values/controls、目标及进度；需特定控制时checks核实际value。效果开启、装备存在与持续覆盖分别核，细节见[资料](data.md)。

## 动作与资源

按原目标选择单次、平均或完整窗口：

- single_payments各从满可用储量独立支付；mean_use为单技能满速费用减固定净回复，正值为消耗、负值为余量，mean_pool_depletion_seconds为同口径投影。publication按当前及各实验变体呈现，吸取、充能、药剂补充、额外损失及动态变化未含；平均余量不证明持续，固定回复与事件回复也不能合并成同一个必要条件。
- 简单同窗usage用{skill_ref,casts_per_second,active_seconds,recovery_seconds,continuous,claim_id}，单位次/秒及秒；未知省略。claim关联skill:<ref>，每技能用自己的窗口。
- 准备接战斗或混合动作用timeline:{duration_seconds,repeat,events:[{skill_ref,at}]}；at只付一次，repeat:true核接缝。preparation:{duration_seconds,events}只准备一次，等待耗时、实际余额带入战斗；需补放的效果在战斗重列。此时usage只关联skill_ref/claim_id及必要phase。

核payment_schedule/skill_schedules实际次数、首末时点及费用，final在duration_seconds，trace末项仅末次动作。主攻、位移及手动技能共用时间，按native_timing核到期/补放，未知自动频率不凭名字猜。

时间线只重放费用与固定净回复；execution_coverage保留敌伤、动作互斥、事件恢复等未测，DPS未按节奏重算。解释实际已算安排及其伤害范围；目标依赖未算部分时，设计有据的降频、补给、退出或可用前段。保留未知不能代替必要操作设计。药剂参数见[资料](data.md#技能配置与明细)。

## 物资与价格

requirements/material_specs取实际差异，含停用/备用宝石、加工和退点；原XML没有不推出用户没有。inventory/shopping共用kind/quantity/evidence_refs，gem取skill_id/level/quality，item取spec_sha256，service取service_id；同规格合并数量，来源不重复占用。

库存据真实user_statement的requirement_id、规格及context.quantity。报价low/high为整条quantity总价，context对应realm:cn、league、currency、quantity、low/high、规格与checked_at；quote:<引用>判断关联同条来源。国际价无国服/汇率依据不冒充国服已核价。

预算true需完整来源及总上界不超限，已知必要下界超限false，其余null；totals_complete:false只算部分金额。未设预算可保留价格未知，取得步骤仍需设计。

## 最终复核与交付

读本次review包facts.user_goal_sources，对照最终实物和完整必要操作：每项目标由什么承担？核实际变化、费用/频率、槽位、效果、取得与切换，矛盾先修源重算。

沿review_template/check审全部objectives/conclusions。scope限实际对象/窗口，findings写支持、反例及未知；evidence_refs/counter_evidence_refs放实际依据。原话只证明要求/持有，目标达成据当前对象、观测或适用规则判断。

| 状态 | 位置与处理 |
|---|---|
| 对象已完整设计，未来待取得、训练或执行 | conditions及condition_reviews，写具体条件及退路，可完成规划 |
| 必要规格、操作或退路尚未设计 | unimplemented，继续设计实算 |
| 本轮工作或评审未完成 | assessment.unresolved及assessment.review.unresolved；逐项完成，已审未来条件移至conditions/condition_reviews |

已审未测范围留scope/conditions；完整否定/未知可完成readonly，规划目标未知或被反驳仍未完成。

condition_reviews用{subjects,claim_ids,conditions,fallback,evidence_refs}：subjects取当前facts.pending，关联supported/partial判断。退路须在条件失败时仍可执行，或保留已有可用前段待条件满足再切换；来源、计算、合法性或许可缺口回源修复。

用edit --review-from关联当前计算，再重算、正式发布。build/build_plan交XML与HTML，readonly只报告，route交路线和实际关键XML；只取成功files。短答沿当前publication/facts_summary/HTML写给玩家的选择、用法和限制，恢复见[命令](commands.md#恢复与交付完整性)。
