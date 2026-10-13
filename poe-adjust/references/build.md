# 角色配装与复核（schema_version 2）

本文件管已有角色的解释、作者复核与调整，XML 或完整纯 PoB 分享码文本均可作为 start/inspect 输入。查资料用[资料](data.md)，判断、操作和自审用[证据](evidence.md)，调用/保存用[命令](commands.md)。

## 只读解释

直接事实展示沿 inspect/probe 的事实页完成。解释机制、归因、取舍或复核作者是否满足原目标时，用同一 start/intent/evaluate/export，intent 声明 `task_kind:"read_only_review"`。最小 intent 为 schema_version:2、confirmed:true、goal、非空 objectives、真实 user_statement evidence、constraint_sources 中的 `/goal`；objective 的 evidence_refs 对应用户问题。本机范围可省略 league，此时 game_version/version_source 也留空。

plan 使用 `mode:"refine",operations:{}`，保留输入，不使用 template_xml、scenario、measurement 或 baseline_main_group 改被审对象。实验用 probe，作为证据与审查对象分开。解释写 claims；数字/状态读当前事实，评审每项支持、反证、条件与未测范围。

task_completion 核对审查完成，character_readiness 保留原角色问题。完整评审的 partial/refuted/unknown 可以完成审查，不要求先修原角色；缺引用、过期绑定和缺项仍需修。export 生成中文 HTML 和核验文件，无新 XML，xml_exported 为 false。文件保存不认证解释正确或作者通过。

## 规划交付

用户要可按条件实施的方案用 `task_kind:"build_plan"`；要当前角色满足要求用默认 `build`。不缩掉原目标来完成任务。build_plan 与只读可留 league 未知；目标国服适用性与本机 data_version 分别交代。

默认 `delivery_scope:"execution_plan"` 落实操作。仅用户明确要求局部原生快照比较、无需重做整套操作时，build_plan 可选 `snapshot_comparison`：constraint_sources 的 `/delivery_scope` 关联实际原话；subjects 含 delivery_scope 的当前 claim 同时引用用户依据与 `native:materialization#/actual_changes`，最终复核说明范围。程序只排除原生记录完全未改的非主组的缺失 usage，列入 scope_exclusions；改动组、真实错误与用户目标照常核验，原角色未就绪项保留。

未来取得、训练、进度可按[证据的条件复核](evidence.md#最终复核与交付)记录进入条件和退路。已知非法输入、错误数字、许可缺口、必要设计/计算未完成继续修。完成规划不证明实物已得、持续输出或实战成功。

## 意图与方案

intent 保存真实请求；confirmed 只是流程记录，程序不独立认证聊天授权。限制用 constraint_sources 将 `/字段名` 关联真实 user_statement。未知省略/null，不把模型建议当用户约束。objectives 为 `{objective_id,text,evidence_refs}` 数组。

常用限制按当前 input_contract 选择：预算 amount/currency、保留主技能/职业/升华、keep_slots 的 item_identity 或 exact_spec、keep_skill_ids、minimum_stats、最大更换件/槽数、目标等级与已知可用点数。任务进度未知不套固定奖励公式，许可缺口只用真实用户回答补齐。用户场景在 intent，模型共同测量场景在 plan。

可计算 plan 起点：`{"schema_version":2,"mode":"refine","summary":"本次具体目的","operations":{}}`。rebuild 也使用相同接口，可选 template_xml 相对 plan 目录，来源与迁移代价需说明。完整字段由当前契约提供；sources/assumptions/legality_notes 的文字不自动成为证据。

| operations | 格式与语义 |
|---|---|
| gems | `[{group:1,index:2,skill_id:"当前ID",level:1,quality:0}]`，只改指定颗；可补 enabled/count/skill_part/skill_stage_count/skill_minion 与当前全局开关 |
| groups | `[{index:1,enabled:true,slot:"当前槽",main_active_skill:1,include_in_full_dps:true,group_count:1}]`，字段按需 |
| replace_skills | 最终完整组数组，每组 `{slot,gems:[{skill_id,level,quality}],...组字段}`；保留其他组，空占位用 gems:[] |
| main_group | 当前主组，1起索引 |
| items | 普通目标用下述 craft；实际原件/持有物品/特殊来源用 `{id:正整数,text:"完整原生文本"}` |
| equip | `[{slot:"当前槽",item_id:ID}]`，0 移除；珠宝槽 `Jewel <节点ID>` |
| tree | nodes 全量或 nodes_add/nodes_remove 增量；当前 class_id/ascendancy_id/secondary_ascendancy_id；masteries 全量或 masteries_add 局部映射 `节点ID字符串:效果ID` |
| level / weapon_set | 角色目标等级；武器套装1或2 |
| config | 候选配置；顶层 scenario 是共同场景。类型与选项由当次 config_types/controls 给出，null 删除覆盖，false/0/默认分开 |

main_active_skill 是物化后主动选项序号，与宝石位置不同。计算的 skill_selections 给 index/id/name/gem_index；新组合重新查。granted_effects 的实际 ID、operation_field 与 global_enabled 决定效果开关，effect 序号不代表普通/临时；active_skill_present 只说明原生列表存在。

移动已有宝石要在最终布局移除旧位置再放新位置；停用仍占孔和保留物资。组真正卸下用空 slot。装备 assigned、active、socket_allocated、effective_in_calculation 各有作用，不能用 active=false 推定普通装备未穿；保留规格与保留生效状态按用户口径分别核对。

## 普通目标生成

```json
{"id":100,"craft":{"base":"当前完整基底名","ilvl":60,"quality":0,"sockets":"R-R G-G","affixes":[{"id":"当前词缀ID","roll":0.5}]}}
```

示例只给格式，物等/孔连/词缀按当前取得与数据选择。roll 为范围位置0–1，默认0.5；affixes:[] 生成普通基底。原生检查词缀适用、物等、互斥、数量和孔数，不另套统一3前3后。

先确定可达取得路径和物等，生成最低可用目标，再比较整套。正式最终规格（含模板携入）须有原件保留、有效 owned 或当前 native craft 支撑。解析/SaveDB 回读/legality 判断不代替生成依据；缺普通生成依据的 native:item_generation 是当前设计缺口，不能包装成未来掉落条件。有效配方也不证明基底、物等、品质和孔连在所述地点能取得。特殊机制沿实际物品和当前来源评审；probe 反事实文本不认证正式普通目标。

## 测量与实际变化

`measurement:{flasks_enabled:false,effects:[{skill_id:"当次效果ID",enabled:false}]}` 在两侧各自改造后应用再算，效果设置该 ID 全部实例的原生全局开关，不改宝石 enabled/规格。药剂开关不删除实物；absent 或无已适配开关按诊断处理。模型据目标与覆盖依据选择测量条件，subjects 含 measurement 的 claim 评审，可同时关联 configuration:candidate。

evaluate/export 保存共同 scenario/measurement 下的实际 XML。收益比较 baseline/result；用户保留性另读 original 与 materialization.actual_changes，后者包含原角色到交付的全部变化及 object_before/object_after。共同条件一致不能证明用户角色未被改，改变主输出仍核原始许可。

inspect --metrics 可选当前 skill_ref 与 actor(player/minion/mirage)。breakdowns 的 context 绑定 XML/引擎/配置；Calcs 与 MAIN 不同或缺明细时不借其公式解释 MAIN。各主体伤害/回复不互借。probe 的 `variants:[{id,operations}]` 共用操作格式，各项独立应用；组合须放一项整体算，不能相加单项增益。零差分仅说明该角色/开关/主体/指标下的观察。node screen 不含连接和点数合法性，完整路径另算。筛选不会修改候选，采用后保存完整 plan/evaluate/export。

补路、宝石延期建议先看 preview，再用[原生候选入口](commands.md#采用原生候选)采用；方案可选补属性、延期或换技能。原生自然可用等级不证明已训练、已取得或可免费降级。

## 复核已有交付

收到真实原要求、原角色、作者 XML 与说明再审查。先锁定作者原句的对象、条件与断言范围，不把当前状态改读成所有状态，也不把排他结论改读成举例。作者说明、自评、操作和条件都是待核对象。作者 XML 作为只读 start 输入；原角色 inspect、原目标和作者原文存工作区 artifact。计划空 operations，作者安排只在确实读到后带入，缺失保持未知。新文件写审查工作区。

分别核对原角色与作者角色的真实规格、全局开关、生效槽、场景与许可。审查的 materialization 无差分只说明没再改作者，不证明作者保留原角色。角色身份用 character 引用，字节身份用 input_bytes/saved_bytes；保存可能重排 XML。

涉及资源时分别核对独立单次、平均投影、准备、战斗窗口与覆盖，按完整窗口终点及相接余额评审；基础施法时间之和不能当实测共同准备。用当前 config_control_ids/原文选实验，检查实际变化再谈因果；多项联合变化仅支持联合观察。DPS 不证明敌人死亡时间。直接 `probe --input '<作者.xml>' --variants '<记录.json>' --out '<工作区>/probe.json'`，不手拼 XML。

自己的 claims 和最终复核沿当前模板提交，再 export 同一中文 HTML；否定/未知可完成审查。保留作者原 XML 作为对象。用户另外要求改进时才转 build_plan/build 落实获准调整。
