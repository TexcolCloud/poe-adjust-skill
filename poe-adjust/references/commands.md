# 命令与文件协议

此文件用于实际调用 `scripts/poe.py`。全部 JSON 为 UTF-8；数字、布尔值和数组使用 JSON 类型。路径带空格时用 PowerShell 单引号。使用文件参数传递 JSON，不把整段 JSON 拼进命令行。

以下 `$entry`、`$run` 在每次独立 PowerShell 调用中重新赋值，或直接写绝对路径；不能假设前一工具调用的变量仍存在。Python 由 TeleAgent 运行时提供，不依赖 Codex。

## 命令

```powershell
$entry = '<技能目录>/scripts/poe.py'
python $entry doctor
python $entry inspect --input '<角色.xml>' --out '<任务目录>/inspection.json'
python $entry inspect --input '<角色.xml>' --calculate --out '<任务目录>/diagnosis.json'
python $entry start --invoked --input '<角色.xml>' --run '<工作区>/配装任务'
python $entry intent --run '<工作区>/配装任务' --file '<意图.json>'
python $entry evaluate --run '<工作区>/配装任务' --name proposal-a --plan '<计划.json>'
python $entry export --run '<工作区>/配装任务' --name proposal-a
python $entry status --run '<工作区>/配装任务'
```

`--pob '<PoECharm 或内部 PoB 文件夹>'` 可放在任何子命令后。会话建立后通常无需重复提供。`inspect` 的结果较大，优先保存到文件并读取相关部分。

分享码：把实际取得的纯 PoB 分享码存成文本，运行 `decode --input '<分享码.txt>' --out '<模板.xml>'`。也可直接将纯分享码文件交给 inspect/start。网页地址和 HTML 不是分享码；用现有联网工具获取其中的真实分享内容，保留来源。

查询：

```powershell
python $entry catalog --kind translations --query '盾' --limit 12
python $entry catalog --kind gems --query 'Shield Crush' --limit 10
python $entry catalog --kind nodes --input '<角色.xml>' --query 'life' --limit 15
python $entry catalog --kind nodes --input '<角色.xml>' --query '12345' --limit 1
python $entry catalog --kind uniques --query 'Emperor' --limit 3
python $entry catalog --kind config --query 'enemyIsBoss' --limit 3
```

技能返回 `skill_id` 才是操作字段；`id` 可能是游戏物品路径。天赋查询返回邻接节点，按可连通路线选点。查询只返回最多 100 条；细化关键词，不反复截断全库。查询天赋时带输入文件，使用其版本。

## 意图 intent.json

```json
{
  "confirmed": true,
  "goal": "当前等级刷图更稳定，尽量少按键",
  "league": "国服 S29（由用户确认）",
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
  "notes": "用户允许更换主技能；保留盾牌；不重新练号。"
}
```

示例数值不是默认事实，按对话填写。`mode` 为 refine/rebuild/both。预算 amount/currency 必填，金额是本次可投入预算；自力模式可写 0 并将确认已有的物品记为 owned。没有预算时先问。

`keep_slots` 锁定该插槽原物品的完整文本，而非只锁物品名字。`keep_skill_ids` 要求候选中该技能启用。未明确允许时三个 allow 字段保持 false。`baseline_main_group` 从 inspection 的 `skill_groups[].index` 选择；先确定用户真正使用的主技能。

`scenario` 是共同敌人/战斗配置，实际配置名用 config 查询。空对象使用引擎默认场景，并在报告明确；不要把默认场景称为终局首领。输入里旧战斗 Input 会被重置，盗贼与万神殿信息保留。共同场景在候选中不可单独改动。

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
  "sources": [{"title": "实际查阅的机制资料", "url": "https://www.pathofexile.com/", "version": "填写实际版本", "checked_at": "填写实际查阅时间"}],
  "shopping": [{"description": "需要准备的辅助宝石", "status": "unknown"}],
  "assumptions": ["按用户确认的主技能和战斗场景比较；药剂覆盖率需说明。"],
  "migration_steps": ["先准备并练好替换宝石，再按说明调整技能组。"],
  "legality_notes": "本例不新增装备词缀。"
}
```

链接/日期必须替换为实际证据，不能照抄占位示例。示例改动只是协议展示，并非对所有角色适用的建议。

`mode` 为 refine 或 rebuild。候选名命令参数 `--name` 使用短英文，如 `proposal-a`；面向用户的中文名称放 `name`。失败候选可用同名重新评估；原角色快照不覆盖。

### operations 可用字段

| 字段 | 内容 |
|---|---|
| gems | 数组：group/index 为当前技能组/组内宝石的 1 起始序号；其余可设 skill_id、level、quality、enabled、enable_global1、enable_global2、count、skill_part、skill_stage_count、skill_minion |
| groups | 数组：index 加 enabled、slot、label、include_in_full_dps、main_active_skill、group_count 中的字段 |
| replace_skills | 完整替换当前技能方案的技能组数组；其他未启用方案保留。每组包含 slot、enabled、include_in_full_dps、main_active_skill 等组字段，以及 gems 数组；新宝石至少提供 skill_id，默认 1 级 0 品质 |
| items | 数组：`{"id":20,"text":"Rarity: RARE\n名称\n基底\n..."}`；原 ID 表示替换，新 ID 表示新增。文本必须为 PoB 可识别格式，可保留已有物品的 ID |
| equip | 数组：`{"slot":"Weapon 2","item_id":20}`；flask 可附 `active:false`；`Jewel 12345` 表示天赋珠宝插槽；0 表示移除 |
| tree | 对象：nodes 是完整节点 ID 数组；class_id、ascendancy_id、secondary_ascendancy_id 可选；masteries 为 `{"节点ID":效果ID}`。沿用输入天赋版本，不能靠改 treeVersion 强行迁移 |
| level | 1–100，必须等于用户确认的 target_level |
| main_group | 当前技能组的 1 起始序号 |
| weapon_set | 1 或 2 |
| config | 候选条件对象，需在 config_evidence 按键说明来源；不能修改 scenario 中的共同条件 |

正常装备插槽：Weapon 1、Weapon 2、Weapon 1 Swap、Weapon 2 Swap、Helmet、Body Armour、Gloves、Boots、Amulet、Belt、Ring 1、Ring 2、Flask 1–5。珠宝插槽为 `Jewel 节点ID`。

宝石的 `count`、技能组 `group_count` 与层数仅在真实机制支持时设置，解释实际持续数量。克隆物品或换词缀要确认前后缀、基底、物等、势力、腐化和赛季限制。程序不替代完整游戏物品合法性数据库。

### 整体换流派

可从原文件用 replace_skills/tree/items/equip 构造完整新方案；也可用 `template_xml` 指向已取得的、同天赋版本的模板 XML，路径相对于 plan.json 所在目录或使用绝对路径。模板和原角色在相同场景下独立重算。

模板方式仍应在 operations 设置用户目标 level、main_group，并迁移必须保留的真实装备。保留装备可从 inspection 复制其原文本到候选对应 ID，再 equip。`sources` 记录模板来源，`migration_steps` 记录核心装备准备、宝石升级、洗点与最后切换顺序。新职业需要 allow_class_change=true，并明确重新练角色。

### 预算与依据

shopping 每项需要 description、status。status：

- owned：用户确认已有或操作不产生通货费用。
- verified_cn：提供 low/high/currency/source/checked_at/league；source 为实际国服来源链接，league 与 intent.league 完全一致。
- estimate：提供 low/high/currency，报告保持“估价”标记。
- unknown：价格或可获得性尚不确定，不计入已知成本范围。

金额单位必须和预算一致；先核实汇率再换算。不同市场的价格、制作期望成本和成品成交价格不可混为一谈。空 shopping 表示价格未核实。列出全部必要成本，价格状态只是记录，不自动证明引用网页可信。

`config_evidence` 示例：`{"conditionOnslaught":"装备词缀提供击杀获得，刷图可持续；无小怪首领不使用此假设"}`。说明必须和计算场景相符。

## 校验与交付

evaluate 会计算原方案与候选、检查约束、输出候选 XML，然后回读重算。`status:invalid` 的 `errors` 必须解决才可 export。warnings 不被隐藏：包含引擎未解析词缀、缺少涂油、预算未知、升级比较等。

已覆盖：解析/重复 ID/悬空装备引用、当前方案选择、丢失天赋、当前等级点数与升华点、启用技能与装备需求、资源警告、物理插槽数量及连线容量、用户锁定条件、预算上限、回读数据与核心指标一致性。

仍需 agent 结合证据审查：具体孔色与可改色成本、触发循环、药剂覆盖率、条件持续性、特殊机制、所有装备词缀组合的游戏内合法性、国服可获得性和价格、用户目标是否实际改善。不能把“计算通过”写成“全部游戏机制与市场均保证”。

export 只接收当前意图下的有效候选；会检查文件摘要并最终复算。输出可直接交付，不把内部 run.json、计划或临时文件作为最终成果。
