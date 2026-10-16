# 调用与恢复（schema_version 2）

## 命令入口

参数用当前--help，JSON归属用input_contract；本文件保留易误用的调用语义。

PowerShell路径分别加引号，JSON用UTF-8序列化或here-string保存，不拼复杂命令行JSON。替换下面占位为本次绝对路径：

```powershell
python '<entry>' doctor --pob '<安装>'
python '<entry>' start --invoked --input '<原件.xml>' --run '<工作区>/run' --pob '<安装>'
python '<entry>' intent --run '<run>' --file '<工作区>/intent.json'
python '<entry>' evaluate --run '<run>' --name candidate-a --plan '<工作区>/plan.json'
python '<entry>' export --run '<run>' --name candidate-a
python '<entry>' route --invoked --file '<工作区>/route.json' --out '<工作区>/路线.html' --pob '<安装>'
```

start新run为空并保留原件；只读直接inspect/probe。路径缺失沿doctor诊断定位，不扩用户工作区或写原生安装。

## 增量保存方案

edit.set可一次保存完整方案/claims，新增或改文自动写当前绑定；依赖改变使旧判断失效时，核正文后用review_claims重登。直接计算未登记JSON会得到缺绑定草案。intent字段留intent，已有源修订加--if-sha256 '<当前revision>'。

```powershell
python '<entry>' edit --invoked --kind plan --file '<工作区>/plan.json' --changes '<工作区>/changes.json'
```

| changes字段 | 语义 |
|---|---|
| set | 顶层字段按契约合并，数组对象按稳定ID更新，未点名项保留 |
| replace | JSON Pointer到新值的映射，精确替换字段/数组 |
| remove | JSON指针数组，删除明确字段 |
| review_claims | 已核当前文字与证据的claim_id数组，刷新程序绑定 |

同次edit指针按同一源revision解释，stage_id/claim_id/instance_id保持真实身份。变更后读invalidated_claims，修文字和条件再登记。

对象稳定后，changes只写set.assessment.review，按本次review_template填写除token外的判断/依据/条件。沿next_actions.review_current_result的edit_arguments提交：

```powershell
python '<entry>' edit --invoked --kind plan --file '<工作区>/plan.json' --changes '<工作区>/review-changes.json' --review-from '<本次details_file>'
```

路线用kind route。程序核计算凭证、源revision与引擎并关联token，省去手抄标识；只保存模型写出的复核，不决定通过。保存后重算再export/route。

## 按需读取与修复

--out放声明源JSON目录/子目录，避开程序管理的候选/交付文件。先读delivery_brief/errors/stage_summary/next_actions，再用view读相关pointer/fields/keys/query及分页；page_unit、continuations/deferred_values决定续读。复制当次native_references/evidence_template，核source_revision与身份。

诊断的location、expected/actual、source_diagnostic给实际修复入口，先看真实节点再改源；revision过期先重读，保存中断按状态收据恢复。输入/证据/能力有新信息才重试，能力缺口如实保留，拒绝操作停止。

## 采用原生候选

补路/延期preview已对组合实算。先审实际点数、资源、数值和限制，再将review_native_proposal的details_file、proposal_id、input_revision传给edit的--proposal-file、--proposal-id、--if-sha256，仍带--invoked，路线指定route kind。采用后evaluate/route重核目标，无需抄路径或另生XML。自然可用等级不证明已训练/取得，可用点未知仍留进度条件。

## 恢复与交付完整性

status --run '<run>'或status --route-state '<路线.state.json>'读当前源、候选、last_operation并沿返回动作恢复。路线仅复用身份/文件完整的已完成阶段，失败重跑同一route；重算取消旧导出身份。

只有task_completion.status为scoped_result、errors/pending为空，file_exported才true且列最终files。失败files为空，内部JSON/阶段快照留恢复；export返回ok:false/退出2，route.ok只表示计算命令完成，交付另看file_exported。输出受完整性核对，改动后修源重新生成，不手改页面或用旧成功凭证覆盖新失败。
