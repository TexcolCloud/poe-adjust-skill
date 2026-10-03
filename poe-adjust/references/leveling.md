# 开荒路线

本分支用于用户明确调用后的新角色/开荒推荐，不需要先有角色 XML。已有角色要改进具体装备时仍走 SKILL.md 的实算流程。

## 对话与研究

每轮最多两题，确认赛季/国服、职业或偏好、剧情到什么阶段、交易还是自力、初始通货与已备物资、手动按键/近战/召唤等接受程度。能从上下文知道的别重复问。给 2–3 个有资料支持的方向，解释核心依赖、早期弱点与转型成本，再根据用户选择整理完整路线。

完整路线覆盖开局、主技能解锁、核心辅助/第一次升华、中后剧情、剧情结束与异界初期。分段按角色等级设里程碑，章节进度另写，不能假定玩家必定在某一级做完某章。用户只问局部则 scope=partial。

联网读取当前版本公告、原作者攻略和宝石任务/商店表。先查国服与资料版本映射；没查证就 version_status=unverified，不能把本机 3_29 或文件名 S29 自动当作国服当前赛季。官方公告优先；社区原始配装需标注作者、版本及演示条件。不给脱离版本的“必然最强”排名。

原生宝石等级用 catalog 批量查询。技能能使用的等级不等于该职业能从商店购买的进度：两者分别核对。逐项写领取章节/任务/NPC/职业限制；例如任务奖给另一个职业时，不能写本职业也免费领取。转化、瓦尔、觉醒、掉落限定宝石和特殊暗金不能当作稳定开局条件，必须提供普通宝石或不依赖该装备的替代。

主方案优先三/四连，五/六连写获得后的升级及没获得时的四连。每段给装备属性优先级、升级武器/盾牌的判断方式、生命/抗性与属性检查、天赋先后和升华顺序、魔力药剂/资源办法、切换所需核心条件。资源、命中、元素抗性惩罚、移速和解异常状态都要落实为可做的步骤，不用一堆流派名代替路线。

## 路线文件与命令

把已确认的路线写 route.json，执行：

```powershell
python '<技能目录>/scripts/poe.py' route --invoked --file '<route.json>' --out '<工作区>/开荒路线.html'
```

返回 ok:false 则按错误修正；成功的 route_checked 只表示阶段、宝石等级与获取依赖等已实现检查通过，不表示已对完整装备实算。最终用 TeleAgent 文件工具交付 HTML。不要假造“实测 DPS”或把技能草图包装成完整可用 XML。

顶层字段：

| 字段 | 含义 |
|---|---|
| confirmed | 用户已明确方向，true |
| name / goal / league | 中文标题、目标、用户确认的国服赛季 |
| game_version | 本路线依据的机制版本（如 3.29）；必须与本机数据匹配，国服 S30 对应 3.29，不能从 S29 编号推断 |
| class_id / ascendancy | 职业 ID 与预定升华；0 贵族、1 野蛮人、2 游侠、3 女巫、4 决斗者、5 圣堂武僧、6 暗影刺客 |
| trade_mode | ssf 或 trade |
| budget | amount/currency，初始可投入通货 |
| version_status / version_source | verified 或 unverified；verified 时 version_source 为实际支持国服版本映射的来源 ID |
| scope | maps（默认，完整至异界）、campaign（完整剧情）、partial（局部） |
| sources | id/title/url/checked_at/version 数组，url 为实际网页，checked_at 为实际 ISO 日期/时间；不得复制示例占位内容 |
| stages | 按 level 严格递增的 1–12 个阶段，完整路线至少三段，从不高于 5 级覆盖至至少 68 级 |
| risks / notes | 风险文字数组及补充说明 |

每个 stage：

| 字段 | 含义 |
|---|---|
| name / level | 阶段名与角色等级 |
| main_skill_id / links | 当前主技能与主技能连线数 1–6；超过四连须填写 link_fallback 的四连替代 |
| skills | 宝石数组，字段见下文；main 与 support 合计不得超过 links，utility 不计入主连线 |
| gear / passives | 装备属性优先级、天赋分配先后，均为非空文字数组；被动关键节点要查本机名字和连接，不凭空写 ID |
| ascendancy / resources | 本段升华进度/顺序，资源与操作办法，文字 |
| checks / transition | 本段达标检查与转入下段的条件，非空文字数组 |
| required_items | 可选核心物品数组，每项 name/acquisition/source_id；随机、交易或迷宫获取须写 fallback；不需要暗金则省略 |

skills 每项必须填写：

```json
{
  "skill_id": "从本机 catalog 取得的 ID",
  "level": 1,
  "quality": 0,
  "role": "main",
  "acquisition": "vendor",
  "source_id": "实际来源 ID",
  "source_note": "实际查到的章节、任务、NPC、适用职业及必要步骤"
}
```

role 为 main/support/utility，每段恰有一个 main，并与 main_skill_id 一致。level 是本段目标宝石等级，不是角色等级；不会自动下调超出需求的宝石。获取方式 acquisition 为 quest/vendor/drop/trade/lab/owned。trade 在自力模式下拒绝；随机、迷宫、交易或特殊宝石须提供 fallback_skill_id，普通替代必须在该阶段可用。fallback 是主路线卡住时的过渡技能；仍须在说明核对替代辅助和武器类型，不能随便塞一个低级技能。

宝石可以沿用前一段的获取来源；source_note 写“上一阶段已取得并继续练级”。拿到技能不等于已经练到推荐等级。所有国服价格无证据时保持未知，不用预算 0 推导一切免费。

catalog 与 HTML 优先采用本机 Items_Gems 中文名称，并保留英文便于核对。3.29 普通孔色不限制宝石插入，匹配色提供品质加成；开荒缺资源时先满足连线和基础防御，不能把旧版强制改色流程当作必要花费。

需要具体阶段 XML 时，取得可信同版本模板并按真实/明确目标装备建立配装候选，走 intent/evaluate/export；不能只写几颗宝石就宣称完成伤害、生存和续航验证。用户推进后可以上传新的角色 XML，继续计算下一步。
