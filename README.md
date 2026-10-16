# POE 配装助手

Windows 上的 PoE1 配装 skill。用户明确调用后，一个 agent 复用本机 PoECharm 的数据和计算、联网核实外部事实，完成查询、复核、调整或开荒规划，交付中文 HTML 和适用 XML。

**交付目标为 TeleAgent 极速、单 agent；目前尚未放行。** 已发布2.8.17普通单agent成绩仅作历史参考。上一轮2.8.26十项实机5/10通过，仅查询与局部调整各连续两次；当前安装2.8.27减少重复关联与登记，368项完整回归、源/ZIP/安装一致性及doctor通过，新一轮实际任务进行中。仍为15文件、六Python/两Lua、schema2；原件、原分和旧发布保留，heartbeat未恢复。[验证报告](TEST-REPORT.md)是状态依据。

## 使用

从[2.8.17精简实验版发布页](https://github.com/TexcolCloud/poe-adjust-skill/releases/tag/v2.8.17)取得原样包 `poe-adjust-2.8.17-lean003.zip`；验收范围见[验证报告](TEST-REPORT.md)。仓库保持私有，需要有访问权限的GitHub账号。该发布版通过普通单agent离线样本验收；TeleAgent极速当前尚未通过。旧[2.8.16发布](https://github.com/TexcolCloud/poe-adjust-skill/releases/tag/v2.8.16)保留。在TeleAgent的“技能·专家·应用连接 → 我的 → 导入技能”中选择ZIP的步骤已实际使用。

准备能在 PoECharm 打开的 XML，作为 TeleAgent 附件，并发送：

> 调用 POE 配装助手（poe-adjust）。请先和我聊清目标，再用本机 PoECharm 计算，给我 XML 和中文说明。我的目标是：刷图少死一些、操作简单点。

[使用说明](使用说明.md)面向使用者；无需运行下面的开发命令。已有角色优化、整体换流派、已有交付复核与无 XML 开荒分别有协议。极速首轮的原件、原分与补充证据保留；新候选须重新完整验收，不拼前轮成绩。

## 实现与边界

确定性接口规则由程序校验，配装取舍由模型决定，结论对应本次具体证据。合法值、效果描述和版本位置读取本机当前数据；检测内容和适配器指纹，更新后重新计算，无赛季白名单。

模型提交结构化操作，XML 经 PoB 的 `Build.SaveDB` 保存并冷回读。本机资料查询只读；输出写入获准任务工作区，保留原 XML。单次支付、有限时间线、循环支付、效果覆盖与目标达成分别报告，未知不当零，自审不认证自由文字。

| 位置 | 用途 |
|---|---|
| [poe-adjust/SKILL.md](poe-adjust/SKILL.md) | 单执行者入口与任务分流 |
| [poe-adjust/references](poe-adjust/references/commands.md) | 五份按需协议，命令以当前实现为准 |
| `poe-adjust/scripts/` | 六个 Python 模块、两个 Lua 适配文件，单一 `poe.py` CLI |
| [tests/test_workflow.py](tests/test_workflow.py) | 接口与原生行为回归，不是配装答案评测集 |
| [package.py](package.py) | 只打包 15 个交付文件 |
| [DESIGN.md](DESIGN.md) | 当前职责、证据与恢复边界 |
| [WORKLOG.md](WORKLOG.md) | 本轮交接与历史检索 |

只使用 Python 标准库；本轮单执行者验证需要Windows、可用Python及PoECharm。仓库不包含原生软件、游戏数据和私人角色文件；下列TeleAgent安装说明对应既有发布路径。

## 维护

```powershell
python -B poe-adjust/scripts/poe.py --help
python -B -m unittest discover -s tests -v
python -B package.py
```

完整原生测试依赖约定的盾牌角色样本；跨机直接运行可能失败或跳过，具体输入要求见[测试报告](TEST-REPORT.md#如何复现程序检查)。不能将跳过说成通过。打包文件生成于 `dist/`，各冻结包的大小、SHA256和验收范围见测试报告；重新打包的ZIP容器时间可能变化，应同时核对内部文件。

2026-10-13用户授权上传已验收版本并准备TeleAgent适配。原精简轮已停止，持续迭代自动化保持暂停；旧发布不覆盖。

## 历史与许可

本机 `.work/` 保留自然任务首份、评分、恢复与原生复算；不上传原角色或冻结实测数据。此前长篇设计和测试过程在 Git 历史 `1ce832d` 可查，其中本地证据链接仅在原机器有效。仓库提供当前摘要，不声称可从摘要独立重现 TeleAgent 当时行为。

`headless.lua` 包含 PoB 衍生内容，许可见 [第三方声明](poe-adjust/THIRD-PARTY-NOTICES.txt)。未为本工程其他内容新增开源许可证。
