# 角色配装与复核（schema_version 2）

角色解释、作者复核与改装沿本协议；成长见[路线](leveling.md)，字段取input_contract，保存见[命令](commands.md)。

## 只读解释

原生观察、来源范围及独立算术直接用inspect/probe事实页；判断数值能否支持用法、解释原因或推断来源是否齐全，用start/intent/evaluate/export：intent.task_kind为read_only_review，plan为refine及空operations。原对象保留，实验另用probe。本机范围可留league/version未知。

先锁定原句的对象、场景和范围，再用当次规则/公式核前提、反例与结论。计算与某禁用判断冲突时，查字段用途及规则。完整否定/未知可完成只读审查，原角色问题留character_readiness，export仅报告与核验。

## 规划交付

条件方案用build_plan，当前即用就绪核验用build；[入口](../SKILL.md#入口和范围)决定原范围。snapshot_comparison只用于用户明确要求的局部比较：constraint_sources关联原话，delivery_scope判断引用该依据及native:materialization#/actual_changes。未改非主组的usage可不纳入比较；相同支付错误由当前delivery_scope.inherited_usage_errors列主体与依据，模型判断是否影响原目标，再用condition_reviews关联范围判断及所列引用。不相关的已审问题保留在character_readiness，相关、改变或未复核的错误仍阻止交付。

先落实能承担目标的最低对象。整套任务说明装备、天赋、技能如何共同工作及保留理由，局部任务保持许可边界；不为凑改动强制换件。未来条件和本轮设计缺口见[最终复核](evidence.md#最终复核与交付)。

## 意图与方案

intent登记原目标、场景、许可、预算、保留项及真实user_statement依据。整组声明保留用keep_skill_groups原文件1起序号；程序对照原声明及当次原生保存的等价声明，派生效果另审，原件字节身份分别保留。confirmed仅流程记录。等级/点数取实际进度，progress_limits只是当次上界。

用[增量保存](commands.md#增量保存方案)创建plan。rebuild同接口，可用相对plan目录的template_xml，说明来源及迁移。非显然语义：

- gems/groups用1起组/index；replace_skills是最终组。撤下组清空，存放组空slot且停用；停用但仍插着的宝石占孔、需物资。
- main_active_skill取物化后skill_selections，区别宝石位置。granted_effects沿实际ID、operation_field和global_enabled；效果存在/开启不证明持续。
- items用`[{id,text}]`或craft，正整数id；equip用`[{slot,item_id}]`，0移除，珠宝槽为`Jewel <节点ID>`。config按当前控件选值，null删除覆盖，false/0/默认分开。
- 移动实例先移原位置再放新位置；核装备孔连、属性及生效槽。孔色/品质/特殊规则取当次来源。

## 普通目标生成

```json
{"id":100,"craft":{"base":"当前完整基底名","ilvl":60,"quality":0,"sockets":"R-R G-G","affixes":[{"id":"当前词缀ID","roll":0.5}]}}
```

当前基底/词缀池先生成最低规格，再算收益；roll是范围位置0–1，空affixes只生成白装。原生核适用、物等、互斥、数量及孔数，示例不指定游戏规格。

最终实物含模板携入，须有原件保留、有效owned或当次native craft依据。普通目标缺native:item_generation是设计缺口；配方仍要真实取得、加工手段和费用。不支持配方的类型沿实际物品原文与当前来源核规格，special_text只标来源；解析/保存/反事实文本不替代生成取得依据。

## 测量与实际变化

用户场景落实到scenario/measurement，回读config/effect_state和窗口；原件开启不说明目标时段有效。measurement的flasks_enabled:false及effects:[{skill_id,enabled:false}]在两侧改造后应用；effects设置该ID所有实例的全局效果，不改宝石enabled，关药不删实物。该判断可并入configuration:candidate。

收益比较baseline/result；许可与保留核original及materialization.actual_changes。共同测量也进入交付XML，须计入真实改动。频率、短效增益及药剂使用与实际状态一致。

`inspect --metrics`沿当前skill_ref/actor和明细context；Calcs与MAIN上下文不同就分开。probe各variant独立，组合放同一项；单变量实验配合当次规则解释原因，零差分限本对象/场景。采用候选后保存plan、evaluate/export，补路沿[原生候选](commands.md#采用原生候选)。

## 复核已有交付

先收原目标、用户原角色、作者最后XML/说明/原答，登记可追踪artifact。作者XML作只读start输入，用户原角色另inspect；空operations，仅带入实际读到的作者安排。本会话native:original是作者XML，用户原角色before取真实inspect artifact。

先核两角色的实际规格、状态、场景和原许可，再核原句支持/反例。自身materialization无差分只证明没再改作者；character身份与input_bytes/saved_bytes字节身份分开，保存重排不当玩法变化。

资源按原问题核单次、平均或准备接战斗完整窗口及相接余额。自己的判断和复核沿模板提交、export同一报告；否定/未知可完成审查，另获改进请求才转规划。
