# 判断、操作与自审（schema_version 2）

配装、只读解释和路线共用；任务范围见[配装](build.md)。程序核对字段、事实、引用与版本，你判断文字含义、原因、覆盖和目标。

## 来源与判断

evidence 必需 evidence_id/kind/subject，ID 在 intent 与 plan 合并后唯一，不以 native: 开头。status/digest/origin 由程序生成。

| kind | 真实内容与边界 |
|---|---|
| user_statement | text 为实际表达；库存仍是用户声明，不是独立盘点 |
| web | url/excerpt 为公开官方或 PoEDB 页面实际正文；其他网站用可用联网工具存 artifact |
| artifact | path 相对声明源 JSON 目录，仅该目录/子目录的文本；JSON 用 view.evidence_template 的实际 pointer，文本用真实 excerpt，二者选一。记录 URL/查阅时间；越界按 source_diagnostic 修源 |
| local_data | data read 的 source/line/count/content_sha256 模板，补 ID/subject；由程序重读，不拿自由 text 替代 |

原生事实无需声明 evidence，沿当前 reference_contract/view 复制 native_references。阶段根属于各自 evaluation，未算阶段没有该根。检查实际对象、数组索引、XML/引擎与 source_revision；引用失效先读诊断的真实父对象/合法引用再修，不猜根名。artifact/local_data 可追溯不证明含义、真实性、当前相关性或游戏内效果。

claim 字段由 input_contract 给出；基本格式：`{claim_id,text,subjects,evidence_refs,conditions,uncertainties,checks}`。claim_type 默认为 judgment；比较用 comparison 且 checks 非空，例如 `{reference:"当前原生引用",op:"ge",value:本次阈值}`。op 为 eq/ge/le，按原始值比较，eq 也支持同类型布尔/字符串。缺值、null、异类型为 unknown，不能当0；检查只证明所列命题。

subjects 关联对象，claim_id 是记录身份。每项目标用 `objective:<objective_id>`；总体用 goal，assessment.goal_claim_ids 只选含 goal 的当前判断。同一 claim 可关联多个适用对象，不必重复正文。依赖以当前 pending/required_subject 为索引：配置用 configuration:candidate 或 configuration:stage:<id>，测量用 measurement，技能用 skill:<skill_ref>，阶段用 stage:<id>，快照用 snapshot:<id>，取得用 acquisition:<instance_id>，训练用 training:<stage>:<instance>，进度用实际 progress subject，物品合法性用 legality:item:<ID>。

`assessment:{goal_claim_ids:["总体判断ID"],scope:"实际用户范围",unresolved:[]}`。每项目标写实际实现、依据、条件、代价与未做部分，不能用旧计划或局部数字代替。条件未证实且影响目标时保留 uncertainty；因果、状态覆盖与用户目标不能由“测到一个值”直接推出。联合实验只解释联合变化，零差分只说明该对象/主体/场景/指标的观察。固定状态账目不证明动态效果或敌人死亡时间。

“只有/全部/不存在”等断言需核同一条件下的相关实际对象与机制，主动查支持和反例；已分配节点、装备或技能的事件效果不能因未进入固定每秒账目而被排除。局部观察不足以证明唯一，覆盖未核就限定为已观察范围。改变条件的实验另列，不能代替原状态下的裁决；发现反证时修正源判断与结论。

## 当前文字与数字

持久 claims 与 review 用返回 text_templates：`{{fact:当前引用}}` 带对象、显示比例和单位，是完整事实句；`{{value:当前引用}}` 仅原始标量，单位/换算自行有据说明。meaning/context 标明 actor、限定对象、XML、配置与引擎；同名指标不能互换。旧手写数字不会随计算刷新。

edit 生成 bindings；对象/来源改变出现 invalidated_claims。读新事实，改源正文、条件、操作与关联，再 review_claims 刷新所选绑定。最终复核写对新数字不能修正仍错误的源 claim。程序投影的 judgments 与 HTML 同源，revision_pending 的旧文不作操作建议；有效绑定仍不认证语义。

配置用一份 configuration:candidate（路线为 configuration:stage:<id>）判断覆盖当前全部场景和控件，引用实际 config_values/controls、原文与进度；无需为每个控件重复声明 config:<key>。需要某控制时写 checks 比较实际 value。装备存在不证明效果覆盖，false/默认/null 见[资料](data.md)。用户声明只说明要求。

## 动作与资源

usage 简单同窗格式为 `{skill_ref,casts_per_second,active_seconds,recovery_seconds,continuous,claim_id}`，技能来自当前 active_skills；claim.subjects 含对应 skill:<ref>。频率次/秒、时间秒，未知省略。原生满速、声明降频与实际 DPS 范围分开；不同窗口不借主技能窗口放行。

独立 single_payments 从每项满可用储量单独检查，不证明连续动作或任意时点。mean_use 是单技能满速费用减固定净回复；mean_pool_depletion_seconds 只是平均耗尽投影，不含混合动作、吸取、充能、药剂、额外损失或动态状态，不认证无限续航。

混合动作使用 `timeline:{duration_seconds:秒数,repeat:false,events:[{skill_ref:"当前引用",at:秒数}]}`。一个 at 只支付一次，不是开始持续施放。事件处于窗口内，按顺序支付；重复周期用 repeat:true，检查接缝及终点恢复初始储量。有限窗口支付通过仍只证明该窗口，sustained_use 不能升级成无限使用。

战前准备在同一 timeline 增加 `preparation:{duration_seconds:秒数,events:[...]}`，对应 usage.phase 为 preparation，其余为 combat。准备一次，等待计时，完整结束余额带入战斗，不自动回满；战斗再使用仍需事件。使用 timeline 时 usage 仅关联 `{skill_ref,claim_id}`（需要时加 phase），不再定义另一组频率/窗口。

自动技能按当前 SkillTriggerRate/费用安排完整窗口，缺实际触发频率或支付规则保留 unknown；零费用不用虚构支付。手动频率按 usage_rate.basis/metric/repeats，不按技能名猜。payment_schedule/skill_schedules 定位当前实际次数、首末时点、费用与关联，修返时同步源 claim。完整窗口 final 在 duration_seconds，trace 最后一项只是末次动作；准备 final 对接战斗 initial。

时间线只重放费用与固定净回复；execution_coverage 保留敌伤、事件恢复、动作互斥及动态效果等未测范围。覆盖结论核实际开关、native_timing、施放与补放时点；准备期施放会随等待到期。主输出和其他动作按真实安排共享时间，原生满速不是同时可执行频率。药剂 continuous 仍受 flask_coverage 限制，按目标核有限窗口即可。

## 物资与价格

requirements/material_specs 来自实际差异，包括仍插着的禁用/备用宝石、加工和退点；原 XML 没有不证明用户没有。inventory/shopping 公共字段 kind、quantity、evidence_refs：gem 用 skill_id/level/quality，item 用当前 spec_sha256，service 用 service_id。item_id 只属某个 XML，跨阶段身份用规格与物理实例。

库存来源 subject 沿 requirement_id，用户实际 context.quantity，装备匹配 spec_sha256；同规格合并，不重复使用同份数量依据。报价 low/high 为整条 quantity 总价，来源 context 匹配 realm:cn、league、currency、quantity、low/high、装备规格和实际 checked_at；quote:<物资引用> 判断关联同一来源与规格。国际服价无汇率/国服依据不算国服已核实价格。

预算 true 需完整来源及总上界在限额内；已知必要下界超限为 false，其他 null。totals_complete=false 金额仅部分。未知、估算、条件交易与已核实分别说明，来源有链接也不等于成交或拥有。

## 最终复核与交付

方案/claims 修订后计算，沿 next_actions.review_current_result 读真实 user_goal_sources、objective_coverage、pending 与当前 check。对照原话，按 review_template 逐项填写所有 objectives/conclusions 的 scope、findings、verdict 与 evidence_refs。反证、条件、未实现项有内容时写 counter_evidence_refs、conditions、unimplemented，省略按空数组处理；supported 需支持，refuted 需反证。

发现偏离先改源并重算，再单独提交 assessment.review，之后 evaluate/export 或重跑 route。token 绑定本次意图、对象、文字、引用、XML/引擎与账目；改变后重审。不要用脚本自动生成语义复核或填 token 假称通过，引用/token 有效不证明文字因果正确。

规划的真实未来依赖可用 condition_reviews：`{subjects,claim_ids,conditions,fallback,evidence_refs}`。subjects 原样取当前 facts.pending，关联本次 supported/partial conclusions，写具体进入条件与在条件失败时能执行的退路（或明确等待再开始）。把能力/未来条件写在适用结论和 condition_reviews；review.unresolved 只记录当前尚未完成的复核工作，不重复列已审查的未测范围。必要设计/来源/计算仍缺、已知非法、矛盾、许可违反、过期复核不能包装成未来条件；普通生成缺口见[配装](build.md#普通目标生成)。

完整 readonly 评审可否定/未知；规划的 unknown/refuted 目标如实说明未完成。按[入口的完成步骤](../SKILL.md#工作与完成)核成功返回的文件：build/build_plan 交 XML 与 HTML，readonly 仅报告，route 交本次路线文件。

publication、facts_summary、HTML 与核验共享事实，最终短答用真实状态、已核结果、代价、限制与下一步。源/输出完整性和恢复见[命令](commands.md#恢复与交付完整性)。已有有限支付、未知预算/覆盖、进入条件各保留；导出文件、自审和链接都不能替代目标依据。
