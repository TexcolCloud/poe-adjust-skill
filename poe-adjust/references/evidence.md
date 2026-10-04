# 证据、使用条件与交付（schema_version 2）

配装和开荒共用本协议。具体用户目标不套预设答案；程序检查确定性输入、可读取事实与关联，模型评审资料含义、取舍与目标是否完整覆盖。

## 证据、结论与使用方式

每条 evidence：`evidence_id/kind/subject` 必需。其余字段按下列 kind 使用；ID 在 intent+plan 合并后唯一，不以 native: 开头。status/digest/origin 由程序生成。

- user_statement：text 是实际表达，输出 user_declared。用户库存不是程序独立盘点。
- web：url 与正文实际包含的 excerpt，由脚本读取公开官方/PoEDB 页面，输出 source_observed。其他公开来源可用 TeleAgent 联网工具读取，再使用 artifact。
- artifact：path 指向本次任务目录内文本，excerpt 必须确实存在，记录内容摘要与采集 URL/时间。输出 artifact_observed，代表片段可追溯，不假装文件必定来自声称的网站。不能用此路径读取平台内部输出目录。
- local_data：使用 `data read` 的 evidence_template（source/line/count/content_sha256），补 evidence_id/subject。程序从本机重新读取，不接受替代 text/path/url。输出 local_data_observed 只证明原文及摘要，不证明含义与当前方案相关、当季可得或游戏内实测。
- 原生引用直接写 `native:candidate#/stats/Life`、`native:baseline#/stats/Life` 等 JSON pointer，无需声明 evidence。只能访问本次已有字段，指针不执行代码。引用值与引擎/计算 XML 绑定；不要猜旧数组下标。

claim 的字段为 `claim_id,text,subjects,evidence_refs,conditions,uncertainties`。subjects/evidence_refs/conditions/uncertainties 均为字符串数组；claim_id 唯一。缺引用、引用不存在、来源未解决或 uncertainties 非空，保持 unresolved。程序附上 observations 的真实值、对象上下文和摘要；不得手写这些输出字段。model_assessed 表示模型作了有引用的判断，**不表示程序证明语义充分**。最终 review 另行绑定，不能让旧文字自动得到新方案的复核身份。

用户目标逐项使用 `objective:<objective_id>`，对应 intent/route.objectives；每项解释实现、依据、条件、代价与未解决项。`objective_coverage` 只核对用户来源及关联，不独立证明目标拆分完整或已经达到。整体 goal 判断仍说明总体取舍。

数值判断优先使用原生占位引用，例如 `text:"生命 {{value:native:candidate#/stats/Life}}"`。程序生成 rendered_text，引用也自动纳入 evidence_refs；缺字段保持 unresolved，不能替换为零。HTML 使用同一 rendered_text，最终聊天沿用 export.facts/facts_summary；自由文字中的因果和替代方案排除仍需复核。

常用 subject：`goal`、`progress:normal`、`progress:ascendancy`、`progress:secondary_ascendancy`、`condition:guard`、`condition:Flask 1`、`config:<字段>`、`skill:<skill_ref>`、`dependency:<skill_ref>`、`count:<组>:<宝石位置>`、`resource:Mana:leech`、`resource:EnergyShield:recharge`、`transformation:<槽位>`、`legality:item:<物品ID>`、`migration`、`template_source`。以实际 pending 为索引说明问题；permission、缺失原生量和 capability 缺口不能靠填理由转成程序通过。

`assessment:{goal_claim_ids:["目标claimID"],scope:"实际核对范围",unresolved:[]}`。goal claim 引用用户目标与本次数值/来源，解释收益、代价、是否满足。不得以改变 scope 隐去用户已经明确要求的部分。

usage 每行是 `{skill_ref,casts_per_second,active_seconds,recovery_seconds,continuous,claim_id}`。引用 evaluation.result.active_skills 本次真实 skill_ref；claim 关联 `skill:<同一引用>`。频率为次/秒，窗口与恢复为秒，有限非负；未知就省略，不能编数。每个手动技能分别填写实际频率，包括0次的场景也解释。原生 usage_rate 提供动作频率及来源；程序核对每个技能的频率、支付资源和原生满速费用。只有原生费用符合单次支付乘动作频率，才线性投影；按秒费用或其他不匹配情况保持 capability 缺口。各技能同一组使用/恢复窗口可以合并核算；不同窗口的时序暂未实现时保留 timing 缺口，不能借用主技能短窗口放行其他技能。声明 continuous 的技能独立检查无限使用所需资源；药剂仍需实际覆盖依据。程序核对单次支付、可核算的线性费用、使用窗口与恢复；不能可靠推导的时序留 capability 缺口。默认展示原生满速费用；不要求全部方案满速持续。净缺口没有使用窗口、充能启动前储量不足，不因 prose claim 存在而消除。降频不能沿用满速命中吸取来证明续航，未重算时保持 unknown；DPS 同样不能当节奏调整后的持续输出。

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

历史输入迁移：calculation_mode、resource_mode、unused_points_reason、config_evidence、burst_seconds、upgrade_gems、fallback_skill_id、version_status、verified_cn。分别迁入具体 usage、claims、阶段或来源；旧字段显式报错，不保留双套运行路径。
