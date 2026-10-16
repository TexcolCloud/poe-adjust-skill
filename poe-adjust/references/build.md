# 角色配装与复核（schema_version 2）

已有角色用本协议；[资料](data.md)、[证据](evidence.md)、[命令](commands.md)按需查，字段沿input_contract。

## 只读解释

快照数值及已返回投影直接用inspect/probe事实页。另证机制因果或只讨论配装取舍用start/intent/evaluate/export，intent.task_kind为read_only_review，plan用refine及空operations，保留被审对象；实验另用probe。intent含实际goal/objectives、user_statement及constraint_sources，本机范围可留league/version未知。

先核实际对象与当次规则的关键前提，再判断支持、反证与未知。禁用判断若与实际计算冲突，查该字段用途及当前规则来源。完整否定/未知可完成审查，原角色问题留character_readiness；export仅报告和核验。作者复核见[复核已有交付](#复核已有交付)。

## 规划交付

未来物资未拥有可列条件；最低规格、来源、使用和失败退路须本次设计完成，缺项记unimplemented，见[最终复核](evidence.md#最终复核与交付)。

默认delivery_scope为execution_plan。仅用户明确要局部原生快照比较、无需重做整套操作时可用snapshot_comparison：constraint_sources关联真实原话，delivery_scope判断同时引用用户依据与native:materialization#/actual_changes。程序仅排除完全未改非主组的usage，改动组、真实错误、目标及原角色状态照常核验。

## 意图与方案

intent保存原话决定的目标、场景、许可、预算与保留范围，constraint_sources关联真实user_statement；confirmed仅流程记录。未知省略/null。目标为{objective_id,text,evidence_refs}，目标等级/可用点数按实际进度，progress_limits只是当次保守上界。

plan最小结构为{"schema_version":2,"mode":"refine","summary":"本次目的","operations":{}}；rebuild同接口，可选相对plan目录的template_xml，说明来源与迁移。操作字段及类型沿input_contract：

- gems/groups用1起组序号与宝石index；replace_skills写最终组，撤下组清空，存放组空slot且停用。停用但仍插着的宝石占孔且需物资。
- main_active_skill是物化后主动选项序号，不是宝石位置；取当前skill_selections。granted_effects的实际ID/operation_field/global_enabled决定开关，effect序号或列表存在不证明持续。
- items写[{id,text}]或下节craft，id为正整数；equip写[{slot,item_id}]，item_id:0移除，珠宝槽为Jewel <节点ID>。tree改节点/专精/职业升华；config按当前控件类型、选项与原文选值，null删除覆盖、false/0/默认分开。
- 移动实例先移原位置再放新位置，核实际装备孔数/连线、属性和生效槽。孔色、品质与特殊规则按当次资料解释，不套旧版本常识。

## 普通目标生成

```json
{"id":100,"craft":{"base":"当前完整基底名","ilvl":60,"quality":0,"sockets":"R-R G-G","affixes":[{"id":"当前词缀ID","roll":0.5}]}}
```

新增普通目标先用当前基底/词缀池的craft落实最低规格，再计算收益；物等/孔连取可达来源。示例仅格式，roll为范围位置0–1，affixes:[]仅普通白装。原生核适用、物等、互斥、数量和孔数。

最终全部实物（含模板携入）有原件保留、有效owned或当次native craft依据。普通目标缺native:item_generation是设计缺口，不能放进未来条件。合法配方仍需真实取得/加工步骤与费用；配方不支持的类型按本机实际物品原文、当前来源核规格，special_text只标来源。解析、保存、legality判断或probe反事实文本都不替代生成/取得依据。

## 测量与实际变化

先将玩家要求的场景与操作落实到scenario/measurement，核实际config/effect_state及改变关键结论的当前来源；原件开启不代表所需时段有效。measurement可设flasks_enabled:false及effects:[{skill_id,enabled:false}]，在两侧改造后应用；效果设置所有该ID实例的原生全局开关，不改宝石enabled，药剂关闭不删除实物。measurement判断可合并configuration:candidate。

同条件收益比较baseline/result；保留范围比较original与materialization.actual_changes的object_before/object_after。交付XML包含共同测量改动，不能用“仅比较”隐去对用户角色的变化。主动技能、短效增益与药剂的执行频率/窗口也须与实际开关一致。

inspect --metrics用当前skill_ref/actor；Calcs明细的context与MAIN不同时不互借公式。probe每个variant独立应用，组合放同一项实算；联合变化只支持联合观察，零差分只说明本对象/主体/场景/指标。节点筛选不证明完整路径合法，采用后保存plan并evaluate/export。补路/延期沿[原生候选](commands.md#采用原生候选)。

## 复核已有交付

先收到真实原目标、原角色、作者最终XML/说明和原答；按实际最后交付锁定对象。把作者原句、原要求和原文件登记为可追踪artifact，原文是待核主张。作者XML作只读start输入、原角色inspect；plan空operations，作者安排仅带入确实读到的内容，缺失留未知。所有新文件写审查工作区。

先比较原角色与作者的真实规格、状态、场景和保留许可，再核原句范围及其支持/反例。审查自身materialization无差分只说明没再改作者；角色身份用character，字节身份用input_bytes/saved_bytes，XML保存重排与角色变化分开。

资源分别核单次、平均投影及原要求的准备/战斗完整窗口，携带相接余额，DPS不作击杀时长。实验用probe及当前控件原文。自己的claims与当前复核沿模板提交、export同一HTML；否定/未知可完成只读任务。用户另要改进时才转规划落实。
