# 调用与恢复（schema_version 2）

## 命令入口

入口是当前加载的 `scripts/poe.py`，使用可用 Python；doctor 返回实际解释器、脚本、本机安装与能力。恢复用这些真实路径，勿凭包名猜旧位置。`--help` 查当前参数，`input_contract` 查 JSON 字段和归属；本协议只说明容易误用的调用语义。

PowerShell 路径分别加引号、用绝对路径调用；JSON 是 UTF-8 文件，优先用可靠序列化或 here-string 保存，不把复杂 JSON 拼进命令行。下面的占位路径替换成本次实际值：

```powershell
python '<skill>/scripts/poe.py' doctor --pob '<安装>'
python '<skill>/scripts/poe.py' start --invoked --input '<原件.xml>' --run '<授权工作区>/run' --pob '<安装>'
python '<skill>/scripts/poe.py' intent --run '<run>' --file '<工作区>/intent.json'
python '<skill>/scripts/poe.py' evaluate --run '<run>' --name candidate-a --plan '<工作区>/plan.json'
python '<skill>/scripts/poe.py' export --run '<run>' --name candidate-a
python '<skill>/scripts/poe.py' route --invoked --file '<工作区>/route.json' --out '<工作区>/路线.html' --pob '<安装>'
```

start 的新 run 须为空，保留原件。只读查询直接 inspect/probe，具体见[资料](data.md)。路径缺失时沿 doctor 诊断定位；不写原生安装或扩展用户工作区授权。

## 增量保存方案

完整源 JSON 可一次保存；含 claims 时，核对正文与当前证据后用 edit 的 review_claims 登记，再 evaluate/route。直接计算未登记的 JSON 只得到缺绑定的草案。edit 的 set 可一次提交完整方案并同时 review_claims，不必拆成小事务。字段归属见 input_contract，intent 不能搬进 plan。

```powershell
python '<entry>' edit --invoked --kind plan --file '<工作区>/plan.json' --changes '<工作区>/changes.json'
```

新文件直接创建；修订已有源在命令加 `--if-sha256 '<当前revision>'`。变更格式及合法字段由返回契约提供：

| 变更 | 用法 |
|---|---|
| set | 顶层字段按契约合并；数组对象用稳定 ID，未点名项保留 |
| replace | JSON Pointer 到新值的映射，替换指定位置 |
| remove | JSON Pointer 数组，删除指定位置 |
| review_claims | 已读当前证据、重新判断后的 claim_id 数组；刷新绑定 |

同一 edit 的指针按同一源 revision 解释。stage_id、claim_id、宝石 instance_id 保持对象身份；新实物使用新实例。更新对象或来源后读 invalidated_claims/current facts，改正文、条件与关系，再 review_claims。assessment.review 在方案/claims 修订与当前计算完成后单独提交，接着重算和导出；token 由程序返回。

## 按需读取与修复

将 `--out` 存在声明源 JSON 的目录或子目录，避开程序管理的候选/交付文件。先读 `delivery_brief/errors/stage_summary/next_actions`，按目标读取相关明细，不必每次倾倒全部目录或矩阵。

```powershell
python '<entry>' view --file '<工作区>/evaluation.json' --pointer '/result/stats' --keys
python '<entry>' view --file '<工作区>/review.json' --pointer '/facts/pending'
```

`view` 的 fields/keys/query、offset/limit 按返回 `page_unit` 分页；续页及过长字段在 continuations/deferred_values。复制当次 `native_references/evidence_template`，核对对象身份和 source_revision。零、false、null 分开。

诊断给 location、expected/actual、source_diagnostic 与可选合法引用。先读真实节点再修源；revision 过期重读输入。`io_source_commit` 等保存中断沿状态收据核对当前源与 revision。只在输入、证据或能力有新信息时重试；能力缺口如实保留，拒绝的动作停止。

## 采用原生候选

补路或宝石延期建议的 preview 已对组合实算。先看实际点数、资源、数值和用户限制，再将 `review_native_proposal` 的 details_file、proposal_id、input_revision 分别传给现有 edit 的 `--proposal-file`、`--proposal-id`、`--if-sha256`（仍带 `--invoked`）；路线指定 route kind。无需抄路径或生成另一份 XML。已知超点先重设计，可用点数未知则保留进度条件。保存后 evaluate/route 重核全部目标，建议不保证最优、实物可得或效果覆盖。

## 恢复与交付完整性

`status --run '<run>'` 或 `status --route-state '<路线.state.json>'` 查看当前源、候选和 last_operation，沿返回动作恢复。路线仅复用身份/文件一致的已完成阶段；失败后重跑原 route 命令。重算会取消旧导出身份，不能拿旧成功状态覆盖新失败。

只有 task_completion 为 scoped_result 且 errors/pending 均空，file_exported 才为 true 并列出最终 files。未通过时 files 为空；内部诊断与阶段快照仍保存以便返修。export 未通过返回 ok:false、退出码2；route 的 ok 只表示计算命令完成，交付另看 file_exported。原件、输入、候选、来源与引擎受完整性检查；被改输出应修源再生成，不手改生成页。
