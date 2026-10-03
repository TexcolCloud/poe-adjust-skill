# POE 配装助手交付记录

目标：为 TeleAgent 交付显式调用、多轮意图确认、联网检索、PoECharm/PoB 实算、局部优化与整体换流派、XML 与中文报告导出的单一技能。完成离线测试后安装本机 TeleAgent，以 Computer Use 进行真实多轮对话并修正问题。

约束：用户无需维护依赖；保留原角色；不更改 PoECharm 安装文件；不虚构计算、价格或测试结果；执行环境是 TeleAgent Windows。

已核实：PoECharm 3.6.0，PoB 2.67.2；TeleAgent 2.7.0-rc.1 含 Python 3.12、PowerShell、SKILL.md 及脚本调用机制。原样本位于本机 PoECharm Builds/夷陵盾牌S29 原始配装.xml。项目最初为空，已初始化 Git。

计划：
1. 封装只读调用本机 Lua 计算引擎与 XML 解析/修改/回读。
2. 完成会话、约束、候选设计、数据查询、比较与导出命令及 TeleAgent 技能说明。
3. 离线单元/真实引擎集成测试，覆盖局部修改和完整换流派，记录限制。
4. 安装到 TeleAgent 测试环境，经真实 UI 多轮对话验证、修正并回归。
5. 交付技能包、使用说明、验证记录，清理临时实现。

状态：实现中。原生 Windows Computer Use 已初始化，可发现 TeleAgent 窗口。
