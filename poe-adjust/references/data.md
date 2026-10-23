# 当前资料与事实查询

## 只读查询与完成

摘录本次原生对象、定义和独立算术用inspect/probe，--out保存JSON、--report交事实页，无需intent/plan。支付与平均投影保留其假设和未知。要在此基础上下机制、用法或资源支撑结论，转[只读解释](build.md#只读解释)写入当前claims/review，由同一publication生成说明；新方案按[入口](../SKILL.md#入口和范围)选择任务类型。

```powershell
python '<entry>' inspect --input '<原件.xml>' --calculate --out '<工作区>/facts.json' --report '<工作区>/事实.html'
python '<entry>' inspect --input '<原件.xml>' --metrics 'Life,ManaCost' --skill-ref '<当前引用>' --actor player --out '<工作区>/detail.json'
```

## 按需目录

catalog种类取当前--help。独立查询可--batch提交queries的kind/query/limit/offset，--out保存再view；先小查，沿真实ID分页。节点用--input或class_id取职业起点，升华连接计点。links只测query中实际提供的主动/辅助skill_id配对，checked_pairs为本次实测数；候选从gems查询，空配对不表示没有可用辅助。物理孔连/属性/取得另核。bases.valid_slots属当次布局，缺上下文留未知。

## 技能、配置与明细

保存level/quality与effective_specifications分开。catalog.gems的granted_effects列整颗宝石授予的效果，level_requirements_effect_id标明逐级数值对应哪个效果；本角色开关和实际技能取inspect.gems.granted_effects。逐级base_resources为原生编码值，base_resource_units.cost给本机ResourceString/Divisor；native_MORE_percent为百分比修正，native_raw为单位未知，均非完整角色费用。active_skills按skill_ref绑定费用、频率、native_timing及各自source/cost_scope，伤害actor不替换费用或时长的来源主体。

config_controls读value/default/value_basis、Input/Placeholder与runtime/applied，选当前options[].val并核visibility/apply_source及resolved值。null非false，删覆盖可能恢复默认，一个控件未生效不代表全部关闭。

`inspect --metrics`自动计算，breakdowns给value/detail/context及MAIN对照。value_status与detail_status分别说明数值、明细是否可用；旧status保留明细状态，缺明细不抹掉已观测值或零值。available_metrics从本次CALCS输出及明细键读取，可包含MAIN没有的合法字段；不从名字猜对应关系。解释因果先核输入、中间因子、上限、输出和已计因子，沿来源读适用原文；需要时用probe检验单一变量。主体、场景或上下文不同就分开，缺字段留未知。有限实验不证明排他规则。

flask_base是基础，flask_local是单件处理后参数，二者不重复叠加。角色修正、当前充能和实际覆盖另核；开启快照不证明持续。合法性据当前适用规则，计算接受与模型旧常识都不单独认证。

## 读原文和引用

view分页见[命令](commands.md#按需读取与修复)；引用核身份/source_revision，逻辑根和索引限本次评估。

data的list/search/read定位公开只读source，沿实际路径/行号读上下文；local_data复制source/line/count/content_sha256模板，程序重读，含义由你核对。平台内部目录、私有过程和无关资料不读。

## 联网取得与版本

research --gems给当前ID的逐级要求与网页choices。沿同一行核职业、任务/前提、NPC、奖励选择数量及费用；Quest Reward只是渠道标签，多奖励不等于全能领。前期无奖励不推出后期无商店，小号/交易按用户许可，模板在choices[].evidence_templates。

research --trials位置/区域等级不证明已通关或可用点。doctor的数据/树版本只证明本机资料，research --version读真实官方正文；旧树、目标国服/赛季另核适用。其它网页保存URL、正文与查阅时间到工作区，沿[证据](evidence.md)引用；本机规则、公开规则和未知国服差异分别说明。
