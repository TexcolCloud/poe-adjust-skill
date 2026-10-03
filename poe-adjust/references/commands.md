# 命令与文件协议

此文件用于实际调用 `scripts/poe.py`。全部 JSON 为 UTF-8；数字、布尔值和数组使用 JSON 类型。路径带空格时用 PowerShell 单引号。使用文件参数传递 JSON，不把整段 JSON 拼进命令行。

以下 `$entry`、`$run` 在每次独立 PowerShell 调用中重新赋值，或直接写绝对路径；不能假设前一工具调用的变量仍存在。Python 由 TeleAgent 运行时提供，不依赖 Codex。

## 命令

```powershell
$entry = '<技能目录>/scripts/poe.py'
python $entry doctor
python $entry inspect --input '<角色.xml>'
python $entry start --invoked --input '<角色.xml>' --run '<工作区>/配装任务/会话'
python $entry inspect --input '<角色.xml>' --calculate --out '<工作区>/配装任务/会话/diagnosis.json'
python $entry intent --run '<工作区>/配装任务/会话' --file '<意图.json>'
python $entry evaluate --run '<工作区>/配装任务/会话' --name proposal-a --plan '<计划.json>'
python $entry export --run '<工作区>/配装任务/会话' --name proposal-a
python $entry status --run '<工作区>/配装任务/会话'
```

`--pob '<PoECharm 或内部 PoB 文件夹>'` 可放在任何子命令后。会话建立后通常无需重复提供。`inspect` 的结果较大，优先保存到文件并读取相关部分。

`--run` 始终指向同一个会话目录。首次 start 要求目录为空或尚不存在；原始文件和已有诊断放在其他目录也可以。读取诊断时用 `Get-Content -Raw -Encoding UTF8 ... | ConvertFrom-Json`，优先选取 `calculation.stats`、`calculation.warnings` 和相关技能组，不需要逐行读完整个大文件。

start 返回 overview，已含职业、等级、两个版本和技能组，不需要为了首次问询再 inspect。inspection 文件保留原 XML 字段：skill_groups[].gems[].skillId/nameSpec；计算结果则为 calculation.gems[].id/name。不要反复试猜字段名。任务点/升华进度仍由用户确认，不能从缓存的 unallocatedPassives 推断已完成任务数。

分享码：把实际取得的纯 PoB 分享码存成文本，运行 `decode --input '<分享码.txt>' --out '<模板.xml>'`。也可直接将纯分享码文件交给 inspect/start。网页地址和 HTML 不是分享码；用现有联网工具获取其中的真实分享内容，保留来源。

查询：

```powershell
python $entry catalog --kind translations --query '盾' --limit 12
python $entry catalog --kind gems --query 'Shield Crush' --limit 10
python $entry catalog --kind skills --query 'Embrace Madness' --limit 3
python $entry catalog --kind nodes --input '<角色.xml>' --query 'life' --limit 15
python $entry catalog --kind nodes --input '<角色.xml>' --query '12345' --limit 1
python $entry catalog --kind nodes --input '<角色.xml>' --query 'Champion' --limit 30
python $entry catalog --kind uniques --query 'Emperor' --limit 3
python $entry catalog --kind config --query 'enemyIsBoss' --limit 3
python $entry catalog --kind classes --query 'Duelist' --limit 1
python $entry catalog --kind links --query 'ShieldCrush,SupportBrutality,SupportMeleePhysicalDamage' --limit 1
python $entry catalog --batch '<queries.json>' --input '<角色.xml>'
```

一次需要多个查询时优先 `--batch`，只启动一次引擎。文件格式 `{"queries":[{"kind":"gems","query":"id:ShieldCrush","limit":2},{"kind":"nodes","query":"Champion","limit":30},{"kind":"config","query":"enemyIsBoss","limit":3}]}`。支持 gems/skills/nodes/uniques/config/classes/links，最多 30 项；translations 单独查询。返回 `queries[].entries`。`gems` 的 `id:技能ID` 是精确检索；结果含各宝石等级的角色等级需求 `level_requirements`、`min_level`、`special_variant`、`vaal`，开荒时据此核对而非猜等级。

`classes` 返回当前树的职业与升华 ID/英文名，避免错配或使用已移除的升华。`links` 的 query 用逗号依次写一个主动技能 ID 和最多五个辅助 ID，返回 compatible/incompatible。它调用原生技能类型匹配，只用于早期排除不兼容组合；不替代实装后的武器、触发等级/冷却、伤害转换和资源验证。evaluate 还会拒绝未辅助任何已启用技能的辅助宝石。

技能返回 `skill_id` 才是操作字段；`id` 可能是游戏物品路径。查询天赋时带输入文件，使用其版本与当前已点天赋。结果按本机引擎计算的所需新增点数排序；`allocated` 表示已点，`reachable` 表示有可分配路线，`points_to_allocate` 是新增点数，`path` 包含到目标需要的节点 ID。将路线与保留节点合并后设置完整 `tree.nodes`，再评估点数和依赖；不要孤立添加远处的大点。`linked` 返回邻接节点，供调整已有路径时查看。查询只返回最多 100 条；细化关键词，不反复截断全库。

局部调整优先 `tree.nodes_add` / `tree.nodes_remove`，由程序保留其他节点，避免手抄整个列表遗漏连接或珠宝节点。nodes_add 要包含 catalog 返回的完整 path；nodes_remove 只接受确实已点的节点。不要与完整 nodes 混用。增量操作不自动修补移除节点造成的断路，evaluate 会拒绝丢失的节点，须按真实连接修复。换升华时也需移除不再适用的原升华节点。

升华节点返回 `ascendancy_name` 和 `is_ascendancy_start`；用升华英文名（如 Champion）可一次查齐该升华节点及原生路径，不能按节点 ID 大小猜测归属。升华起点不消耗点数，分配结果以 evaluate 的 `ascendancy_points` 为准。

`gems` 用于查找可装配宝石；`skills` 用于读取技能说明，也包括装备赋予的技能（`from_item=true`），不能把这些技能当成可以买到的独立宝石。`config` 同时检索配置键、标签和提示文字，可查实际增益/负面层数的解释。先用这些只读命令及网络资料查机制，通常无需搜索整个安装目录或读取引擎源码。

## 意图 intent.json

```json
{
  "confirmed": true,
  "goal": "当前等级刷图更稳定，尽量少按键",
  "league": "国服 S30（由用户确认）",
  "game_version": "3.29",
  "version_source": "实际查阅并支持赛季对应关系的网页 URL",
  "version_checked_at": "实际查阅的 ISO 日期",
  "mode": "both",
  "budget": {"amount": 20, "currency": "divine"},
  "target_level": 86,
  "allow_class_change": false,
  "allow_ascendancy_change": true,
  "allow_main_skill_change": true,
  "keep_slots": ["Weapon 2"],
  "keep_skill_ids": [],
  "baseline_main_group": 1,
  "scenario": {"enemyLevel": 83},
  "minimum_stats": {"FireResist": 75, "ColdResist": 75, "LightningResist": 75},
  "quest_passive_points": 23,
  "ascendancy_points": 8,
  "calculation_mode": "steady",
  "resource_mode": "sustained",
  "notes": "用户允许更换主技能；保留盾牌；不重新练号。"
}
```

示例数值不是默认事实，按对话填写。`target_level`、`baseline_main_group`、`quest_passive_points`、`ascendancy_points` 均须明确填写，脚本不默认满任务或满升华。用户未回答时先分轮询问。`mode` 为 refine/rebuild/both。预算 amount/currency 必填，金额是本次可投入预算；自力模式可写 0 并将确认已有的物品记为 owned。没有预算时先问。

`game_version` 为已联网核实的游戏主版本（如 3.29），不是 PoB 2.67.2、XML targetVersion=3_0 或国服赛季编号。`version_source` 为实际网页 URL，`version_checked_at` 为查阅日期。国服 [S30 官方公告](https://poe.qq.com/webplat/info/news_version3/22557/22558/22601/m14525/202607/988692.shtml) 明确说明 3.29.0；本机机制/宝石数据版本为 3.29。S29 文件名不证明目标赛季，若用户也说 S29，必须先澄清是否永久区/旧文件用于当前赛季。旧树并不切换技能与计算规则。本包暂不支持历史版本实算。

`keep_slots` 锁定该插槽原物品的完整文本，而非只锁物品名字。`keep_skill_ids` 要求候选中该技能启用。未明确允许时三个 allow 字段保持 false。`baseline_main_group` 从 inspection 的 `skill_groups[].index` 选择；先确定用户真正使用的主技能。

`scenario` 是共同敌人/战斗配置，实际配置名用 config 查询。空对象使用引擎默认场景，并在报告明确；不要把默认场景称为终局首领。输入里旧战斗 Input 会被重置，盗贼与万神殿信息保留。共同场景在候选中不可单独改动。

`calculation_mode` 默认 `steady`：守护增益、药剂必须关闭。`conditional` 用于用户接受的条件面板，开启的守护需 `config_evidence.guard`，每个药剂需 `config_evidence["Flask 1"]` 等说明触发、时长与覆盖。

`resource_mode` 默认 `sustained`：每秒消耗超出秒回与吸取时拒绝交付。只有用户接受停手恢复才用 `burst`，计划给出 `burst_seconds`；程序校验时间不超过资源池并报告停手仅靠秒回所需的恢复时间。吸取值仍依赖命中，不能当转场回复。

## 候选 plan.json

```json
{
  "name": "保留现有盾牌的调整方案",
  "mode": "refine",
  "summary": "修正技能搭配与资源保留，然后比较伤害和生存。",
  "operations": {
    "gems": [{"group": 1, "index": 1, "skill_id": "SupportBrutality", "level": 20, "quality": 0}],
    "groups": [{"index": 7, "enabled": false}],
    "main_group": 1
  },
  "sources": [{"title": "实际查阅的机制资料", "url": "<实际支持结论的直接页面 URL>", "version": "填写实际版本", "checked_at": "填写实际查阅时间"}],
  "shopping": [{"description": "需要准备的辅助宝石", "status": "unknown"}],
  "assumptions": ["按用户确认的主技能和战斗场景比较；药剂覆盖率需说明。"],
  "migration_steps": ["先准备并练好替换宝石，再按说明调整技能组。"],
  "legality_notes": "本例不新增装备词缀。"
}
```

链接/日期必须替换为实际证据，不能照抄占位示例。示例改动只是协议展示，并非对所有角色适用的建议。

`mode` 为 refine 或 rebuild。候选名命令参数 `--name` 使用短英文，如 `proposal-a`；面向用户的中文名称放 `name`。失败候选可用同名重新评估；原角色快照不覆盖。

可用普通点或升华点应完成分配。确需留点时，计划加 `"unused_points_reason":{"normal":"具体留点用途","ascendancy":"具体原因"}`，只填实际留点的类型。理由会进入最终说明；不能用通用套话跳过设计。

### operations 可用字段

| 字段 | 内容 |
|---|---|
| gems | 数组：group/index 为当前技能组/组内宝石的 1 起始序号；其余可设 skill_id、level、quality、enabled、enable_global1、enable_global2、count、skill_part、skill_stage_count、skill_minion |
| groups | 数组：index 加 enabled、slot、label、include_in_full_dps、main_active_skill、group_count 中的字段 |
| replace_skills | 完整替换当前技能方案的技能组数组；其他未启用方案保留。每组包含 slot、enabled、include_in_full_dps、main_active_skill 等组字段，以及 gems 数组；新宝石至少提供 skill_id，默认 1 级 0 品质 |
| items | 数组：`{"id":20,"text":"Rarity: RARE\n名称\n基底\n..."}`；原 ID 表示替换，新 ID 表示新增。文本必须为 PoB 可识别格式，可保留已有物品的 ID |
| equip | 数组：`{"slot":"Weapon 2","item_id":20}`；flask 可附 `active:false`；`Jewel 12345` 表示天赋珠宝插槽；0 表示移除 |
| tree | 对象：nodes 完整替换，或 nodes_add/nodes_remove 增量添加/移除；class_id、ascendancy_id、secondary_ascendancy_id 可选；masteries 为 `{"节点ID":效果ID}`。沿用输入天赋版本，不能靠改 treeVersion 强行迁移 |
| level | 1–100，必须等于用户确认的 target_level |
| main_group | 当前技能组的 1 起始序号 |
| weapon_set | 1 或 2 |
| config | 候选条件对象，需在 config_evidence 按键说明来源；不能修改 scenario 中的共同条件 |

正常装备插槽：Weapon 1、Weapon 2、Weapon 1 Swap、Weapon 2 Swap、Helmet、Body Armour、Gloves、Boots、Amulet、Belt、Ring 1、Ring 2、Flask 1–5。珠宝插槽为 `Jewel 节点ID`。

宝石的 `count`、技能组 `group_count` 与层数仅在真实机制支持时设置，解释实际持续数量。克隆物品或换词缀要确认前后缀、基底、物等、势力、腐化和赛季限制。程序不替代完整游戏物品合法性数据库。

3.29 起宝石不受普通插槽颜色限制，匹配红/绿/蓝提供 +10 品质；不应为插入异色宝石强制增加改色成本。`gems[].quality` 是宝石自身品质，不能把插槽加成再加进去。原生计算按同装备各组和组内宝石的先后顺序分配物理孔位，输出 `matches_socket` / `socket_quality_bonus`。程序保留未启用组并清除其 slot，避免其挤占在用组的品质位置。真实不相连的孔必须拆组；同一连线上含辅助的技能不能拆成独立组来漏算保留/消耗。新 XML 与说明包含实际镶嵌顺序。

### 整体换流派

可从原文件用 replace_skills/tree/items/equip 构造完整新方案；也可用 `template_xml` 指向已取得的、同天赋版本的模板 XML，路径相对于 plan.json 所在目录或使用绝对路径。模板和原角色在相同场景下独立重算。

模板方式仍应在 operations 设置用户目标 level、main_group，并迁移必须保留的真实装备。保留装备可从 inspection 复制其原文本到候选对应 ID，再 equip。`sources` 记录模板来源，`migration_steps` 记录核心装备准备、宝石升级、洗点与最后切换顺序。新职业需要 allow_class_change=true，并明确重新练角色。

### 预算与依据

shopping 每项需要 description、status。物品条目还应提供结构化关联：

- 宝石：`{"kind":"gem","skill_id":"SupportBrutality","level":20,"quality":0,"quantity":1,"description":"将原 19 级练至 20 级或采购替换","status":"unknown"}`。
- 装备：`{"kind":"item","item_id":20,"quantity":1,"description":"目标胸甲，含连线/改色","status":"unknown"}`。
- 洗点、制作等额外操作：`kind:"service"`，用 description 写具体内容。

程序从前后 XML 自动检测新增宝石、等级/品质/数量变化和新装备/改造；未匹配的需求保持 unknown。价格 low/high 表示该条全部 quantity 的总价。原 XML 19 级不能抵消目标 20 级，缺少同规格的物品不能直接标 owned。原 XML 也不等于仓库盘点；用户额外声称已备齐时在说明记录，实际规格须核对。

status：

- owned：原 XML 可复用的同规格物品，或确认不产生通货费用的操作。没有 kind 的文字声明不能抵消程序检测的新增需求。
- verified_cn：提供 low/high/currency/source/checked_at/league；source 为实际国服来源链接，league 与 intent.league 完全一致。
- estimate：提供 low/high/currency，报告保持“估价”标记。
- unknown：价格或可获得性尚不确定，不计入已知成本范围。

金额单位必须和预算一致；先核实汇率再换算。不同市场的价格、制作期望成本和成品成交价格不可混为一谈。空 shopping 表示价格未核实。列出全部必要成本，价格状态只是记录，不自动证明引用网页可信。

`config_evidence` 示例：`{"conditionOnslaught":"装备词缀提供击杀获得，刷图可持续；无小怪首领不使用此假设"}`。说明必须和计算场景相符。

## 校验与交付

evaluate 会计算原方案与候选、检查约束、输出候选 XML，然后回读重算。`status:invalid` 的 `errors` 必须解决才可 export。warnings 不被隐藏：包含引擎未解析词缀、缺少涂油、预算未知、升级比较等。

已覆盖：解析/重复 ID/悬空装备引用、当前方案选择、丢失天赋、当前等级点数与升华点/无解释的留点、启用技能与装备需求、宝石自身等级/品质上限和无效辅助、常态守护/药剂、持续资源或有时限的爆发、自动物品差异、启用技能组的数量/连线与原生顺序、3.29 孔色品质、用户锁定条件、预算上限、回读数据与核心指标一致性。禁用技能组视为备选方案，不占用当前配装的物理插槽。

仍需 agent 结合证据审查：改色/连线的可行性与成本、插槽顺序对特殊装备的作用、触发循环、药剂覆盖率、条件持续性、特殊机制、所有装备词缀组合的游戏内合法性、国服可获得性和价格、用户目标是否实际改善。不能把“计算通过”写成“全部游戏机制与市场均保证”。

export 只接收当前意图下的有效候选；会检查文件摘要并最终复算。输出可直接交付，不把内部 run.json、计划或临时文件作为最终成果。

`evaluate.readiness` 返回实际常态/条件开关、剩余点数和资源明细。原方案诊断失效或条件不同时，`comparison.*.percent` 为 null，不能自己重新计算百分比并称为同条件实战提升。`status` 返回已发现的 pob_path/engine_version，后续查询无需反复 doctor。新技能版本会要求旧候选重新 evaluate。
