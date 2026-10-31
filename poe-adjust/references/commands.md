# 命令、保存与恢复

单入口 `scripts/poe.py`；参数取当前 `--help`，字段取 `input_contract`。首次定位用 `doctor`，沿返回的 Python/entrypoint/pob_path。原件与本机安装只读，所有新文件、缓存和临时目录落本任务获准目录。

## 增量保存方案

你编写独立 changes JSON，`edit --invoked --kind plan|route --file ... --changes ...` 创建或修改源文件；已有源带 `--if-sha256 '<当前revision>'`。返修直接取 next_actions.edit_arguments，输入变更后重算，最终复核单独提交。

| changes | 用途 |
|---|---|
| set | 合并顶层；数组对象按稳定ID更新，未点名项保留 |
| replace | JSON Pointer到新值，精确替换字段/数组 |
| remove | 删除明确JSON指针；同次指针按同一源revision解释 |
| review_claims | 核过当前正文与依据后，重登所列claim_id的程序绑定 |

新增或改文由edit绑定。依赖变化先计算当前对象，再修正文并重登invalidated_claims；绑定不更新文字。intent字段在intent，plan/route字段沿各自契约。

新方案先写 `{"set":{"mode":"refine","summary":"本次目的","operations":{}}}` 到 plan-changes.json，按需补实际方案字段，由首次 edit 创建 plan.json。沿以下顺序工作；review-changes仅写set.assessment.review，按当前review_template审原目标、对象和条件：

```powershell
python '<entry>' start --invoked --input '<原件.xml>' --run '<run>' --pob '<安装>'
python '<entry>' intent --run '<run>' --file '<工作区>/intent.json'
python '<entry>' edit --invoked --kind plan --file '<工作区>/plan.json' --changes '<工作区>/plan-changes.json'
python '<entry>' evaluate --run '<run>' --name candidate-a --plan '<工作区>/plan.json'
python '<entry>' edit --invoked --kind plan --file '<工作区>/plan.json' --changes '<工作区>/review-changes.json' --review-from '<本次details_file>'
python '<entry>' evaluate --run '<run>' --name candidate-a --plan '<工作区>/plan.json'
python '<entry>' export --run '<run>' --name candidate-a
```

路线两次edit用kind route；计算/发布均为 `route --invoked --file '<工作区>/route.json' --out '<工作区>/路线.html' --pob '<安装>'`。首次与重审整体替换复核；沿review_current_result.edit_arguments核源和凭证。

## 按需读取与修复

`--out`选声明源目录/子目录，避开程序管理的候选和交付文件。先读delivery_brief、errors、stage_summary、next_actions；`view`沿pointer/fields/keys/query定位，按page_unit、continuations/deferred_values续读，无须重算来翻页。purpose指出分页动作，changes/edit_arguments可按当前正文复用。

location、expected/actual、source_diagnostic指向实际问题；先读源节点，证据沿source_base和真实路径。revision过期重读；有新输入、证据或能力才重试，缺能力保留未知。

## 采用原生候选

补路/延期的preview已对组合实算。先核点数、资源、目标与限制，再以review_native_proposal的details_file、proposal_id、input_revision调用edit的 `--proposal-file/--proposal-id/--if-sha256`，仍带invoked和对应kind。采用后重算核目标。自然可用等级不证明已训练，未知进度仍为条件。

## 恢复与交付完整性

`status --run '<run>'` 或 `status --route-state '<路线.state.json>'`核源、候选与last_operation；大返回加新`--out`再view。已有run的start沿status恢复；路线仅复用身份/文件完整的已完成阶段。

交付门槛见[正式交付](../SKILL.md#工作与完成)。失败files为空，JSON/阶段快照供恢复；export失败返回ok:false/退出2，route.ok只说明命令完成。变更后修源重新生成，交付只取本次成功files。
