# 角色配装与候选协议（schema_version 2）

已有 XML 的微调或整体换流派使用本文件。目录/原文查询见 [资料协议](data.md)；开始编写 claims、usage、库存、报价或最终复核时读取 [证据协议](evidence.md)。

## 用户意图 intent.json

```json
{
  "schema_version": 2,
  "confirmed": true,
  "goal": "保留当前主技能，优先改善生存",
  "objectives": [{"objective_id":"survival","text":"优先改善生存","evidence_refs":["user-direction"]}],
  "league": "用户实际说出的国服赛季",
  "allow_main_skill_change": false,
  "evidence": [{
    "evidence_id": "user-direction", "kind": "user_statement", "subject": "intent",
    "text": "替换成用户本次实际原话，不抄此占位句", "message_ref": "可用的对话位置"
  }],
  "constraint_sources": {"/goal":"user-direction", "/allow_main_skill_change":"user-direction"}
}
```

`confirmed` 记录流程，程序不独立读取聊天验证授权。每项已明确的限制在 constraint_sources 用 `/字段名` 关联 user_statement。可以一段原话对应多项真实表达，不把模型推断写成原话。

可选字段：

| 字段 | 语义 |
|---|---|
| objectives | 按用户本次要求拆分的 `{objective_id,text,evidence_refs}` 数组；依据来自真实 user_statement。遗漏目标保持草案；每项用 `objective:<id>` claim 关联本次实现、事实、条件和代价 |
| game_version / version_source / version_checked_at | 当前目标机制版本、实际官方 URL、真实查阅时间；未知不猜，交付保留缺口 |
| mode | refine / rebuild / both；未知省略/null |
| budget | `{amount,currency}`；amount 有限非负，未知省略/null |
| target_level | 明确目标角色等级；不是从文件随手复制的确认 |
| allow_class_change / allow_ascendancy_change / allow_main_skill_change | true / false / null；副升华变化也检查升华许可 |
| keep_slots | `[{slot:"本机槽位",preserve:"item_identity或exact_spec"}]`；实物加工与完整规格保留分开 |
| keep_skill_ids | 明确需保留启用的本机技能 ID 数组 |
| minimum_stats | `{本次原生指标名:有限最低值}`；缺失指标不按0 |
| max_changed_equipment | `{count,basis:"replaced_items或changed_slots"}`；更换物品或改槽位规格，按用户口径 |
| scenario | 用户明确限定的配置值；模型自行选择的共同场景放 plan.scenario |
| baseline_main_group | 用户明确指定的原角色主组，1起索引；否则沿用文件 |
| quest_passive_points | 已明确的任务点事实，不能默认全完成 |
| normal_points_available | 已明确的普通总可用点数；未知不由程序套固定奖励公式 |
| ascendancy_points / secondary_ascendancy_points | 已明确可用点数，不是必用点数；不固定8/10等赛季上限 |
| notes | 其他记录 |

未知值可先计算，只影响依赖它的判断。用户若不知精确点数，确认任务/角色进度后由模型结合当前来源评审 progress claim，不要求用户懂 JSON 或点数算法。重大方向 permission 缺口只能用真实用户回答更新 intent，不能写 claim 越过。

## plan.json：修改与决策

方向确认后用 [增量保存](commands.md#增量保存方案) 保存头部，再补操作和证据。最初可计算计划为 `schema_version:2, mode:"refine或rebuild", summary:"具体目的", operations:{...}`，evaluate 返回草案缺口及本次技能引用。完整字段是：schema_version、name、mode、summary、template_xml、operations、scenario、measurement、sources、shopping、inventory、assumptions、migration_steps、legality_notes、evidence、claims、usage、timeline、assessment。sources/assumptions/legality_notes 是说明，不自动充当已验证证据；用 claims 关联实际来源。

| operations 字段 | 格式/作用 |
|---|---|
| gems | `[{group:1,index:2,skill_id:"本机ID",level:1,quality:0,...}]`；只改指定宝石 |
| groups | `[{index:1,enabled:true,slot:"本机槽位",label:"用途",include_in_full_dps:true,main_active_skill:1,group_count:1}]`，按需写字段 |
| replace_skills | 完整替换当前技能集：`[{slot:"...",gems:[{skill_id:"...",level:1,quality:0}],...组字段}]`；原生占位空组使用 gems:[]，缺字段/非数组报错 |
| main_group | 当前主技能组的1起索引 |
| items | `[{id:正整数,text:"原生完整物品文本"}]` 或下述 craft；不改其他物品/非活动套装 |
| equip | `[{slot:"本机槽位",item_id:物品ID,active:false}]`；0 表示移除；天赋珠宝使用 `Jewel <节点ID>` |
| tree | nodes 全量或 nodes_add/nodes_remove 增量，均填写实际分配节点及连接点；class_id、ascendancy_id、secondary_ascendancy_id 从当前目录选择；masteries 全量或 masteries_add 局部映射 |
| level | 目标角色等级，由实际引擎核对保留值与使用要求 |
| weapon_set | XML 支持的1或2；这是接口格式，非武器组合策略 |
| config | 候选专有配置变化；null 显式删除。共同场景写顶层 scenario；新增值按本次原生字段与类型校验 |

`config` 与共同 `scenario` 使用同一校验：本次 `config_types` 的 check 只接受 JSON 布尔值，数值控件只接受有限数值且不接受布尔值，text 接受字符串，list 使用当前 options[].val 并区分 false 与 0。类型、合法选项缺失或出现未适配控件时返回能力诊断，不猜新赛季格式。null 删除覆盖并允许原生默认值，不等于关闭或数值 0；可以清除旧版字段。旧 XML 中未知字段原样保留并提示未覆盖，已识别字段的错误类型阻止交付。数值范围、原生截断/默认贡献与作用条件仍按当前原文和实算判断，不复制赛季常数。

`main_active_skill` 是当前物化组的主动技能选项序号，不等于宝石位置或宝石数。`inspect --calculate` 的 `calculation.skill_selections` 给出当前各组 choices（index/id/name/gem_index）；采用新组合后重新计算。非法选项或原生静默改写会返回 `native_selection_changed`、请求/实际值及 `available_options`。模型决定选哪一项，程序不自动替换；省略字段的原生默认值和无主动技能空组的默认占位可以正常保存。

宝石额外字段：enabled、enable_global1、enable_global2、count、skill_part、skill_stage_count、skill_minion。`gems[].granted_effects` 按当前原生顺序给出每个效果的 index/id/name、has_global_effect、global_enabled、operation_field、skill_types、active_skill_present。一个宝石可能提供多个不同效果；按实际 ID 和 operation_field 操作，效果序号不自带“普通/临时”含义。active_skill_present 是原生列表观察，不证明实际触发或覆盖。关闭守护收益与仍装备/触发/支付费用分开。禁用普通组不会释放 slot；确实卸下需显式 `slot:""`。

`measurement` 是模型选择的共同测量条件：`{"flasks_enabled":false,"effects":[{"skill_id":"本次 granted_effects 的效果 ID","enabled":false}]}`。它在原方案和候选完成各自改造后应用，再分别实算；开启/排除哪些效果由本次用户目标、当前描述及覆盖依据决定。effects 会设置两份角色中此 ID 的全部实例的原生全局开关，不改宝石 enabled、等级、品质或物品文本。某份没有该效果时 measurement_audit 标 absent；两份都没有时返回输入修订诊断，不能证明已排除。无已适配全局开关的效果返回具体错误，不由程序猜一个替代操作。flasks_enabled 设置当次原生类型为 Flask 的已装药剂开关，不删除实物。两份实际应用在 configuration_audit.measurement_actual，selected_main_before/after 记录测量前后主技能；共同条件改变主输出时，仍按原始主技能核对用户许可，不能用改过的基准绕开保留要求。写 subjects 含 `measurement` 的 claim 评审这些条件，并按需同时关联 `configuration:candidate`。整体换流派时，分别说明只在某份存在的效果及其意义。

`equipped[].assigned` 表示保存了槽位映射，`active` 是原生槽位开关，`socket_allocated` 是珠宝孔当次实际分配状态，`effective_in_calculation` 从原生 MAIN 的 player.itemList、flasks、tinctures 列表读取。active=false 不表示普通装备未穿戴；位置保留也不表示珠宝仍生效。模型结合用户的保留意图判断可否调整生效状态。`text` 是当前原生物品原文，角色状态摘要同时绑定这份规格。

tree.masteries/masteries_add 使用节点 ID 字符串到效果 ID 的映射；nodes 全量不能混用增量，masteries 全量不能混用局部；保留其他专精。模板路径相对 plan 所在目录，仍须核对实际来源与迁移步骤。

普通装备配方使用 `items:[{id:100,craft:{base:"当前完整基底名",ilvl:正整数,quality:非负整数,sockets:"R-R G-G",affixes:[{id:"目录词缀ID",roll:0.5}]}}]`。affixes:[] 生成无显性词缀的普通基底，非空生成稀有目标，不添虚构词缀。roll 是范围位置0–1，默认0.5；默认品质0。词缀数量、互斥、适用和孔数由本机物品对象核对，不强加3前3后。配方只覆盖原生普通词缀路径；特殊机制用实际物品文本与当前依据，不假称已模拟全部制作。

## 按需明细、变体与效率

```powershell
python $entry inspect --input '<候选.xml>' --metrics 'Life,ManaCost,Armour' --out '<工作区>/calcs.json'
python $entry inspect --input '<候选.xml>' --metrics 'Life,CombinedDPS' --skill-ref '<active_skills 中的完整引用>' --actor minion --out '<工作区>/minion-calcs.json'
python $entry probe --run '<工作区>/会话' --name proposal-a --variants '<工作区>/variants.json' --metrics 'Life,CombinedDPS,ManaCost' --out '<工作区>/comparison.json'
python $entry probe --run '<工作区>/会话' --name proposal-a --node-ids '<本机节点ID,另一个ID>' --out '<工作区>/node-screen.json'
```

`inspect --metrics` 自动计算。`calculation.breakdowns[]` 返回实际 `metric/skill_ref/actor/value/detail/context`，context 绑定输入 XML 摘要、引擎指纹和实际配置。默认当前主技能及其主体；可选 player/minion/mirage。Life 等明细直接保留原生公式文本或表格。`available_metrics` 是此主体实际提供的明细键，不是技能合法性表。

Calcs 使用输入角色的主技能状态、EFFECTIVE 模式与战斗配置。`main_value` 在可匹配时给出 MAIN 观测；`matches_main_value=false` 或 `different_context` 不能用于解释 MAIN 数值。`unsupported` 表示缺少此明细/主体，`partial` 表示原生结构存在未覆盖或截断，不自行补成公式。不同主体不可互相借用回复或伤害。没有明细时，可用只改变一个明确输入的 probe 观察结果，但只能说明该输入在此角色和场景中的影响。

`variants.json` 仅含 `variants` 数组，各项为 `{"id":"自定唯一标识","operations":{...}}`。operations 与 plan 共用同一套装备、宝石、天赋、配置、配方接口，不写第二套变化格式。每个变体独立应用到指定 candidate.xml；需要组合时把所有变化放在同一项，禁止将单项增益简单相加。旧 `--group/--index/--ids` 是生成同样变体的单位置简写。自动分批，没有固定候选总数。

每项返回完整原生实算后的指标、分配点数、错误、资源账目与缺口；输入不合法的单项单独报错。所有结果都是 `screened` 或诊断 `blocked`，不代表预算、获取、最终目标已经通过。变体不修改候选文件；选定后写回完整 plan/evaluate/export。`--node-ids` 使用原生 getNodeCalculator，只筛未分配且未被装备授予的节点词缀，不包含连接路径/点数合法性；用完整路径组成变体再次实算。

未保留节点的 tree_diagnostics 从实际 XML 查询 path；程序在隔离的当前角色中调用原生 `PassiveSpec.AllocNode` 顺序补路，再完整实算并 SaveDB 冷回读。`diagnostic.connection_proposal.preview` 给出组合后的实际普通/主升华/次级升华点数、声明上限、全部数值差异和资源检查，保留原树操作的职业、升华、已请求节点与专精。共享路径只计一次；原生珠宝和特殊连接规则参与，顺序补路不证明全局最优。显式 nodes_remove 冲突、目标互斥/不可达、未知节点或预览失败明确报告，保留原目标供重新设计。已知超点提案不能直接采用；未声明可用点数保持未知。模型审查影响后按[命令协议](commands.md#采用原生候选)引用已保存提案，程序直接应用，再 evaluate/route 重新检查全部目标。

宝石需求错误的 `native_gem_requirements_unmet` 列出当前等级/属性缺口、原生逐级要求和当前属性可用的自然等级。`change_proposal` 只预览这一颗延期升级到当前可用最高等级的实际收益/损失和费用；模型可以选择补属性、延后或换技能。接受沿用同一提案引用入口，不改其他宝石。当前属性不足以认证取得、自然训练或实物降级；这些成本与其他用户目标仍需在完整评估中核实。未知需求或没有可用等级时保留缺口。

`--out` 的比较文件放在会话目录外的普通任务工作区，避免覆盖候选。模型读取需要的行，勿一次输出大矩阵导致平台转存到内部目录。

## 复核已有交付

用户提供原角色、待复核 XML 与作者说明时，先分别 inspect --calculate 保存当前原生观察；作者说明是待验证主张。依次核对实际对象与开关、数字是否可复现、因果是否成立、用户各项要求是否满足。相同数字或 XML 可回读不替代后两项。用当前 config_controls 和原文区分物品授予技能、配置实际开启与效果覆盖；某个技能探针无变化不能直接判为“引擎未实现”。击杀时间还需要对应敌人生命、伤害阶段等依据，不能仅由 DPS 宣称。

已有 XML 的受控实验直接用 `probe --input '<待复核.xml>' --variants '<工作区>/variants.json' --metrics '<本次指标>' --out '<工作区>/probe.json'`。它复用同一 operations、原生批量计算和费用检查，不写源 XML、不建立新候选或认证交付；按返回 baseline_effect_state 与各变体 effect_state 核对实验实际改变了哪个开关。不要自行拼接 Config/Calcs、正则改 XML 或另写临时变体生成器。候选会话仍用 probe --run/--name；两种来源进入同一实算路径。需要改进交付时，用完整 plan/evaluate/export，由原生 SaveDB 生成 XML；仅审阅通过可保留原 XML，并明确其已核对范围与未解决项。

引擎查询/计算自动缓存到当前用户临时目录 `poe-adjust-cache`，仅含可丢弃 JSON、总量受控；不用用户维护。键包括安装位置、所有相关引擎/适配器内容指纹、完整 XML 和参数。命中前检查结果摘要；损坏、缺失或内容更新时重算。最终导出、回读和 doctor 跳过结果缓存重新启动原生引擎。命令的 `runtime` 分别报告 requests、worker_calls、cache_hits、worker_seconds、elapsed_seconds；缓存命中不算新一次原生计算，也不刷新网页或市场报价的真实查阅日期。
