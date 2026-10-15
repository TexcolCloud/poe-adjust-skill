# 当前资料与事实查询

## 只读查询与完成

名称、开关、规格、费用、频率及已算数值的含义，直接 inspect/probe，用 `--out '<授权工作区>/facts.json' --report '<授权工作区>/事实.html'` 交事实页与简短答复。当前独立单次支付、固定收支和平均耗尽投影也属事实，按返回范围解释，未测部分明确未知；无需建立 intent/plan。禁用技能保留原状。需要另证机制因果、配装取舍或裁决作者主张时，才转[只读解释](build.md#只读解释)。

```powershell
python '<entry>' inspect --input '<原件.xml>' --calculate --out '<工作区>/facts.json' --report '<工作区>/事实.html'
python '<entry>' inspect --input '<原件.xml>' --metrics 'Life,ManaCost' --skill-ref '<当前完整引用>' --actor player --out '<工作区>/detail.json'
python '<entry>' catalog --kind gems --query '<相关名称>' --out '<工作区>/gems.json'
```

参数与分页沿 `--help` 和[命令协议](commands.md)。查询不会认证角色合法、取得渠道或整套目标达成。

## 按需目录

当前 catalog 支持 classes/rules/gems/skills/links/bases/affixes/nodes/config/metrics/uniques/translations。只查本次需要的内容；批量 queries 的每项含 kind/query/limit/offset。职业、升华、技能、基底、节点与配置 ID 从本次结果取，不从记忆硬写。

节点路径用 `--input` 或当前 class_id 给上下文，升华连接点也算点数。links 使用实际有序技能 ID，验证辅助类型，不代替物理孔连/属性/取得。bases 的 valid_slots 属于该次布局上下文，缺上下文保留未知，不能当全局武器规则。

doctor 的 data_version/树版本表示本机资料。旧角色树与当前共享目录不同则说明范围，目标国服/赛季机制版本仍需实际来源。无需为当前查询读取整套历史 Data 或下载无关构筑。

## 技能、配置与明细

宝石保存的 level/quality 与 effective_specifications 分开；unknown 不猜。active_skills 的 skill_ref/actor、cost/rate、native_timing 限定当次主体、费用、频率及可见时长/冷却；player、minion、mirage 不互借。granted_effects 不证明触发或覆盖。

config_controls 区分 value/default/value_basis，以及 Input/Placeholder、runtime/applied。null 是未知或未覆盖，false 是实际关闭；清除覆盖可能恢复默认。只读到一个控制未生效，不推断全部开关关闭。按当前控件类型、options[].val、visibility 与原文选择控制；数值范围、默认贡献和条件由本次资料/计算判断。需要追机制时沿 apply_source 的 source/line 读实际原文。

`inspect --metrics` 自动计算，breakdowns 提供实际 value/detail/context、available_metrics 和 MAIN 对照。不同上下文、不同主体、不支持或截断明细明确说明；缺字段不填零或编公式。有限 probe 只证明实际改变的输入在本场景中产生的观察，不直接证明游戏机制不存在。

药剂的 flask_base 是基础，flask_local 是单件经本地处理的参数；两者不能重复叠加。它们不含完整角色修正、当前充能、补充事件与持续覆盖，开启快照也不证明无限药剂。

## 读原文和引用

`view` 定位已有 JSON，按 pointer/fields/keys/query 和分页读取相关对象；复制返回 native_references 或 evidence_template，核对当次身份。逻辑根由当前 reference_contract 决定，不能把早先查询根/数组索引搬到新评估。

`data` 提供 list/search/read：先定位当前允许的 source，再以真实相对路径和行号读取上下文。截断片段不直接当完整机制。local_data 使用返回 evidence_template 的 source/line/count/content_sha256，由程序重读；它证明原文可追溯，不证明当季可得或解释成立。不读取平台内部目录、私有过程或无关本机资料。

## 联网取得与版本

`research --gems '<当前ID列表>'` 返回原生逐级要求、基础类型/资源与页面 choices。核对章节、任务、NPC、职业、实际前提、奖励选择数量和商店费用；早期某职业奖励行不表示其他职业永远拿不到。后续商店、小号/交易等是否适用按真实用户限制判断。来源模板在 choices[].evidence_templates，正文不足则读真实上下文再下结论。

`research --trials` 的位置/区域等级不证明已通关或实际点数；`research --version` 的真实官方页面不由赛季编号猜机制版。其他公开网站可用可用联网工具，保存 URL、正文片段与实际查阅时间到授权工作区，按[证据](evidence.md)关联。本机数据、网页观测、用户声明和模型解释各自保留范围。
