# 入口与调用约定（schema_version 2）

所有 JSON 为 UTF-8，使用实际数字、布尔值和数组。文件路径用 PowerShell 单引号，通过文件传 JSON。每次独立工具调用重新定义变量或直接使用绝对路径，Python 使用 TeleAgent 运行时。输入路径沿用用户提供的位置，结果路径沿用本次 CLI 返回；恢复先 status/next_actions，不从环境变量猜平台工作空间路径。

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

## 按当前任务读取

| 当前操作 | 读取资料 |
|---|---|
| 已有角色优化、换流派、写 intent/plan、比较候选 | [角色配装](build.md) |
| 无角色 XML 的开荒、成长、关键阶段快照 | [开荒路线](leveling.md) |
| 查询对象、合法选项、描述、机制或联网事实 | [资料查询](data.md) |
| 写判断、资源使用、库存/采购、最终复核与交付 | [证据协议](evidence.md) |

不要预先读取所有参考文档。同一事实只维护一份；当前合法值用查询结果，不从例子复制版本、职业或上限。参数细节可运行 `python $entry <子命令> --help`。

## 增量保存方案

方向确认后先保存小草案，再补当前阶段或操作。edit 只维护输入与标识，不调用计算、代替模型选配装或确认游戏合法性。intent 仍使用 intent 命令，模型最终完整输入可直接 evaluate/route。

```powershell
python '<技能目录>/scripts/poe.py' edit --invoked --kind route --file '<工作区>/route.json' --changes '<工作区>/change.json'
python '<技能目录>/scripts/poe.py' edit --invoked --kind route --file '<工作区>/route.json' --changes '<工作区>/change.json' --if-sha256 '<上次revision>'
```

plan 使用 `--kind plan`。change.json 为 `{"set":{要更新的字段},"remove":["/明确要删除的字段"]}`。对象逐字段合并，null 保留为未知值；普通数组整体替换。顶层 stages/claims/evidence/objectives 按各自 *_id 合并；operations 的 items/equip/groups/gems 按 id/slot/index/(group,index) 合并，适用于阶段 snapshot.operations。删除记录用明确 JSON pointer；先 view 核对本次位置。不是自定义表达式或脚本语言。

新增顶层记录省略标识时程序生成，返回 handles；路线省略宝石 instance_id 时返回 stage_instances。跨阶段复用同一颗宝石必须沿用返回的实例 ID，同名多颗不会自行合并。已有记录使用该标识更新；省略标识的另一内容视为新增。required_items 宝石省略实例仅在同阶段唯一同 skill_id 时补入，有歧义则报位置错误。

每次沿用返回 revision；源文件变化会拒绝陈旧修改，同一成功请求可幂等重试。receipt_file 保存完整标识，大结果通过 view 分页读取。保存采用完整文件原子替换；版本摘要是乐观检查，多个写入方仍应顺序修改同一源文件。若凭证落盘失败但源文件已更新，重新 view 源文件/核对摘要后恢复，不盲目覆盖。

## 错误与恢复

失败返回 error 与 diagnostic；有 location 时按 expected/actual 修对应 JSON 路径，input_file 指出源文件。证据对象与查询数组错误会定位到条目/字段；actual:null 表示字段缺失或值为空，不表示未提供诊断。edit 先检查证据字段/类型，不联网；错误不会覆盖已有源文件。native_invalid_input 是当前原生规则明确拒绝的输入，action:edit_input 不表示接口不支持；inspect_capability 表示测量接口范围，其他引擎失败先查实际错误。不要盲目原样重试或解析英文报错猜字段。输入问题修源文件，能力缺口不能通过填理由消除。

大结果用现有 --out 保存到任务工作区，再用 view 按需读字段、字段名或文本缺口，具体示例见资料协议。delivery_brief 汇总全部缺口；next_actions 指向完整文件和具体路径，按实际错误、目标/依赖、最终复核处理，不因文件已导出而跳过。stage_summary 和 errors 不能因为摘要只显示部分 pending 而略过。

配装使用 `status --run`，读取 candidate_records 的完整性、待办及源 plan/review 文件路径，按 next_actions 继续。失败记录 last_operation，失败导出不保留成功交付身份。开荒使用 `status --route-state <输出名.state.json>`，失败记录 phase:failed、failure 和具体修复动作；检查输入/输出/引擎是否一致，变更后不把旧阶段算成本次完成。恢复重跑同一 route，有效计算缓存自动复用，资料与模型判断重新核对。
