# 证据、使用条件与交付（schema_version 2）

配装和开荒共用本协议。具体用户目标不套预设答案；程序检查确定性输入、可读取事实与关联，模型评审资料含义、取舍与目标是否完整覆盖。

## 证据、结论与使用方式

每条 evidence：`evidence_id/kind/subject` 必需。其余字段按下列 kind 使用；ID 在 intent+plan 合并后唯一，不以 native: 开头。status/digest/origin 由程序生成。

- user_statement：text 是实际表达，输出 user_declared。用户库存不是程序独立盘点。
- web：url 与正文实际包含的 excerpt，由脚本读取公开官方/PoEDB 页面，输出 source_observed。其他公开来源可用 TeleAgent 联网工具读取，再使用 artifact。
- artifact：path 相对声明源 JSON 所在目录解释（intent 文件目录或 plan/route 文件目录），限该目录及子目录内不超过 2 MB 的文本。source_base 返回实际基准目录，越界诊断给出 /evidence/<位置>/path、允许目录与实际路径；需要的联网/探针记录先保存到该目录。excerpt 必须确实存在，记录摘要与采集 URL/时间。artifact_observed 只证明片段可追溯，不证明文件必定来自声称的网站或其中数字已被原生测量。不能用此路径读取平台内部输出目录。
- local_data：使用 `data read` 的 evidence_template（source/line/count/content_sha256），补 evidence_id/subject。程序从本机重新读取，不接受替代 text/path/url。输出 local_data_observed 只证明原文及摘要，不证明含义与当前方案相关、当季可得或游戏内实测。
- 原生引用使用本次 reference_contract 中的 prefix/fields，无需声明 evidence。next_actions 指向完整契约；字段和证据根来自当前实际结果。view 的 pointer 在输出文件中定位，native 的 locator 则在对应证据根中定位，两者分别使用。配装文件的 `/result` 对应 candidate，`/readiness` 对应 resources；例如持续状态引用 `native:resources#/sustained_use/status`。路线有当次成功计算的 `stage:<id>` 及其派生 `resources:stage:<id>`，未计算阶段没有这些根。只能访问本次已有字段，指针不执行代码；引用值与引擎/计算 XML 绑定，数组索引按当前数据确认。失败时 source_diagnostic 给出现有父对象字段或数组长度及 reference_contract，修实际引用。

claim 使用 `claim_id,text,subjects,evidence_refs,conditions,uncertainties,checks`，可声明 `claim_type`。字符串数组与 checks 格式见下文。可比较命题使用 `claim_type:"comparison"`，checks 必须非空；门槛、指标和取舍由模型选择。默认 judgment 只表示模型解释，不证明数值达标。程序不从自由文字猜测阈值。

通过 edit 新增或修改判断时，程序生成 bindings，绑定所声明对象、来源和当次引擎；保留原判断时，其摘要也保留。对象/来源变化会报告 invalidated_claims，计算时旧判断保持 unresolved。阅读当前事实并重新核对后，用 edit 的 review_claims 提交选定 claim_id，程序刷新摘要。bindings、observations 和状态均由程序生成；直接写入的旧 JSON 可作为草案读取，缺绑定时仍须经 edit 重新评审。摘要绑定不能证明模型解释正确，也不自动解析自由条件的全部语义依赖。

缺引用、来源未解决、过期绑定、uncertainties 非空或 checks 未通过，保持 unresolved。model_assessed 表示模型作了有引用的判断；check_status/verification_scope 才说明所列可比较命题的测量范围。objective_coverage.declared_checks_status 与 publication.declared_checks 展示实际比较，不能把 judgment 当作数值证明。

用户目标逐项使用 `objective:<objective_id>`，对应 intent/route.objectives；每项明确达到/未达到并解释当前对象、依据、条件、代价与未解决项。用户要求的技能、天赋、装备和操作必须在最终方案落实；旧计划文字或关联一个数值不能证明已实现。objective_coverage 只核对来源与关联，不证明目标完整或已经达到。整体 goal 判断说明总体取舍；修复改了组成后重新判断，不继承旧结论。

计算场景也关联当前事实：配装判断使用 subjects:`["configuration:candidate"]`，阶段使用 `["configuration:stage:<stage_id>"]`，引用对应 `native:<计算名>#/config_values`、`config_controls` 或具体字段。config_controls 的 value、default 与 value_basis 分开；false 是关闭，null 是未知。核对当前本机配置原文、进度与实际解析值，决定显式覆盖或明示假设。涉及某效果开关的结论同时声明该实际值的 eq/true 或 eq/false 检查；计数、主体和持续覆盖另找依据，不能由装备存在推定。effect_state 给出护卫、药剂、各实际宝石效果及布尔配置快照，原生观察的 context 同时携带它。缺少当前场景评审会保留 config:scenario；存在引用不自动证明场景选择正确。

决定性排除理由区分使用门槛、属性不足、取得渠道与效果适配；优先用当前原文或最小变体对照核验。局部限制能否通过装备、等级或条件解决，由模型结合实际数据判断，程序不添加职业/宝石专用禁令。

关联写在 **claim.subjects**，不是 claim_id。例如 objective_id 为 `user-X`，解释该目标的 claim 可以用 claim_id:`explain-X`、subjects:`["objective:user-X","goal"]`。一个判断可以关联多个真实适用的对象，不必为每个字段写重复文字。objective_coverage.required_subject 和 diagnostics 给出缺少的关系；claims[].diagnostics 指向失败引用和实际来源原因。先修这些协议/引用遗漏，再决定哪些确实无法核实。

事实优先使用完整占位引用，例如 `text:"{{fact:native:candidate#/stats/Life}}"`，程序生成 rendered_text 并将引用纳入 evidence_refs。observations 保留 origin/status/scope，区分来源实际值、程序派生投影与模型解释；新接口展示已有数值不等于首次测得。meaning 含当次原生显示 label/format、可辨认的 unit/display_multiplier、actor 和限定归属 object_identity；context 含 XML/引擎摘要、职业、主技能和实际配置。相同名称不能替代限定对象。单位或显示比例不明/冲突时保持 unknown，不从字段名猜单位。`{{value:...}}` 保留原始标量用于已有输入，模型自行添加单位不受程序证明。完整事实可展示数组描述；value 只支持标量。缺字段保持 unresolved，不能替换为零。

可比较命题用 `checks:[{"reference":"native:candidate#/stats/Life","op":"ge","value":3000}]`，阈值由本次用户目标/模型理由决定，示例不规定目标。op 为 eq/ge/le；数值按**原始值**比较，eq 也支持同类型布尔/字符串，布尔不当数字。budget 与 resources 是当前程序核验结果，可引用 `native:budget#/within_limit` 或 `native:resources#/timeline/status`。预算确认示例为 eq/true；缺值、null 或异类型为 unknown，矛盾为 refuted，带 expected/actual 的诊断不能由文字或自审 token 消除。checks 证明精确命题，不能证明任意自由文字、因果、来源可信度或整套实战可行。

条件依据与最终验收分别记录：报价、库存、充能启动等前提引用实际来源/原生值；最终判断可引用 budget/resources。核验报告不能反过来充当生成它自己的前提，循环引用不会补齐该项依据。引用绑定规范化后的当前 XML，原生调用的运输格式差异不作为新方案身份。

“测得某值”“机制导致某值”“实际覆盖该条件”“满足用户目标”分别需要对应依据。引用一个面板数值不证明自己的因果解释。探测先核对 damage_observation 的技能、主体、XML 与场景；另一技能的指标不变，不能排除被改组件的作用。与解释冲突时，先检查当前支付资源、主体、实际装备、配置和明细；必要时只改一个输入探测，再说明其影响范围。模型可以选择不同使用条件或接受代价，但须把条件落进对应候选/阶段，不用假想实战效果覆盖当前值，也不以无依据的“引擎问题”解释异常。已有数值而缺少启动/持续覆盖，与根本未读到数值分别表述。

常用 subject：`goal`、`progress:normal`、`progress:ascendancy`、`progress:secondary_ascendancy`、`condition:guard`、`condition:Flask 1`、`config:<字段>`、`skill:<skill_ref>`、`dependency:<skill_ref>`、`count:<组>:<宝石位置>`、`resource:Mana:leech`、`resource:EnergyShield:recharge`、`transformation:<槽位>`、`legality:item:<物品ID>`、`migration`、`template_source`。以实际 pending 为索引说明问题；permission、缺失原生量和 capability 缺口不能靠填理由转成程序通过。

`assessment:{goal_claim_ids:["总体claimID"],scope:"实际核对范围",unresolved:[]}`。所选每条判断须在 subjects 中关联 `goal`，并已针对当前证据评审；`objective:<id>` 的逐项目标关联另由 objective_coverage 检查，不能把只有该关联的 ID 当总体判断。goal_assessment 诊断给出具体数组位置、当前 subjects/status 与可用总体 ID；按实际用途选记录或补关联，不改输出状态。goal claim 引用用户目标与本次数值/来源，解释收益、代价、是否满足。scope 覆盖实际用户要求。

简单频率/同一窗口用 usage，每行 `{skill_ref,casts_per_second,active_seconds,recovery_seconds,continuous,claim_id}`。skill_ref 来自本次 active_skills，对应 claim.subjects:`skill:<引用>`。频率为次/秒，窗口为秒，有限非负；未知省略。程序核对原生动作频率、单次支付和满速费用，只有费用符合单次支付乘频率时才线性投影。同一窗口可合并，不同窗口不会借用主技能窗口放行；混合动作使用下文 timeline。降频不沿用满速吸取，未重算的 DPS 仍为原生快照。资源缺口和启动前不足不能被 prose 消除。

readiness.single_payments 列出所有当前启用技能对三资源的独立单次支付、可用储量、原生定位和三值状态，包括尚未声明 usage 的副技能。每项从满可用储量独立检查，不证明连续动作或任意时点都可支付；生命支付后须存活，EB 与能盾恢复上限从当前原生规则读取，缺字段保持 unknown。resources.snapshot_balance_nonnegative 只比较当前账目中的声明频率与假设回复。timeline.resources[].initial_pool_restored 仅表示窗口支付成功且终点恢复起始储量；readiness.resources 中对应事实为 timeline_initial_pool_restored。旧 repeatable/ repeatable_at_native_regen_only 字段已删除，重复支付统一看 sustained_use 的当前状态及范围；finite_window_only 为 unknown。execution_coverage 另列整套动作实际执行、触发与效果覆盖的未测量部分。无共同时间线时，即使面板吸取大于消耗也不能写成已证实持续恢复。

每行 mean_use 给出当前技能原生满速 cost_per_second、fixed_net_regeneration_per_second 及相减后的 net_drain_per_second。固定净回复优先用当前 Net<Resource>Regen，否则要求同时读到回复和持续伤害；缺值不填零。mean_pool_depletion_seconds 仅在正净消耗下给平均储量耗尽估算，生命保留正储量；非正净消耗或缺值为 null，不宣称无限续航。它是独立平均投影，不模拟离散扣费、其他动作、命中吸取、充能、药剂或效果变化；准确动作支付与混合节奏用 timeline。

共同时间线为 `timeline:{duration_seconds:时长秒数,repeat:false,events:[{skill_ref:"当前引用",at:窗口内秒数}]}`。模型按目标选择：repeat=false 检查一次有限窗口；repeat=true 检查重复周期，省略时保留已有周期输入的语义。动作处于 `[0,duration_seconds)`，按时间排序；同一时刻按输入顺序支付，不推断不同技能可以同时执行。最大1000动作是工程限制，较长计划分批表达。程序逐次扣原生费用，在动作间按固定净回复补充到可用上限，核对同技能动作间隔；重复周期另查循环接缝。Life/魔力/能盾分别记录初始、trace 和最终值；EB 从当前原生支付规则读取。

时间线检查范围只有离散费用和固定净回复，未计敌人伤害、额外资源损失、吸取、能盾充能、药剂补充、动作互斥或条件变化；缺项可能使实际更好或更差。自动技能可投影声明的固定原生频率支付：读取 SkillTriggerRate 与每秒费用，模型选相位并在 events 安排完整窗口，程序检查相邻间隔、首尾缺口和重复接缝，同时核对每秒费用等于单次费用×该频率。automatic_schedules 说明所验证的支付安排；实际触发、启动、目标和效果变化仍未测得。缺安排为 edit_input，缺原生触发频率/支付规则、非离散费用或回复量为 unknown；零费用自动技能无需虚构支付。手动技能频率可由当前原生速度或零速度技能的冷却倒数得到，读取 usage_rate.basis/metric/repeats，不按技能名猜。有限窗口支付通过只证明所列储量、固定回复和动作；sustained_use 仍保持 unknown。重复周期还需接缝合法且结束恢复起始储量；平均回复够但瞬间不够为 refuted。模型仍须检查操作、冷却、目标和实际覆盖范围。使用 timeline 时，usage 仅关联 `{skill_ref,claim_id}`，不重复定义另一组频率/窗口。阶段使用 snapshot.timeline，规则相同。

原生 `equipped[].flask_local` 是单件药剂参数，`flask_scope` 明示不包括角色修正、当前充能、补充事件或实际覆盖。开启药剂的快照若声明 continuous，程序会保留 `capability:flask_coverage:<槽位>`；当前适配没有完整覆盖时序测量。可以研究具体有限窗口与缺口，但不能只写“有药剂”就宣称无限续航。

## 库存和采购

requirements 从实际 XML 差异产生，包含仍装备的禁用/备用宝石、规格变化、装备改造和退点资源。阅读 evaluation.budget.materials，不能用“原 XML 没有，所以用户肯定没有”代替询问。

物资公共字段：kind、quantity、evidence_refs，可选 description。gem 用 skill_id/level/quality；item 用 spec_sha256（读取当前 requirement_id/material_specs，不手算），可选 item_id 仅指某个 XML 内的编号；service 用 service_id。库存写 inventory，报价写 shopping，均不能自填 status。

库存 evidence.subject 为 `gem:<skill_id>:<level>:<quality>`、`item:<spec_sha256>` 或 `service:<service_id>`，直接沿用程序返回的 requirement_id。context.quantity 对应用户实际确认数量，装备还匹配 spec_sha256。同规格合并数量，依据不重复。同一条用户声明不反复累计数量。两个阶段都出现 item_id:1 不表示同一规格；相同规格用不同 XML 编号仍按同一规格累计，实物 instance_id 用于首次取得与后续沿用。

报价另含 low/high/currency：low/high 是**该整条 quantity 的总价**。来源 context 需对应 realm:"cn"、league、currency、quantity、low、high，装备还需 spec_sha256，并有实际 checked_at。`quote:<物资引用>` claim 必须引用同一价格记录，解释规格与语义匹配。只写 URL 或 verified_cn 会被拒绝。国际服报价不计成国服已核实价格；无汇率依据不自动换算。

程序区分 unknown、uncertain_range、quote_assessed、user_declared、no_additional_cost、over_budget。within_limit 为三值：已核对完整来源且上界不超过限额为 true，已知必需下界超过为 false，其余为 null；模型估算不提升为 true。totals_complete=false 时金额仅为已列部分。quote_assessed 仍有交易时效条件，非成交保证。

## 结果、完整性与交付

evaluate 保存 candidate.xml、requested.xml、plan.json、evaluation.json、review.json。读取 result 的实际技能、装备、配置、统计与伤害分项，readiness 的资源、errors/pending、budget、claims/evidence 和逐类 validation。diagnostics 带 code/category/subject/evidence_refs/affects/message/status；未知能力不改写成游戏非法。默认值等价性按本机 ConfigTab.GetDefaultState/Save 语义检查，同时核对统计；没有一律丢弃 false。config_values 记录最终保存文件的实际输入，config_saved_values 是省略原生默认值后的保存语义。

delivery_brief 从全部 errors/pending 生成状态和缺口类别；next_actions 指向实际输入与完整详情，先修错误、评审目标/依赖，再复核当次结果。终端只显示部分 pending 时仍含全部类别，不能漏项或用删功能让缺口消失。actual_changes/equipment_changes 来自当前对象和 XML 规格，普通/主升华/次级升华点数分别计数，不把“新增节点总数”再次加算升华。

最终复核使用现有 TeleAgent 模型，不增加另一模型服务。先完成方案/claims 修订并 evaluate，再阅读 review.json 的 facts/claims：核对数字与技能/主体/阶段、因果证据、替代方案排除理由、切换进入条件以及 summary/迁移说明/最终回答的范围。发现问题就修改并重新 evaluate，直到可以如实记录本轮复核。填写源 plan 的 `assessment.review`，再 evaluate：

```json
{"token":"本次 review.json 的完整 token","findings":"整份方案的核对结果，包括各条说明是否一致","unresolved":[],"objectives":[{"objective_id":"当前实际 objective_id","scope":"此要求已实现及尚未实现的范围","findings":"对应对象、测量、来源、代价和未解决条件"}],"conclusions":[{"claim_id":"当前实际 claim_id","scope":"此条结论成立的对象、场景及范围","findings":"实际依据、条件、发现与取舍；解释和已测量部分分别说明"}]}
```

先读取 next_actions.review_current_result 指向的当前 facts.objective_coverage 和 facts.pending，再按 review_template 逐项评审。模板列出所有用户 objective_id 和判断 claim_id，scope/findings 留空；objectives 与 conclusions 分别覆盖当次全部 ID，缺 claim 的目标也必须记录未实现部分。重复 ID 报具体位置，陌生 ID 及遗漏保留 draft，coverage 列出差异。只有总评或只有判断评审的旧记录可追溯，但不能代替完整目标复核。程序只核对覆盖与摘要；这些文字不能消除原有物资、资源、目标或机械缺口，也不证明目标已经达到。

token 绑定意图、方案、文字、引用内容、最终 XML/引擎、资源/预算和数值；改变后旧复核失效。reference_contract.identity_references 区分 character（当前原生角色状态）、input_bytes（提交给引擎的 XML 字节）与 saved_bytes（声明保存文件字节）。判断角色一致性选 character 的实际引用；原生保存可能重排 XML，使 input_bytes 改变，即使角色不变。显式引用的文件摘要改变仍须复核，不能删掉摘要校验强行放行。`final_review:missing/stale` 保留 draft，按 export 后的新 review/next_actions 核查；findings/unresolved 不改写机械矛盾或其他缺口。此步骤只证明模型复核针对这一版，不证明语言无误；不要用脚本自动填 token，不能以复核记录宣称全部机制验证。

候选名称为1–48位小写英文/数字/连字符。原件、意图、请求 XML、候选记录/文件及引擎内容都有完整性校验；直接手改输出会要求重算。baseline 缓存按意图、共同场景、引擎及适配版本区分，各候选保留自己的基准。

probe 不改候选文件，可用于 blocked/draft 中仍可计算的输入。返回各变体原生快照、差异与未评审依赖；baseline_damage_observation 和各行 damage_observation 记录当次选定主技能、skill_ref、组、主体、显示定义、XML 与实际配置。dps_metric 按各行当前主技能选择，不假定它等于被修改对象；其他技能的影响应选择对应 main_group 或读取有主体的分项再实算。零差值只支持所测对象在该场景下数值不变，不证明某机制无效。不继承旧宝石的使用结论，最终选择仍写完整 plan/evaluate。批量大小为进程资源保护，自动分批比较。

export 再复算并重核来源和复核 token。blocked 只输出诊断 HTML；draft 输出配装草案 XML 与说明；scoped_result 输出改进 XML 与说明。目录为 `run/output/<候选名称>/`，附 final-assessment.json、review.json、delivery.json。只有 XML 与 HTML 均落盘后 delivery.file_exported 才为 true，清除同候选旧状态的另一 XML 文件；可捕获失败记录 failed/last_operation，并取消本次交付成功身份。进程被强制终止仍可能留下 preparing/exporting，由 status 核对后恢复。无 URL 的本地证据仍正常展示。

publication 是 HTML、最终核验与 facts_summary 的共同事实来源，包含全部缺口类别、实际数值、所列比较和预算/时间线的三值结论，以及当前 capabilities。资源 window.final 是 duration_seconds 时点的储量，包含末次动作之后的固定净回复；trace 最后一行是最后动作时点，不能互换。execution_coverage 单列整套动作可执行性的未测范围；各资源支付通过不能替它背书。delivery_brief.valid_statuses 列出真实交付状态；next_actions 区分可修复依赖与接口能力缺口。聊天先沿用它的状态与事实，再解释模型取舍；预算 null、时间线 unknown 保持对应条件。尚未支持的测量不能靠补输入或填写 token 变成已支持，未来取得/进度也不必伪装成已经发生。checks 与自审仍不证明全部机制或实战效果。

直接交付返回路径，避免另做未经绑定的复制/改名/说明。draft 不改名为改进配装；聊天先说明 delivery_brief 的真实状态与全部剩余类别，再说明已有收益、代价和用户下一步。原生能力缺口可以保留为条件规划；已知非法输入、可修复字段遗漏和来源404不能靠填写复核 token 变成成品。

历史输入迁移：calculation_mode、resource_mode、unused_points_reason、config_evidence、burst_seconds、upgrade_gems、fallback_skill_id、version_status、verified_cn。分别迁入具体 usage、claims、阶段或来源；旧字段显式报错，不保留双套运行路径。
