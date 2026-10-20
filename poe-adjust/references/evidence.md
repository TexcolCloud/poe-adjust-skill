# 判断、操作与自审（schema_version 2）

字段取input_contract，原生引用取本次reference_contract。

## 来源与判断

| kind | 实际内容 |
|---|---|
| user_statement | 真实原话，只证明用户的方向/物资声明 |
| web | 已读官方或PoEDB正文的url/excerpt；其他网站存artifact |
| artifact | 获准文件准确路径；JSON用view模板pointer，UTF-8文本用view模板excerpt，保留URL/查阅时间 |
| local_data | data read返回source/line/count/content_sha256模板，补ID/subject，程序重读 |

原生事实直接复制当前native_references，核XML/引擎/配置/source_revision与真实指针；阶段根属于各段evaluation。失效先读实际父对象/合法引用再修，不猜根名。来源真实可追踪后，再确认它的内容确实支持本句，取得表不替代装备/机制证据。

对象稳定后集中写claims：claim_id、text、subjects、evidence_refs及实际conditions/uncertainties/checks。同一证据与理由合并适用对象，不逐条待办另造结论。目标关联objective:<id>、总体判断由assessment.goal_claim_ids选定，其他subject取当前pending/required_subject。

claim_type默认为judgment；comparison需checks，用当前reference及eq/ge/le比较实际阈值。缺值/null/异类型为unknown，不填0；检查仅证明所列命题。assessment的scope写真实范围、unresolved保留未完成工作。

解释先确定原命题的对象、场景与范围，从明细/原文查输入、已含因子、上限和反例，再写推断。局部观察只说明局部；“只有/全部/不存在”需相应覆盖，不能把未进入固定账目的事件效果排除。联合实验只说明联合变化，固定快照不证明动态覆盖或击杀时长。

## 当前文字与数字

持久文字用返回模板：{{fact:当前引用}}是含对象/比例/单位的完整事实句，{{value:当前引用}}仅原始值，换算自行核公式。名称、技能状态与所在槽也从当前对象取，避免抄早期候选。

绑定与复核token只关联当前材料，不更新旧正文；返修后沿当前源与计算核全文一致。

配置以一份configuration:candidate（路线configuration:stage:<id>）判断覆盖实际config_values/controls、目标与进度，无需逐控件重复声明。需要某控制时用checks比较实际value；装备存在、默认值或开关开启不证明效果持续，细节见[资料](data.md)。

## 动作与资源

按原问题选择单次、平均或完整窗口；只查单次不扩成整套操作认证。

- single_payments各从满可用储量独立支付，不证明连续动作。mean_use为单技能满速费用减固定净回复；mean_pool_depletion_seconds只作同口径平均投影，吸取、充能、药剂、额外损失与动态变化未含。
- 简单同窗usage用{skill_ref,casts_per_second,active_seconds,recovery_seconds,continuous,claim_id}，单位次/秒和秒；未知省略。claim关联skill:<ref>，各技能用自己的窗口。
- 混合动作或准备接战斗用timeline:{duration_seconds,repeat,events:[{skill_ref,at}]}。at只付一次；repeat:true核接缝与末尾恢复。preparation:{duration_seconds,events}只准备一次，等待计时且实际余额带入战斗；战斗重放再列事件。此时usage只关联skill_ref/claim_id及必要phase，不再另给冲突频率。

检查payment_schedule/skill_schedules的实际次数、首末时点与费用，完整窗口final在duration_seconds，trace末项仅末次动作。主攻、位移、手动技能共用真实时间，满速频率不能同时相加；准备效果按native_timing和等待核到期/补放。自动技能缺实际频率或支付规则留未知，不凭名字猜。

时间线只重放费用与固定净回复，execution_coverage保留敌伤、动作互斥、事件恢复等未测。解释实际已算节奏的伤害；静态吸取或开启药剂不补成完整窗口。若原目标依赖未算部分，设计有据的降频、补给、退出或可用前段；保留未测限制不等于缺少必要操作设计也完成。药剂参数范围见[资料](data.md#技能配置与明细)。

## 物资与价格

requirements/material_specs取实际差异，含仍插着的停用/备用宝石、加工与退点。原XML没有不推出用户没有。inventory/shopping共用kind/quantity/evidence_refs：gem取skill_id/level/quality，item取spec_sha256，service取service_id；规格相同合并，数量来源不重复占用。

库存用真实user_statement对应requirement_id、规格及context.quantity。报价low/high为整条quantity总价，来源context对应realm:cn、league、currency、quantity、low/high、规格与checked_at，quote:<引用>判断关联同条来源。国际价无汇率/国服依据不充当已核国服价。

预算true需完整来源及总上界在限额内，已知必要下界超限false，其他null；totals_complete:false仅部分金额。未设预算时价格未知保留说明，物资取得仍需设计；有预算按限额核验。

## 最终复核与交付

先读本次review包的facts.user_goal_sources，对照最终实际对象：每项目标由哪些已设计/计算的装备、技能、场景、进度与完整时段承担？用当前变化、费用/频率、启用槽位和效果状态核全文；数值/名称沿当前模板，矛盾先修源重算。

沿review_template/check审全部objectives/conclusions。scope限实际对象/窗口，findings简写支持、反例与未知。evidence_refs/counter_evidence_refs放实际支持/反证；目标达成从本次对象、观测或适用规则取得，原话仅证明要求/持有。

| 实际状态 | 写入位置 | 处理 |
|---|---|---|
| 完整对象已设计，玩家未来待取得、训练、通关或执行 | conditions及condition_reviews | 写具体条件和退路，可完成规划；就绪仍保留条件 |
| 本轮必要规格、操作或退路尚未设计 | unimplemented | 继续设计和实算 |
| 本轮所需评审尚未完成 | review.unresolved | 继续复核 |

已审未测范围留scope/conditions；完整否定/未知可完成readonly，规划目标未知或被反驳则保留未完成。

condition_reviews用{subjects,claim_ids,conditions,fallback,evidence_refs}，subjects取当前facts.pending，关联当前supported/partial判断；退路在条件真的失败时仍能执行，或保留已可用前段等条件满足才切换。来源/计算缺失、已知非法、矛盾或许可缺口回源修，不能仅改评审结论清待办。

沿[复核提交](commands.md#增量保存方案)关联当前计算并重算，按[入口完成步骤](../SKILL.md#工作与完成)取files：build/build_plan交XML与HTML、readonly仅报告、route交路线及实际关键XML。短答沿publication/facts_summary/HTML的当前结果与取舍，恢复见[命令](commands.md#恢复与交付完整性)。
