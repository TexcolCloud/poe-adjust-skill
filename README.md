# POE 配装助手（TeleAgent）

面向 Windows TeleAgent 的 PoE1 国服配装 skill。用户明确调用后，多轮聊清目标，复用本机 PoECharm 的数据和计算，再联网核实需要的外部事实，交付原生生成的角色 XML 与中文 HTML 说明。

**当前为 2.8.16 实验版。** 316 项离线检查及本机安装自检通过；本轮物化 78/100、续航 74/100、独立目标复核 80/100，三项核心均未放行。数值复算成功不等于配装建议可靠，当前结果需要人工审核。[完整验证范围与已知缺口](TEST-REPORT.md)是状态依据。

## 使用

从 [2.8.16 实验版发布页](https://github.com/TexcolCloud/poe-adjust-skill/releases/tag/v2.8.16) 下载 `poe-adjust-2.8.16.zip`。私有仓库需要有访问权限的 GitHub 账号；上传后的包已下载核对，与本机冻结包一致。在 TeleAgent 的“技能·专家·应用连接 → 我的 → 导入技能”中选择 ZIP，不需要另装 Python 或维护游戏数据表。

准备能在 PoECharm 打开的 XML，作为 TeleAgent 附件，并发送：

> 调用 POE 配装助手（poe-adjust）。请先和我聊清目标，再用本机 PoECharm 计算，给我 XML 和中文说明。我的目标是：刷图少死一些、操作简单点。

[使用说明](使用说明.md)面向使用者；无需运行下面的开发命令。已有角色优化、整体换流派、已有交付复核与无 XML 开荒分别有协议；本轮未重新验证开荒和整体换流派的完成质量。

## 实现与边界

确定性接口规则由程序校验，配装取舍由模型决定，结论对应本次具体证据。合法值、效果描述和版本位置读取本机当前数据；检测内容和适配器指纹，更新后重新计算，无赛季白名单。

模型提交结构化操作，XML 经 PoB 的 `Build.SaveDB` 保存并冷回读。本机资料查询只读；输出写入获准任务工作区，保留原 XML。单次支付、有限时间线、循环支付、效果覆盖与目标达成分别报告，未知不当零，自审不认证自由文字。

| 位置 | 用途 |
|---|---|
| [poe-adjust/SKILL.md](poe-adjust/SKILL.md) | TeleAgent 加载入口与任务分流 |
| [poe-adjust/references](poe-adjust/references/commands.md) | 五份按需协议，命令以当前实现为准 |
| `poe-adjust/scripts/` | 六个 Python 模块、两个 Lua 适配文件，单一 `poe.py` CLI |
| [tests/test_workflow.py](tests/test_workflow.py) | 接口与原生行为回归，不是配装答案评测集 |
| [package.py](package.py) | 只打包 15 个交付文件 |
| [DESIGN.md](DESIGN.md) | 当前职责、证据与恢复边界 |
| [WORKLOG.md](WORKLOG.md) | 本轮交接与历史检索 |

只使用 Python 标准库；运行环境需要已安装的 Windows TeleAgent 与 PoECharm。仓库不包含这两个软件、游戏数据和私人角色文件。

## 维护

```powershell
python -B poe-adjust/scripts/poe.py --help
python -B -m unittest discover -s tests -v
python -B package.py
```

完整原生测试依赖本轮约定的盾牌角色样本；跨机直接运行可能失败或跳过，具体输入要求见[测试报告](TEST-REPORT.md#如何复现程序检查)。不能将跳过说成通过。打包文件生成于 `dist/`，本机已冻结包的 SHA256 为 `c35fe88bb63b1422e8ff58305089cb9fd422d483944ea45af1849b983ffe6518`；重新打包的 ZIP 容器时间可能变化，应同时核对内部文件。

本轮已结束，持续迭代自动化已暂停；新增开发需用户重新提出。交付源、安装和包保持 2.8.16，后续问题保留为明确限制。

## 历史与许可

本机 `.work/` 保留自然任务首份、评分、恢复与原生复算；不上传原角色或冻结实测数据。此前长篇设计和测试过程在 Git 历史 `1ce832d` 可查，其中本地证据链接仅在原机器有效。仓库提供当前摘要，不声称可从摘要独立重现 TeleAgent 当时行为。

`headless.lua` 包含 PoB 衍生内容，许可见 [第三方声明](poe-adjust/THIRD-PARTY-NOTICES.txt)。未为本工程其他内容新增开源许可证。
