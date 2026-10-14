# 开荒与成长路线（schema_version 2）

没有完整角色时用 route；已有 XML 或完整纯分享码用[配装](build.md)。[资料](data.md)查当前数据，[证据](evidence.md)写判断、动作和复核，[命令](commands.md)保存/恢复。单一路线为顺序 stages，不同方向用独立候选。

## 从可达起点设计

复用用户实际起终点、交易/SSF、物资、预算、职业/技能偏好和操作要求；未知不默认为零预算、全新开荒或禁止某玩法。阶段数量、等级和切换时机由目标决定，同级可因任务/取得分段，不套固定剧情模板。

当前 classes/宝石/基底/连线/节点决定合法对象，国服机制版本另读真实来源。research --gems 给逐级要求与取得表候选；匹配字段原样取同一行。奖励行的npc=Quest Reward只是渠道标签，具体宝石、任务阶段和领奖人需同一来源对应，不能用任务末尾NPC推全部奖励。对应不清时选已核商店渠道，核解锁、费用与前段承接，不凭记忆补领奖人。列最小通货及缺通货退路，国际费用与国服未知分开；小号/交易按用户许可。trials 页面位置不证明通关或点数，升华计连接节点。

先物化原目标终点的最低整套配置，再连接起点与必要切换，最后添可选提升。每次切换核装备/连线/属性、取得或加工、前段能否到达来源及缺物资的实际退路。进入异界的物资不依赖尚不能完成的异界内容；原生配方合法不证明取得。未知实物/进度/训练留条件，最低规格、来源和过渡在本次落实。

普通目标按[配装的生成协议](build.md#普通目标生成)从可达物等与低规格起生成，不能用未核文本投影代替普通依据。已有物资且接受取得后切换不强制普通备用宝石；其他等待需可用前段承接。用户不玩召唤按原意解释，不机械禁全部 minion 标签。

## 阶段与物理实例

顶层与 stage 字段从当前 input_contract 发现。route 常用 schema_version:2、confirmed:true、name/goal/league/scope、当前 class_id/ascendancy、objectives、stages、evidence/claims/constraint_sources/assessment。交易、预算、allow_minions/allow_mule 等只按真实用户表达填写；未知赛季明示未确认，不猜 game_version/version_source。

| stage 内容 | 用法 |
|---|---|
| stage_id/name/level | 稳定唯一 ID、名称、实际/目标等级；auto 需要已知角色等级 |
| act/completed_act | 当前前往章节与已完成章节分开，不代替具体任务完成 |
| skill_groups | `[{name,slot,use,enabled,gems:[{instance_id,skill_id,level,quality,enabled}]}]`；同组为同一物理连线，use 自由描述 |
| bases/weapon_setup | `{当前槽:完整基底名}`，null 是无装备，省略是未知；布局说明自由 |
| gear/passives/checks/transition/resources | 当前实际设计、顺序、检查、过渡与费用范围，文字不代替实算 |
| ascendancy_nodes/ascendancy_points | 累计目标节点及已声明可用点；普通/次级点数同样依实际进度评审 |
| required_items | 首次取得的实物/加工需求，沿下节稳定实例与规格 |
| entry_conditions | 条件 claim_id 数组，subjects 含 stage:<id> |
| entry_checks | `{check_id,locator,op,value,claim_id}`；locator 指本阶段实际字段如 /stats/FireResist，op/value 按当前目标选择 |

选定的可计算进入条件写入 entry_checks，对照本段 snapshot；缺量为 unknown，未达为 unmet。点数读当次 progress_limits 的保守上界，再核真实取得；自填可用点数不扩展上界。bases 对应实际布局；启用组分配真实槽位，停用仍占孔，空 slot 仅存放停用组。练级辅助不假称伤害辅助。

同一颗跨阶段沿用 instance_id；两颗同名宝石两实例，换 skill_id 用新实例。level:auto 仅明确请求时选当次可自然训练且角色等级可用的最大值，不代表最优/已练/已取得。自然等级上升且品质不变，写 training:<stage>:<instance> 判断说明经验、时点与进入前条件；其他规格变化写 transformation 判断及成本。

宝石 required_items 形式：`{kind:"gem",instance_id:"稳定实例",skill_id:"当前ID",level:1,quality:0,quantity:1,acquisition:"vendor",available_from_act:1,quest:"所选行任务",npc:"所选行npc原值",class_ids:[当前职业ID],evidence_refs:["真实来源ID"]}`。仅是格式，内容从研究取。取得可早于使用；具体步骤/角色、选择和费用由 acquisition:<instance> 判断解释，不把渠道占位当真实NPC。owned 的 user_statement subject 匹配物资，context 含 instance_id 与 quantity。

装备用 kind:item/spec_sha256，服务用 kind:service/service_id；实际数量正整数。阶段 material_specs 提供摘要，不手算。XML item_id 不属跨阶段身份。首次取得只登记一次，沿用不重复采购；加工/新实物另记。已匹配 owned 可直接用库存依据，不再抄一份数量。

## 关键切换的原生快照

没有完整快照只能报告类型/等级/资源基础、布局、路径与来源，不编精确 DPS/血量/抗性/续航。按目标所依赖的进度、预留、属性、防御与资源变化选择关键阶段，标 key_transition:true 并提供 snapshot，不固定数量或每章都算。用于验证推进的快照落实当时可达的装备、天赋和技能；仅技能演示不能承担整阶段可用结论。实际任务点取得与训练时点对应本段分配，精确目标与实际操作一致，手动技能不隐藏费用。

资源窗口结束后安排能实际执行的降频、补给、退出或前段承接。补给未核不能把下一窗口重置满池；若切换改变预留或其他关键依赖，核新的整套状态及前后进入条件，终点数字不能补证未算的中段。

snapshot 可选 template_xml，省略由当前原生空职业角色生成，不手写 XML。operations 复用配装 items/equip/tree/config/main_group/weapon_set；level 与基础技能组由 stage 拥有。operations.gems 只补 count/part 等状态，operations.groups 只补 main_active_skill/include_in_full_dps/group_count，不能偷偷换阶段技能/等级/位置。合法主主动选项从物化 skill_selections 取。

assumptions 为含 snapshot:<stage> 的 claim_id 数组；usage/timeline 用本阶段计算的 skill_ref，必要时先 route 读 active_skills 再补。metrics 仅取实际输出。configuration_context 区分种子 Input、显式覆盖和 resolved_values：种子场景不证明适合此剧情/目标，模型据当前原文与进度选择。

实际装备与 bases 对齐，并出现于截至该阶段的 required_items；当前生成规格、点数、孔位/属性、资源和目标整体核对，模板遗留也包含。native_calculated 只说明算了，blocked 仍修。补路/延期先审 preview 后[采用原生候选](commands.md#采用原生候选)，重跑并核目标/代价。该阶段 xml_path 可作为目录上下文，不能借另一阶段成本。

## 复核与交付

`route --invoked --file '<工作区>/route.json' --out '<工作区>/路线.html'` 生成中文 HTML、evidence/review/state 和实际计算阶段的 XML/evaluation。先读 errors/stage_summary/delivery_brief/next_actions，沿当前 reference_contract 引用 catalog、stage 与资源根。修源保留用户要求，不把删成单宝石的诊断文件当完整路线。

按 previous_stage_id 从起点走到终点，核 required_items、instance_transitions、实际装备/配置/进度、费用和条件失败时退路能否执行。每目标 objective:<id>、阶段 stage:<id>、取得/训练/进度等用当前所需 subject 关联，不重复造文字。未装入的天赋或装备不能解释当前面板。

自己按当前 review_template 审全部目标/判断，在原 route.assessment.review 单独保存后重跑。未来条件用 condition_reviews，原生合法值/生成依据/可修字段错误先修。task_completion 表示本次规划是否完成，character_readiness 保留实际条件；文件导出不证明玩家已备齐、训练或通关。实际 XML/回读/摘要和状态一致才交付，恢复沿[命令](commands.md#恢复与交付完整性)。
