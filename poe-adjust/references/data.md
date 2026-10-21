# 当前资料与事实查询

## 只读查询与完成

读取名称、状态、规格、费用、频率、原生定义、来源范围及独立算术用inspect/probe，--out保存JSON、--report直接交事实页，无需intent/plan。独立支付、固定收支和平均耗尽投影保留其假设与未知。判断配置能否支持用法、解释原因或推断来源是否齐全，转[只读解释](build.md#只读解释)，将判断及反证写入当前claims/review并交程序生成的说明。

```powershell
python '<entry>' inspect --input '<原件.xml>' --calculate --out '<工作区>/facts.json' --report '<工作区>/事实.html'
python '<entry>' inspect --input '<原件.xml>' --metrics 'Life,ManaCost' --skill-ref '<当前引用>' --actor player --out '<工作区>/detail.json'
```

## 按需目录

catalog种类取当前--help。独立查询可--batch提交queries的kind/query/limit/offset，--out保存再view；先小查，沿真实ID分页。节点用--input或class_id取职业起点，升华连接计点；links核技能类型关系，物理孔连/属性/取得另核。bases.valid_slots属当次布局，缺上下文留未知。

## 技能、配置与明细

保存level/quality与effective_specifications分开。active_skills沿skill_ref/actor读cost/rate/native_timing；组设置、授予效果和实际辅助均属于对象。

config_controls读value/default/value_basis、Input/Placeholder与runtime/applied，选当前options[].val并核visibility/apply_source及resolved值。null非false，删覆盖可能恢复默认，一个控件未生效不代表全部关闭。

`inspect --metrics`自动计算，breakdowns给value/detail/context、available_metrics及MAIN对照。解释因果先核输入、中间因子、上限、输出和已计入的因素，沿来源读适用原文；需要时用probe检验单一变量。派生值、主体、场景或上下文不同就分开，缺字段留未知。排他结论核相关实际对象与反例，有限实验不证明机制不存在。

flask_base是基础，flask_local是单件处理后参数，二者不重复叠加。角色修正、当前充能和实际覆盖另核；开启快照不证明持续。合法性据当前适用规则，计算接受与模型旧常识都不单独认证。

## 读原文和引用

view分页见[命令](commands.md#按需读取与修复)；引用核身份/source_revision，逻辑根和索引限本次评估。

data的list/search/read定位公开只读source，沿实际路径/行号读上下文；local_data复制source/line/count/content_sha256模板，程序重读，含义由你核对。平台内部目录、私有过程和无关资料不读。

## 联网取得与版本

research --gems给当前ID的逐级要求与网页choices。沿同一行核职业、任务/前提、NPC、奖励选择数量及费用；Quest Reward只是渠道标签，多奖励不等于全能领。前期无奖励不推出后期无商店，小号/交易按用户许可，模板在choices[].evidence_templates。

research --trials位置/区域等级不证明已通关或可用点。doctor的数据/树版本只证明本机资料，research --version读真实官方正文；旧树、目标国服/赛季另核适用。其它网页保存URL、正文与查阅时间到工作区，沿[证据](evidence.md)引用；本机规则、公开规则和未知国服差异分别说明。
