# 当前本机事实与外部资料

查询对象/合法选项时用 catalog；缺描述/机制时用 data；当前国服规则、取得和价格用 research 或 TeleAgent 联网工具。具体候选收益必须完整角色实算。使用依据时读取 [证据协议](evidence.md)。

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

批量文件：`{"queries":[{"kind":"gems","query":"id:Fireball","limit":2,"offset":0}]}`。支持上述原生 kind，translations 单查。单页 limit=1–100，按返回 next_offset 继续；程序自动分批，不限制整体候选/路线数量。--out 保存完整数据，摘要中的 queries[].entries_pointer 给出该查询的实际条目位置，entry_fields 按 kind 列出本次条目可出现的字段；用 view 选择需要的字段。summary_complete=false 只表示终端查询元数据未全部展示，完整数据未丢，沿用 next_actions 翻页。多次分批前后指纹须一致。

- classes 条目的 id 是职业 ID，ascendancies 给出主升华；--class-id 是命令参数名。nodes 条目的 stats 是效果文本，name/display_name 是名称，linked/path 是连接信息；按返回 entry_fields 读取，查询过的效果无需再搜原文件。rules 含副升华目录、slots、规则来源及 `meaning/status`。observed 不一律表示游戏上限：engine_state 是本次状态，native_fact 是原生事实，engine_constraint 是接口约束，unsupported 是没读到可靠规则。不能把建议/默认值当合法上限。
- bases 返回当前基底 `sockets/valid_slots/valid_slots_context/affix_limits/requirements`。valid_slots 是该次查询装备与天赋上下文中的观察，空配装中不可放不证明与其他装备组合后不可放；实际阶段由 route 整套布局或 XML 调用原生规则。基底容量不证明实际物品的特殊效果、连线或取得。socket_limits 只汇总类别容量，不推荐任一基底；实际选择核对当前属性/等级要求与取得。affixes 返回当前物品词缀池、互斥组及物品等级要求。
- 孔色读取当次 rules.socket_quality 及 gems.matches_socket/socket_quality_bonus，并按 data 返回定位核对本机匹配与品质计算原文。原生加成事实不等于游戏插入限制证明；是否必须改色、是否值得取得匹配孔色，由模型结合当季国服来源和实际差异决定，不沿用旧赛季禁令。
- gems 的 `natural_max_level`、`level_requirements[].naturally_trainable` 与有效数据级别分开。可计算不证明对应裸宝石可获得。quality 不设复制的统一上限，取得方式仍须依据。`base_resources` 是该等级基础费用/保留/辅助倍率，不能代替完整连线续航。
- links 接受有序宝石 ID，可以多个主动及重复 ID。relations 用 `active_index/support_index/applied` 区分输入位置；valid 仅表示类型查询成功，incompatible 是未起作用的辅助，不证明物理安装、触发、费用或冷却正确。
- config 返回原生类型、枚举 options[].val、标签及可读取默认值；用 val 写入，不能用显示 label 替代。最终实际条件在计算结果 config_values。
- metrics 需要真实 XML，列出当前实际数字指标。minimum_stats 和 comparison 不受中文显示名称表限制；未知指标保留缺口。
- 普通节点 path/points_to_allocate 只有真实 XML 或 --class-id 上下文才提供，两者互斥。不带上下文时只查目录，不能用默认角色路径。当前 path 从目标回溯到既有连接处；反序是加点顺序，多个路径须合并后重新计点。
- 完整升华目录可按 linked 与起点核对路径；完整单次查询另含 ascendancy_path/path_cost，截断页不认证整张图。授予天赋在 granted_passives；已在树上分配不能再算一份收益。anoint_recipe 按本机清单保留重复材料和本机译名，不猜固定配方。
- research 保留原生事实与网页事实的不同范围。宝石 choices 是当前页面实际任务/NPC/章节/职业行，不代表角色已完成或国服无差异。试炼只返回观测位置/区域等级，不按表格顺序推导奖励点数、解锁或玩家进度。版本正文未确认即 unverified；历史链接只是查询种子，不能从赛季编号推算版本。

research 同时给出真实原文模板：宝石在 choices[].evidence_templates，试炼在 labyrinths[].evidence_template，版本在 version_evidence.evidence_template。沿用返回的完整 url/excerpt/checked_at，补 evidence_id 和本次具体 subject，放入 plan/route.evidence；不要凭名称重拼 URL，尤其辅助宝石的来源页。模板证明实际读到了对应片段，领取、国服差异、成长和目标效果仍由模型评审。

## 已保存 JSON 的按需读取

```powershell
python $entry view --file '<工作区>/catalog.json' --pointer '/entries' --fields 'skill_id,display_name,min_level,tags' --limit 8
python $entry view --file '<工作区>/route.evidence.json' --pointer '/checked_route/stages/0/calculation' --fields 'status,errors,pending,stats,active_skills'
python $entry view --file '<工作区>/gem-facts.json' --pointer '/gems/<实际ID>/choices' --limit 3
python $entry view --file '<工作区>/evaluation.json' --pointer '/result/stats' --keys --query '<机制关键词>' --limit 30
python $entry view --file '<工作区>/evaluation.json' --pointer '/pending' --query 'objective:' --limit 20
```

view 复用证据的 JSON pointer，数组按 offset/limit 翻页；fields 选直接字段，缺字段列在 missing_fields，零与false保持原值。keys 列出当前对象所有字段名，不与 fields 同用；query 只筛选字段名或文本数组，不执行表达式。计算复核摘要中的 available_stat_keys 来自当次完整 stats，不是固定机制清单；用实际键再读值或查原生公式。view 不启动引擎、刷新网页或改文件。先看概要和待办，再读具体对象；过大时缩小字段、行数或指针，无需另写读取脚本。只读获准工作区结果，不能用于平台内部目录；PoECharm 原文用 data。

网页缓存按 URL 与实际查询上下文分别保存，核对正文摘要和观察时间，一天内复用；内容损坏、上下文变化或过期重新取源。它只保存观察，不延长行情有效期，也不替模型评审同一来源在当前目标下的含义。

doctor/start 的指纹覆盖计算代码、数据、适配器和译名；data_version_basis 说明本机规则版本依据。version_source_hint 只是未核实的历史公告搜索线索，不证明当前赛季或用户区服；research --version 实际读取正文后才返回 version_evidence。软件版本相同但内容变化仍使旧结果失效。接口不兼容明确报告，不自动修改 PoECharm。操作只要求自己需要的能力，不以守护接口缺失否定职业目录。

## 本机原文只读查询

```powershell
python $entry data --mode list --limit 30 --out '<工作区>/data-index.json'
python $entry data --mode list --path 'engine/Data' --limit 40 --offset 0
python $entry data --mode search --path 'engine/Data' --query 'totem' --limit 15 --out '<工作区>/matches.json'
python $entry data --mode search --path 'translations' --query '图腾' --limit 10
python $entry data --mode read --path '<上一步返回的source>' --line <实际行号> --limit 30 --out '<工作区>/passage.json'
```

单页最多 200 条/行，搜索为字面匹配；用 next_offset/next_line 翻页。默认范围是公开代码/资料/简体中文翻译和本机元数据声明的最新标准树；指定 `engine/TreeData/<返回的版本>` 可读其他树，不能当成本赛季规则。source 是 data 接口中的来源名，沿用它和行号继续查询。--out 返回指向工作区凭证的 view 操作，按 next_actions 读原文和模板。搜索 excerpt 可能截短，truncated 明示；采用结论前 read 上下文。read 返回确切行号、原文、文件字节 SHA256 和 evidence_template，补 evidence_id/subject 后放入 evidence 即可。同一文件不同片段的 SHA256 相同是正常的，line/count 区分片段。验证时重新读取相同行、比较文件摘要；更新后旧引用变成 unresolved。

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

本接口不开放 Settings、Builds、任意绝对路径或跳出范围的链接；文本只读不执行。结果仅写任务工作区，安装目录与 PoECharm 资料目录受写入检查。它不是 TeleAgent 其他工具的系统级沙箱；权限边界见 SKILL.md 的入口约定。国服当季取得、完整剧情奖励/商店/试炼路线及实时价格仍要联网核实。
