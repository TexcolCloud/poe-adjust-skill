# 命令与文件协议（schema_version 2）

所有 JSON 为 UTF-8，使用实际数字、布尔值和数组。文件路径用 PowerShell 单引号；通过文件参数传 JSON。每次独立工具调用重新定义变量，或直接使用绝对路径。Python 使用 TeleAgent 已注入的运行时。

## 命令入口

```powershell
$entry = '<技能目录>/scripts/poe.py'
python $entry doctor
python $entry start --invoked --input '<角色.xml>' --run '<工作区>/会话'
python $entry intent --run '<工作区>/会话' --file '<工作区>/intent.json'
python $entry evaluate --run '<工作区>/会话' --name proposal-a --plan '<工作区>/plan.json'
python $entry probe --run '<工作区>/会话' --name proposal-a --group 1 --index 2 --ids '<本机skill_id,另一个skill_id>' --level 1 --quality 0
python $entry export --run '<工作区>/会话' --name proposal-a
python $entry status --run '<工作区>/会话'
python $entry inspect --input '<角色.xml>' --calculate --out '<工作区>/diagnosis.json'
python $entry decode --input '<实际纯分享码.txt>' --out '<工作区>/template.xml'
```

所有子命令接受 `--pob '<PoECharm/内部PoB目录或程序>'`，正常自动发现。start 已做运行自检，目录须为空或不存在，之后始终使用同一 run。inspect/decode 不覆盖输入。网址与 HTML 不是纯分享码；有需要才从实际公开来源取得完整分享码，不抄写或拼接被截断内容。

start 返回原始 overview 与检查文件路径；`inspect --calculate` 返回完整 calculation，保存到文件时终端只显示诊断摘要。inspection 的原 XML 字段是 `skill_groups[].gems[].skillId`；计算结果是 `gems[].id`。数字以实际 stats 为准，Build/PlayerStat 只是缓存。CountAllocNodes 的 points/ascendancy_points 已含连接小点，不能从已分配量反推已完成的任务。

旧 schema 会话只读追溯。请用新空目录 start，从旧记录提取有依据的意图，以新格式重建；不可直接 export。未确认偏好不从旧默认 false 迁移。

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

`--out` 的比较文件放在会话目录外的普通任务工作区，避免覆盖候选。模型读取需要的行，勿一次输出大矩阵导致平台转存到内部目录。

引擎查询/计算自动缓存到当前用户临时目录 `poe-adjust-cache`，仅含可丢弃 JSON、总量受控；不用用户维护。键包括安装位置、所有相关引擎/适配器内容指纹、完整 XML 和参数。命中前检查结果摘要；损坏、缺失或内容更新时重算。最终导出、回读和 doctor 跳过结果缓存重新启动原生引擎。命令的 `runtime` 分别报告 requests、worker_calls、cache_hits、worker_seconds、elapsed_seconds；缓存命中不算新一次原生计算，也不刷新网页或市场报价的真实查阅日期。


## 当前本机目录与资料

```powershell
python $entry catalog --kind classes --query '' --limit 20 --out '<工作区>/classes.json'
python $entry catalog --kind rules --query '' --limit 30 --out '<工作区>/rules.json'
python $entry catalog --kind gems --query 'Fireball' --limit 8 --out '<工作区>/gems.json'
python $entry catalog --kind skills --query 'Embrace Madness' --limit 3
python $entry catalog --kind links --query 'Fireball,FreezingPulse,SupportAddedLightningDamage' --limit 1
python $entry catalog --kind bases --query 'id:Iron Gauntlets' --limit 2
python $entry catalog --kind affixes --query '<完整基底名>|maximum Life' --limit 20 --out '<工作区>/affixes.json'
python $entry catalog --kind nodes --input '<角色.xml>' --query 'life' --limit 20 --offset 0 --out '<工作区>/nodes.json'
python $entry catalog --kind nodes --class-id <本机职业ID> --query 'life' --limit 20 --out '<工作区>/start-nodes.json'
python $entry catalog --kind nodes --query '<完整升华英文名>' --limit 100 --out '<工作区>/ascendancy.json'
python $entry catalog --kind config --query 'enemyIsBoss' --limit 3
python $entry catalog --kind metrics --input '<角色.xml>' --query 'MaximumHitTaken' --limit 30 --out '<工作区>/metrics.json'
python $entry catalog --kind uniques --query '<名字>' --limit 3 --out '<工作区>/uniques.json'
python $entry catalog --kind translations --query '盾' --limit 12
python $entry catalog --batch '<工作区>/queries.json' --out '<工作区>/catalog.json'
python $entry research --gems '<skill_id,skill_id>' --out '<工作区>/gem-facts.json'
python $entry research --version --league '<用户目标赛季>' --source '<官方公告URL>' --out '<工作区>/version.json'
python $entry research --trials --out '<工作区>/trial-facts.json'
```

批量文件：`{"queries":[{"kind":"gems","query":"id:Fireball","limit":2,"offset":0}]}`。支持上述原生 kind，translations 单查。单页 limit=1–100，按返回 next_offset 继续；程序自动分批，不限制整体候选/路线数量。长结果使用 --out 后选择相关字段读取，不把整份大 JSON 打到工具输出。多次分批前后指纹须一致。

- classes 返回当前职业与其主升华 ID；rules 含副升华目录、slots、规则来源及 `meaning/status`。observed 不一律表示游戏上限：engine_state 是本次状态，native_fact 是原生事实，engine_constraint 是接口约束，unsupported 是没读到可靠规则。不能把建议/默认值当合法上限。
- bases 返回当前基底 `sockets/valid_slots/affix_limits/requirements`；这是基底原生容量，实际物品可能有独特改变，完整角色需实算。affixes 返回当前物品词缀池、互斥组及物品等级要求。
- gems 的 `natural_max_level`、`level_requirements[].naturally_trainable` 与有效数据级别分开。可计算不证明对应裸宝石可获得。quality 不设复制的统一上限，取得方式仍须依据。`base_resources` 是该等级基础费用/保留/辅助倍率，不能代替完整连线续航。
- links 接受有序宝石 ID，可以多个主动及重复 ID。relations 用 `active_index/support_index/applied` 区分输入位置；valid 仅表示类型查询成功，incompatible 是未起作用的辅助，不证明物理安装、触发、费用或冷却正确。
- config 返回原生类型、枚举 options[].val、标签及可读取默认值；用 val 写入，不能用显示 label 替代。最终实际条件在计算结果 config_values。
- metrics 需要真实 XML，列出当前实际数字指标。minimum_stats 和 comparison 不受中文显示名称表限制；未知指标保留缺口。
- 普通节点 path/points_to_allocate 只有真实 XML 或 --class-id 上下文才提供，两者互斥。不带上下文时只查目录，不能用默认角色路径。当前 path 从目标回溯到既有连接处；反序是加点顺序，多个路径须合并后重新计点。
- 完整升华目录可按 linked 与起点核对路径；完整单次查询另含 ascendancy_path/path_cost，截断页不认证整张图。授予天赋在 granted_passives；已在树上分配不能再算一份收益。anoint_recipe 按本机清单保留重复材料和本机译名，不猜固定配方。
- research 保留原生事实与网页事实的不同范围。宝石 choices 是当前页面实际任务/NPC/章节/职业行，不代表角色已完成或国服无差异。试炼只返回观测位置/区域等级，不按表格顺序推导奖励点数、解锁或玩家进度。版本正文未确认即 unverified；历史链接只是查询种子，不能从赛季编号推算版本。

doctor/start 的指纹覆盖计算代码、数据、适配器和译名；data_version_basis 说明本机规则版本依据。version_source_hint 只是未核实的历史公告搜索线索，不证明当前赛季或用户区服；research --version 实际读取正文后才返回 version_evidence。软件版本相同但内容变化仍使旧结果失效。接口不兼容明确报告，不自动修改 PoECharm。操作只要求自己需要的能力，不以守护接口缺失否定职业目录。

## 本机原文只读查询

```powershell
python $entry data --mode list --limit 30 --out '<工作区>/data-index.json'
python $entry data --mode list --path 'engine/Data' --limit 40 --offset 0
python $entry data --mode search --path 'engine/Data' --query 'totem' --limit 15 --out '<工作区>/matches.json'
python $entry data --mode search --path 'translations' --query '图腾' --limit 10
python $entry data --mode read --path '<上一步返回的source>' --line <实际行号> --limit 30 --out '<工作区>/passage.json'
```

单页最多 200 条/行，搜索为字面匹配；用 next_offset/next_line 翻页。默认范围是公开代码/资料/简体中文翻译和本机元数据声明的最新标准树；指定 `engine/TreeData/<返回的版本>` 可读其他树，不能当成本赛季规则。搜索 excerpt 可能截短，truncated 明示；采用结论前 read 上下文。read 返回确切行号、原文、文件字节 SHA256 和 evidence_template，补 evidence_id/subject 后放入 evidence 即可。验证时重新读取相同行、比较文件摘要；更新后旧引用变成 unresolved。

先 catalog 查具体对象和合法选项，只有缺信息才搜索原文，不把整个目录塞入上下文。以下是定位提示，文件有变以当次 list 为准：

| 需要核对 | 本机资料位置（相对 engine/，翻译除外） |
|---|---|
| 宝石标签、等级表、品质、技能描述与辅助限制 | Data/Gems.lua、Data/Skills；用 catalog gems/skills/links 优先 |
| 传奇、基底、词缀、物等、药剂参数 | Data/Uniques、Data/Bases、Data/ModExplicit.lua、Data/ModFlask.lua、Data/EnchantmentFlask.lua |
| 天赋、专精、连接与效果 | TreeData 下实际版本；catalog nodes 优先；translations/tree_dn.csv、tree_sd.csv、tree_rt.csv 辅助对照 |
| 精华、工艺台、腐化、势力隐性与附魔 | Data/Essence.lua、ModMaster.lua、ModCorrupted.lua、ModEldritch.lua、ModVeiled.lua、BeastCraft.lua 等；不等于完整制作步骤或解锁成本 |
| 星团、纹身、永恒珠宝 | Data/ClusterJewels.lua、TattooPassives.lua、TimelessJewelData；压缩/二进制种子表只列目录，不伪装成文本解析或已模拟 |
| 角色成长、资源、敌人基础与洗点表 | Data/Misc.lua、Data/Costs.lua；倍率/权重不是角色最终值 |
| 召唤物、灵体与技能 | Data/Minions.lua、Data/Spectres.lua、Data/Skills；分清基础倍率、主体和最终输出 |
| 首领技能与地图词缀 | Data/Bosses.lua、BossSkills.lua、ModMap.lua；只覆盖内置项目，描述存在不代表 apply/计算已实现 |
| 万神殿、盗贼、战斗条件与默认值 | Data/Pantheons.lua、Modules/ConfigOptions.lua；默认场景由模型按实际进度核对 |
| 费用、触发、回复、减伤与伤害实现 | Modules/Calc*、ModParser.lua、SkillStatMap.lua、相关 Classes；读实现只作机制依据，完整收益用实算 |
| 中文效果与交易词条对应 | translations/statDescriptions.csv、Items*.csv 等，Data/TradeSiteStats.lua；不是国服实时行情 |

本接口不开放 Settings、Builds、任意绝对路径或跳出范围的链接；文本只读不执行。结果仅写任务工作区，安装目录与 PoECharm 资料目录受写入检查。它不是 TeleAgent 其他工具的系统级沙箱。目录授权被拒绝后不能换工具绕行，资料缺失就保留缺口。国服当季取得、完整剧情奖励/商店/试炼路线及实时价格仍要联网核实。

## 用户意图 intent.json

```json
{
  "schema_version": 2,
  "confirmed": true,
  "goal": "保留当前主技能，优先改善生存",
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

最初计划可先只有 `schema_version:2, mode:"refine或rebuild", summary:"具体目的", operations:{...}`，evaluate 会返回草案缺口及本次技能引用。完整字段是：schema_version、name、mode、summary、template_xml、operations、scenario、sources、shopping、inventory、assumptions、migration_steps、legality_notes、evidence、claims、usage、assessment。sources/assumptions/legality_notes 是说明，不自动充当已验证证据；用 claims 关联实际来源。

| operations 字段 | 格式/作用 |
|---|---|
| gems | `[{group:1,index:2,skill_id:"本机ID",level:1,quality:0,...}]`；只改指定宝石 |
| groups | `[{index:1,enabled:true,slot:"本机槽位",label:"用途",include_in_full_dps:true,main_active_skill:1,group_count:1}]`，按需写字段 |
| replace_skills | 完整替换当前技能集：`[{slot:"...",gems:[{skill_id:"...",level:1,quality:0}],...组字段}]` |
| main_group | 当前主技能组的1起索引 |
| items | `[{id:正整数,text:"原生完整物品文本"}]` 或下述 craft；不改其他物品/非活动套装 |
| equip | `[{slot:"本机槽位",item_id:物品ID,active:false}]`；0 表示移除；天赋珠宝使用 `Jewel <节点ID>` |
| tree | nodes 全量或 nodes_add/nodes_remove 增量；class_id、ascendancy_id、secondary_ascendancy_id 从当前目录选择；masteries 全量或 masteries_add 局部映射 |
| level | 目标角色等级，由实际引擎核对保留值与使用要求 |
| weapon_set | XML 支持的1或2；这是接口格式，非武器组合策略 |
| config | 候选专有配置变化；null 显式删除。共同场景写顶层 scenario，未知字段保留未覆盖 |

宝石额外字段：enabled、enable_global1、enable_global2、count、skill_part、skill_stage_count、skill_minion，沿用当前本机语义。关闭守护收益与仍装备/触发/支付费用分开。禁用普通组不会释放 slot；确实卸下需显式 `slot:""`。

tree.masteries/masteries_add 使用节点 ID 字符串到效果 ID 的映射；nodes 全量不能混用增量，masteries 全量不能混用局部；保留其他专精。模板路径相对 plan 所在目录，仍须核对实际来源与迁移步骤。

普通稀有配方使用 `items:[{id:100,craft:{base:"当前完整基底名",ilvl:正整数,quality:非负整数,sockets:"R-R G-G",affixes:[{id:"目录词缀ID",roll:0.5}]}}]`。roll 是范围位置0–1，默认0.5；默认品质0。词缀数量、互斥、适用和孔数由本机物品对象核对，不强加3前3后。此助手配方只覆盖原生普通词缀路径；特殊机制用实际物品文本与当前依据，不能假称所有制作都已模拟。

## 证据、结论与使用方式

每条 evidence：`evidence_id/kind/subject` 必需。其余字段按下列 kind 使用；ID 在 intent+plan 合并后唯一，不以 native: 开头。status/digest/origin 由程序生成。

- user_statement：text 是实际表达，输出 user_declared。用户库存不是程序独立盘点。
- web：url 与正文实际包含的 excerpt，由脚本读取公开官方/PoEDB 页面，输出 source_observed。其他公开来源可用 TeleAgent 联网工具读取，再使用 artifact。
- artifact：path 指向本次任务目录内文本，excerpt 必须确实存在，记录内容摘要与采集 URL/时间。输出 artifact_observed，代表片段可追溯，不假装文件必定来自声称的网站。不能用此路径读取平台内部输出目录。
- local_data：使用 `data read` 的 evidence_template（source/line/count/content_sha256），补 evidence_id/subject。程序从本机重新读取，不接受替代 text/path/url。输出 local_data_observed 只证明原文及摘要，不证明含义与当前方案相关、当季可得或游戏内实测。
- 原生引用直接写 `native:candidate#/stats/Life`、`native:baseline#/stats/Life` 等 JSON pointer，无需声明 evidence。只能访问本次已有字段，指针不执行代码。引用值与引擎/计算 XML 绑定；不要猜旧数组下标。

claim 的字段为 `claim_id,text,subjects,evidence_refs,conditions,uncertainties`。subjects/evidence_refs/conditions/uncertainties 均为字符串数组；claim_id 唯一。缺引用、引用不存在、来源未解决或 uncertainties 非空，保持 unresolved。程序附上 observations 的真实值、对象上下文和摘要；不得手写这些输出字段。model_assessed 表示模型作了有引用的判断，**不表示程序证明语义充分**。最终 review 另行绑定，不能让旧文字自动得到新方案的复核身份。

常用 subject：`goal`、`progress:normal`、`progress:ascendancy`、`progress:secondary_ascendancy`、`condition:guard`、`condition:Flask 1`、`config:<字段>`、`skill:<skill_ref>`、`dependency:<skill_ref>`、`count:<组>:<宝石位置>`、`resource:Mana:leech`、`resource:EnergyShield:recharge`、`transformation:<槽位>`、`legality:item:<物品ID>`、`migration`、`template_source`。以实际 pending 为索引说明问题；permission、缺失原生量和 capability 缺口不能靠填理由转成程序通过。

`assessment:{goal_claim_ids:["目标claimID"],scope:"实际核对范围",unresolved:[]}`。goal claim 引用用户目标与本次数值/来源，解释收益、代价、是否满足。不得以改变 scope 隐去用户已经明确要求的部分。

usage 每行是 `{skill_ref,casts_per_second,active_seconds,recovery_seconds,continuous,claim_id}`。引用 evaluation.result.active_skills 本次真实 skill_ref；claim 关联 `skill:<同一引用>`。频率为次/秒，窗口与恢复为秒，有限非负；未知就省略，不能编数。手动副技能需明确实际频率，包括0次的场景也解释。程序核对单次支付、可核算的线性费用、使用窗口与恢复；不能可靠推导的时序留 capability 缺口。默认展示原生满速费用；不要求全部方案满速持续。净缺口没有使用窗口、充能启动前储量不足，不因 prose claim 存在而消除。降频不能沿用满速命中吸取来证明续航，未重算时保持 unknown；DPS 同样不能当节奏调整后的持续输出。

原生 `equipped[].flask_local` 是单件药剂参数，`flask_scope` 明示不包括角色修正、当前充能、补充事件或实际覆盖。开启药剂的快照若声明 continuous，程序会保留 `capability:flask_coverage:<槽位>`；当前适配没有完整覆盖时序测量。可以研究具体有限窗口与缺口，但不能只写“有药剂”就宣称无限续航。

## 库存和采购

requirements 从实际 XML 差异产生，包含仍装备的禁用/备用宝石、规格变化、装备改造和退点资源。阅读 evaluation.budget.materials，不能用“原 XML 没有，所以用户肯定没有”代替询问。

物资公共字段：kind、quantity、evidence_refs，可选 description。gem 用 skill_id/level/quality；item 用 item_id/spec_sha256（来自该候选需求）；service 用 service_id。库存写 inventory，报价写 shopping，均不能自填 status。

库存 evidence.subject 为 `gem:<skill_id>:<level>:<quality>`、`item:<id>` 或 `service:<service_id>`，context.quantity 对应用户实际确认数量，装备还匹配 spec_sha256。同规格合并数量，依据不重复。同一条用户声明不反复累计数量。

报价另含 low/high/currency：low/high 是**该整条 quantity 的总价**。来源 context 需对应 realm:"cn"、league、currency、quantity、low、high，装备还需 spec_sha256，并有实际 checked_at。`quote:<物资引用>` claim 必须引用同一价格记录，解释规格与语义匹配。只写 URL 或 verified_cn 会被拒绝。国际服报价不计成国服已核实价格；无汇率依据不自动换算。

程序区分 unknown、uncertain_range、quote_assessed、user_declared、no_additional_cost、over_budget。已知必需下界超过限额才确定超预算；估计超过或缺项不假称肯定超预算/预算满足。totals_complete=false 时金额仅为已列部分。quote_assessed 仍有交易时效条件，非成交保证。

## 结果、完整性与交付

evaluate 保存 candidate.xml、requested.xml、plan.json、evaluation.json、review.json。读取 result 的实际技能、装备、配置、统计与伤害分项，readiness 的资源、errors/pending、budget、claims/evidence 和逐类 validation。diagnostics 带 code/category/subject/evidence_refs/affects/message/status；未知能力不改写成游戏非法。默认值等价性按本机 ConfigTab.GetDefaultState/Save 语义检查，同时核对统计；没有一律丢弃 false。config_values 记录最终保存文件的实际输入，config_saved_values 是省略原生默认值后的保存语义。

最终复核使用现有 TeleAgent 模型，不增加另一模型服务。先完成方案/claims 修订并 evaluate，再阅读 review.json 的 facts/claims：核对数字与技能/主体/阶段、因果证据、替代方案排除理由、切换进入条件以及 summary/迁移说明/最终回答的范围。发现问题就修改并重新 evaluate，直到可以如实记录本轮复核。填写源 plan 的 `assessment.review`，再 evaluate：

```json
{"token":"本次 review.json 的完整 token","findings":"实际检查内容、发现和仍成立的取舍；不是照抄通过字样","unresolved":[]}
```

token 绑定意图、方案、文字、引用内容、最终 XML/引擎、资源/预算和数值；改变后旧复核失效。`final_review:missing/stale` 保留 draft，findings/unresolved 不改写机械矛盾或其他缺口。此步骤只证明模型复核针对这一版，不证明语言无误；不要用脚本自动填 token，不能以复核记录宣称全部机制验证。

候选名称为1–48位小写英文/数字/连字符。原件、意图、请求 XML、候选记录/文件及引擎内容都有完整性校验；直接手改输出会要求重算。baseline 缓存按意图、共同场景、引擎及适配版本区分，各候选保留自己的基准。

probe 不改候选文件，可用于 blocked/draft 中仍可计算的输入。返回各变体原生快照、差异与未评审依赖；不继承旧宝石的使用结论，最终选择仍写完整 plan/evaluate。批量大小为进程资源保护，自动分批比较。

export 再复算并重核来源和复核 token。blocked 只输出诊断 HTML；draft 输出明确命名的配装草案 XML 与说明；scoped_result 输出改进 XML 与说明。目录为 `run/output/<候选名称>/`，附 final-assessment.json、review.json、delivery.json。只有 XML 与 HTML 均落盘后 delivery.file_exported 才为 true，清除同候选旧状态的另一 XML 文件；失败中断时保持 preparing/会话 exporting，处理错误后重试 export。无 URL 的本地证据仍正常展示。最后交付两份用户文件，以 facts_summary 为事实基础解释取舍；“已回读”不代表全部机制、预算和实战已证实。

退出字段：calculation_mode、resource_mode、unused_points_reason、config_evidence、burst_seconds、upgrade_gems、fallback_skill_id、version_status、verified_cn。分别迁入具体 usage、claims、阶段或来源；旧字段显式报错，不保留双套运行路径。
