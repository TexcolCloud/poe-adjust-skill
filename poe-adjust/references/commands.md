# 入口与调用约定（schema_version 2）

所有 JSON 为 UTF-8，使用实际数字、布尔值和数组。通过文件工具或标准 JSON 序列化保存请求，路径可用正斜杠；命令只引用已保存文件，避免多层 shell/Python 拼接转义。PowerShell 路径用单引号，每次独立调用重新定义变量或使用绝对路径。输入路径沿用用户提供位置，结果路径沿用本次返回。

## 命令入口

首次脚本位置来自技能加载的资源目录，Python 使用 TeleAgent 当次运行时。每次 CLI 返回 entrypoint 的 script/python/skill_version，分别是本次实际绝对路径与适配版本；大结果摘要仍保留入口。之后用该解释器和脚本调用，不复制机器用户名或旧安装位置。恢复先读任务已有 `.edit.json`、`run.json`、`.state.json` 的入口，再 status/next_actions。安装迁移后按当前加载位置重新 doctor；没有可靠入口时记录定位缺口，不向整个平台或用户目录扩大搜索。下列 python 示例由该实际解释器执行。

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
python $entry probe --input '<已有角色.xml>' --variants '<工作区>/variants.json' --metrics '<实际指标>' --out '<工作区>/probe.json'
python $entry decode --input '<实际纯分享码.txt>' --out '<工作区>/template.xml'
```

所有子命令接受 `--pob '<PoECharm/内部PoB目录或程序>'`，正常自动发现。start 已做运行自检，目录须为空或不存在，之后始终使用同一 run。inspect/decode 不覆盖输入。网址与 HTML 不是纯分享码；有需要才从实际公开来源取得完整分享码，不抄写或拼接被截断内容。

start 返回原始 overview 与检查文件路径；`inspect --calculate` 返回完整 calculation，保存到文件时终端只显示诊断摘要。inspection 的原 XML 字段是 `skill_groups[].gems[].skillId`；计算结果是 `gems[].id`。数字以实际 stats 为准，Build/PlayerStat 只是缓存。CountAllocNodes 的 points/ascendancy_points 已含连接小点，不能从已分配量反推已完成的任务。

probe 的来源二选一：已有 evaluate 候选用 --run/--name，已有角色只读实验用 --input，后者不指定 --run/--name。两者沿用同一 variants.operations，输出实际效果状态及指标，均不改输入。source_mode 明示来源；只读观察不等于交付达标。格式与复核方法见角色配装协议。

旧 schema 会话只读追溯。请用新空目录 start，从旧记录提取有依据的意图，以新格式重建；不可直接 export。未确认偏好不从旧默认 false 迁移。

## 按当前任务读取

| 当前操作 | 读取资料 |
|---|---|
| 已有角色优化、换流派、写 intent/plan、比较候选 | [角色配装](build.md) |
| 无角色 XML 的开荒、成长、关键阶段快照 | [开荒路线](leveling.md) |
| 查询对象、合法选项、描述、机制或联网事实 | [资料查询](data.md) |
| 写判断、资源使用、库存/采购、最终复核与交付 | [证据协议](evidence.md) |

不要预先读取所有参考文档。同一事实只维护一份；当前合法值用查询结果，不从例子复制版本、职业或上限。参数细节可运行 `python $entry <子命令> --help`。

## 增量保存方案

保存返回 input_contract，top_level_fields 和 field_owners 直接来自当前验证器。按归属修改源文件：intent 保存配装用户目标，plan 保存配装操作/判断，route 保存路线目标与阶段。完整凭证保留此契约；大响应摘要通过 next_actions 指向它。契约只说明顶层字段，嵌套格式仍按对应协议。

方向确认后先保存小草案，再补当前阶段或操作。edit 只维护输入与标识，不调用计算、代替模型选配装或确认游戏合法性。intent 仍使用 intent 命令，模型最终完整输入可直接 evaluate/route。

草稿允许缺少尚未确定的字段，已填写值仍必须满足接口格式；set/replace/remove 都校验最终文档。game_version 只接收已确认的 `3.<整数>` 或未知 `null`；版本说明写 notes/version_source，不能从混合文本提取数字冒充确认。职业、宝石、天赋、专精、插槽和词缀选项由当前原生数据核对。集合保留唯一稳定 ID，整表 replace 也不能重复或遗漏标识。

```powershell
python '<技能目录>/scripts/poe.py' edit --invoked --kind route --file '<工作区>/route.json' --changes '<工作区>/change.json'
python '<技能目录>/scripts/poe.py' edit --invoked --kind route --file '<工作区>/route.json' --changes '<工作区>/change.json' --if-sha256 '<上次revision>'
```

plan 使用 `--kind plan`。change.json 的四种操作共用一个入口：

- `set`：对象逐字段合并，null 保留为未知值。stages/claims/evidence/objectives 按 *_id 合并；operations 的 items/equip/groups/gems 按 id/slot/index/(group,index) 合并，适用于 snapshot.operations。新字段可直接写入数组。
- `replace`：现有 JSON pointer 到新值的映射，用于精确字段修改或明确替换整数组。例如 `{"replace":{"/stages/1/skill_groups/0/use":"遇到危险时使用"}}`。先用 view 核对实际位置并取得 revision。改变已有非空普通数组必须使用 replace，避免把局部内容当整数组保存。
- `remove`：明确删除的 JSON pointer 数组；全部路径定位同一份 set/replace 后、删除前的文档。例如删除 `/stages/0` 和 `/stages/1` 删除原来的第0、1项。拒绝重复目标或父子路径混删；先核对返回的稳定 stage_id，再取本次索引与 revision。
- `review_claims`：核对当前对象后的 claim_id 数组，如 `{"review_claims":["explain-stage"]}`。程序刷新这些判断的依赖摘要；新增或实质修改判断也会绑定当次对象。

新增顶层记录省略标识时程序生成，返回 handles；路线省略宝石 instance_id 时返回 stage_instances。跨阶段复用同一颗宝石必须沿用返回的实例 ID，同名多颗不会自行合并。已有记录使用该标识更新；省略标识的另一内容视为新增。required_items 宝石省略实例仅在同阶段唯一同 skill_id 时补入，有歧义则报位置错误。

每次沿用返回 revision；源文件变化会拒绝陈旧修改，同一成功请求仅在源文件和引擎上下文一致时幂等重放。counts、actual_changes、invalidated_claims 和 facts_summary 是实际操作结果；大结果完整保存在 receipt_file，通过 view 分页读取。

修改使判断失效时，next_actions 指出需要重新评审的 ID。先重算读取当前 observations，再修正文句和引用并提交 review_claims；程序不自动让旧结论通过。HTML 将未同步判断标为待复核，摘要在本次最终复核后展示；当前实际改动和数值始终可读。

判断草案可以先只存 claim_id，随后用同一标识补 text/subjects/引用；保存草案不等于完成判断，完整评估仍要求正文及关联。

artifacts 标注文件用途：source 是可编辑方案，persistent_state 是持久状态与重放凭证，temporary 是程序可清理的临时文件清单。`.edit.json` 属于 persistent_state，交付与恢复期间保留；程序自行处理内部临时文件。按 facts_summary 报告结果，直接使用 counts，不重新猜数量。

保存采用完整文件原子替换；版本摘要是乐观检查，多个写入方仍须顺序修改同一源文件。凭证缺失、损坏或写入失败时，重新 view 源文件取得当前 revision，再用 edit 恢复；旧重放信息可能已经丢失，不能承诺原请求已被去重。

输入 JSON 和修改请求上限 8 MiB，按合并后实际 UTF-8 编码字节检查再提交；程序证据/状态 JSON 上限 64 MiB，view 可读相同范围。所有派生输出先按实际路径检查与输入、模板、引用资料、收据和其他输出的冲突，不能借换后缀覆盖源文件。源文件和收据各自原子写入，两个文件不是跨文件原子事务；失败 diagnostic 的 source_committed/receipt_committed/current_revision 如实报告当前状态。先 view 恢复，不用旧 revision 重试覆盖新源方案。

## 采用原生候选

evaluate/route 的 `next_actions[].action:review_native_proposal` 给出本次提案的 `details_file/pointer/proposal_id/input_revision`。先用 view 的 `preview_pointer/preview_fields` 读实际点数和缺口；进入该预览的 `/comparison` 用 keys 找本次指标，再 fields 选择与用户目标有关的收益/损失，`/active_skills` 分页核对费用，`/readiness` 核对资源范围。完整数值已保存，不整份重复输出。模型决定接受、补属性、延后或改目标；程序不自动采用。接受后引用已有凭证，无需重抄节点、宝石或完整 changes：

```powershell
python $entry edit --invoked --kind route --file '<本次input_file>' --proposal-file '<本次details_file>' --proposal-id '<本次proposal_id>' --if-sha256 '<本次input_revision>'
```

配装使用 `--kind plan`。此入口与 `--changes` 互斥，复用相同的原子保存、标识、判断失效和幂等凭证。提案绑定唯一源文件、修订、当前技能/引擎和完整计算凭证，原角色、已确认意图及外部模板变化也会拒绝旧提案；先重算取得新提案。天赋提案已知超点或移除冲突不能直接采用；未声明点数仍是待核实草案。保存只表示采用输入，随后重跑 evaluate/route，检查所选目标节点和专精真正保留、全部属性/技能/资源及每项用户目标。旧评审不会跟随修改变为通过。

XML 由当前 PoECharm `Build.SaveDB` 在内存中保存，再由程序写入任务工作区并冷回读；模型提交结构化操作和条件。原始自定义扩展仅由程序保留，原生语义和全部统计仍需一致。中间请求 XML 与最终回读 XML 分开记录。显式主技能与技能状态还需保留请求含义；回读两次一致不能替代此项检查。原生批次逐条返回 request_id，程序校验完整性及对应关系；probe 每个变体都有结果或具体失败，calculated_variants 表示实际通过的计算数，partial/failed 不表示全部已测完。

## 错误与恢复

失败返回 error 与 diagnostic；有 location 时按 expected/actual 修对应 JSON 路径，input_file 指出源文件。共用的证据、判断、目标、约束映射和评审结构在写入/原生查询前检查；约束映射值是单个 evidence_id 字符串。actual:null 表示字段缺失或值为空。输入错误修源文件；inspect_capability 的能力缺口先核对 doctor/publication 的 capabilities，再由模型选择方案或保留条件。

input_revision 指出出错时的源修订。invalid_input/input_too_large 修输入；input_revision_stale/proposal_context_stale 重读并重新绑定；unsupported_context/unsupported_operation 需匹配当前能力或改变操作；io_error 检查文件状态。retryable=false 表示原样重试无用，允许修正后提交；仅已识别的临时 I/O 故障可原样重试。adapter_contract_failure 是未预期的接口异常，记录完整位置交维护者，不能改游戏数值绕过。

最终复核单独提交：完成方案与未解决项修改，重算取得当前 review.json，再仅写 assessment.review。同一请求同时改方案和提交复核会被拒绝，避免旧 token 混入新输入。review_claims 是对象判断的重新评审，assessment.review 是完整结果的最终复核，两者用途不同。

大结果用现有 --out 保存到任务工作区，再用 view 按需读字段、字段名或文本缺口，具体示例见资料协议。delivery_brief 汇总全部缺口；next_actions 指向完整文件和具体路径，按实际错误、目标/依赖、最终复核处理，不因文件已导出而跳过。stage_summary 和 errors 不能因为摘要只显示部分 pending 而略过。

完整终端回复包含 entrypoint/runtime 后仍受 12,000 字符限制，continuations 给出每个缩略字段的完整凭证路径。view 的 page_unit 是 items（数组）、fields（对象）、characters（字符串）或 scalar；沿用 next_offset 继续同一 pointer，offset 按该单位计数。数组/对象 limit=1–100，长字符串可用 limit=4000（最多12000，自动收缩到可返回大小）。单条记录仍过大时，deferred_values/next_actions 指向其子 pointer；读取子字段，再继续父字段的 next_offset。嵌套字段名通过 JSON pointer 转义；无需读取平台内部工具缓存。

配装使用 `status --run`，读取 candidate_records 的完整性、待办及源 plan/review 文件路径，按 next_actions 继续。失效候选保留已知 plan_file/details_file/review_file、当前 input_revision 和 diagnostic；重新 evaluate 使用返回的源文件与 run，源定位未知时先读取已有记录，不猜路径。last_operation 记录最新评估或导出；成功重试更新旧失败，重新评估使同候选旧交付凭证失效，原产物保留供追溯，须重新 export。失败导出不保留成功交付身份。开荒使用 `status --route-state <输出名.state.json>`，失败记录 phase:failed、failure 和具体修复动作；沿用记录的 pob_path，显式 --pob 可覆盖并重新核对指纹。交付完整性包括报告和所有声明的阶段 XML；missing/changed XML 列入 invalid_artifacts，file_exported=false，重跑同一 route 修复。completed_stages 仅表示该输入/引擎下历史原生计算完成，不保证当前 XML 仍存在；有效阶段缓存自动复用，资料与模型判断重新核对。旧状态缺 XML 摘要时重新 route，不能直接继承交付成功。
