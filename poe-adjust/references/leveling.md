# 开荒与成长路线（schema_version 2）

无完整角色用route，已有XML/纯分享码用[配装](build.md)。[资料](data.md)查当前选项与来源，[证据](evidence.md)管判断/资源/自审，[命令](commands.md)管保存/恢复。单路线为顺序stages，不同方向独立候选。

## 先算最低终点，再连接起点

先在同一route填写终点完整snapshot及按原目标选定的entry_checks：装备、天赋、技能、场景与实际节奏达到本次最低条件，再连接原起点、取得前状态和训练。最低规格本次落实到实物；更高提升态另列。

沿用户实际起终点、交易/SSF、物资、预算、偏好和操作选择，不把未知补成禁令。阶段按目标与切换决定，不套固定章节/数量/统一生存门槛。关键切换应从当时实际状态能进入下一状态；尚未取得的已设计实物是条件，泛称“以后补属性/生存/装备”仍是设计缺口。

取得先读research --gems同一宝石的真实行，核职业、任务前提、NPC、奖励选择数量与费用。Quest Reward占位不是具体领奖人；不清楚就核可用商店、解锁和通货。核前段能到达该来源，缺关键物资有能实际使用的前段/低规格退路。国际费用和国服未知分开，小号/交易沿用户许可。trials位置不证明通关或点数。

普通装备沿[生成协议](build.md#普通目标生成)从可达物等与最低规格生成，取得、打孔/连接、宝石训练各核实际手段；给定实物未拥有可以等条件，不能让玩家再替你设计必要规格。用户“不玩召唤”按原意判断，不机械禁止所有minion标签。

## 阶段与物理实例

字段取当前input_contract。顶层登记原goal/objectives、职业/升华、真实约束与evidence/claims/assessment；未知赛季留明示未知，不猜game_version。每段stage_id稳定，name/level/act/completed_act与实际角色状态对应，前往章节不代替任务已完成。

skill_groups落实际slot、enabled和各宝石instance_id/skill_id/level/quality；同组同物理连线，停用仍占孔，存放组空slot且停用。bases对应装备布局（null为空、省略未知）；gear/passives/transition/resources说明当前设计、顺序、费用与缺物资退路。ascendancy_nodes/points为真实累计目标与可用点，连接也占点；progress_limits上界不由自填点数扩大。

同颗宝石跨段沿用instance_id，另颗另实例，换skill_id新实例。level:auto仅明确请求时选本机自然可训练且角色可用最大值；实际经验/取得另作条件。自然升级用training:<stage>:<instance>判断，其他规格变化用transformation与成本。同一理由可关联多个真实subject。

required_items记录首次取得的实物/加工：gem用稳定实例、当前skill_id、level/quality/quantity、acquisition及同一来源的章节/任务/NPC/class_ids/evidence_refs；item用spec_sha256，service用service_id，规格摘要取material_specs。沿用不重购，owned取真实用户对应实例/规格及quantity声明，XML item_id不是跨段身份。

entry_conditions关联含stage:<id>的claim；entry_checks用{check_id,locator,op,value,claim_id}，对照本段快照，缺量unknown、未达unmet。每段前后实际装备、配置、进度、训练及费用相接，而非只在文字中换形态。

## 关键切换的原生快照

承担推进或关键退路的状态标key_transition并提供完整snapshot。不固定每章都算；选择受进度、技能、预留、属性、防御或资源变化影响的关键状态。无快照只说明已有类型/路径/来源，不编DPS/生命/抗性/续航。关键对象仅技能演示不能承担整阶段可用结论。

snapshot省略template_xml则由原生空职业生成；operations复用配装items/equip/tree/config/main_group/weapon_set。等级与基础技能组由stage拥有，gems/groups只补count/part等状态或主主动选项，不暗换阶段规格/位置。合法选择取物化skill_selections，实际装备与bases及截至本段required_items一致，模板携入也核生成依据。

assumptions为当前claim_id数组，正文留claims；本段明确引用的判断由程序绑定阶段。usage/timeline用本段active_skills引用，必要时先route读取。metrics取实际stats字段，默认缺值未知、显式缺值列待办。configuration_context区分种子Input、覆盖与resolved_values，按该段真实进度选场景并检查实际值，终点场景不借给前段。

完整资源窗口按[动作与资源](evidence.md#动作与资源)设计；降频后用实际节奏解释伤害。窗口结束的补给/退出/前段承接可执行，未核补给不重置满池。必要时审原生补路/延期preview后沿[现有入口](commands.md#采用原生候选)采用，重跑核目标。

## 复核与交付

route --invoked --file '<工作区>/route.json' --out '<工作区>/路线.html'生成当前诊断、复核、阶段计算与恢复状态。按errors/stage_summary/next_actions修源；native_calculated只说明算了，blocked继续修。

先对照原终点和实际阶段对象，从起点沿previous_stage_id走到终点，核required_items/instance_transitions、装备、技能、升华、配置、费用与条件失败退路。目标/阶段/取得/训练等用当前required_subject关联同一判断；实际没装入的对象不解释为已生效。

对象与全文一致后才沿[最终复核](evidence.md#最终复核与交付)单独保存assessment.review、重跑同一route。交本次成功返回的HTML、证据和实际XML；快照/导出不证明玩家已取得、训练或通关，真实执行条件继续保留。诊断骨架和只到中段的方案不能宣告原全程目标完成。
