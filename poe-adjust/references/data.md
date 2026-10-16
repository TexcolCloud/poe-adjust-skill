# 当前资料与事实查询

## 只读查询与完成

名称、状态、规格、费用、频率及已算数值的含义用inspect/probe，--out保存JSON、--report交中文事实页。独立单次支付、固定收支与平均耗尽投影也属此路径，无需intent/plan。保留禁用状态，按实际返回范围解释；另证因果或裁决作者时转[配装协议](build.md)。

```powershell
python '<entry>' inspect --input '<原件.xml>' --calculate --out '<工作区>/facts.json' --report '<工作区>/事实.html'
python '<entry>' inspect --input '<原件.xml>' --metrics 'Life,ManaCost' --skill-ref '<当前引用>' --actor player --out '<工作区>/detail.json'
```

## 按需目录

catalog查当前classes/rules/gems/skills/links/bases/affixes/nodes/config/metrics/uniques/translations。已知的相关独立查询可用--batch一次提交queries，各项kind/query/limit/offset；结果保存--out后用view定向读取。未知先小查再沿真实ID/分页扩查，不从记忆猜ID或逐项倾倒全目录。

节点用--input或class_id提供职业起点，升华连接也计点；links检查技能类型关系，物理孔连/属性/取得另核。bases.valid_slots属于当次布局，缺上下文留未知。doctor的data_version/树版本只是本机资料，旧角色树、目标国服/赛季另说明适用范围。

## 技能、配置与明细

以当前物化对象为准：保存level/quality与effective_specifications分开；active_skills的skill_ref/actor、cost/rate、native_timing限定技能、主体、频率、时长及冷却。原组设置、授予效果和实际辅助也属于对象，不能只数可见普通宝石后称全部已排除。

config_controls区分value/default/value_basis及Input/Placeholder、runtime/applied。null不作false；删除覆盖可能恢复默认。选择当前options[].val并读visibility/apply_source原文，再核实际resolved值；一个控件未生效不推出所有开关关闭。

inspect --metrics自动计算，breakdowns给value/detail/context、available_metrics与MAIN对照。解释派生值先读实际明细：输入、中间因子、上限与输出分别是什么，哪些因素已经计入；涉及机制时沿来源读原文。公式/主体/场景不同就分开，缺字段留未知。排他判断核相关真实对象与反例；有限probe不能证明机制不存在。

flask_base为基础、flask_local为单件处理后参数，不重复叠加。角色修正、当前充能、补充与覆盖另核；granted_effects、药剂开启或瞬时数值不证明持续。游戏合法性用当前规则依据，计算接受不单独认证，旧常识也不覆盖本次来源。

## 读原文和引用

view按pointer/fields/keys/query及offset/limit读JSON，续页沿page_unit、continuations/deferred_values。复制当前native_references/evidence_template，核身份与source_revision；逻辑根/索引不跨评估搬用，零、false、null分开。

data用list/search/read定位公开只读source，再按真实路径/行号读完整上下文；截断片段不充当完整机制。local_data使用source/line/count/content_sha256模板，由程序重读，引用真实含义仍由你判断。不读平台内部目录、私有过程或无关资料。

## 联网取得与版本

research --gems '<当前ID列表>'给原生逐级要求与网页choices。取得核宝石/职业/任务/前提/真正NPC/奖励选择和费用，字段取同一行；Quest Reward只是渠道标签，同任务多奖励不等于全可领取。前期无某职业奖励不推出后期商店也无，按用户许可核小号/交易，模板在choices[].evidence_templates。

research --trials的位置/区域等级不证明通关或可用点数；research --version读真实官方正文，不由赛季编号猜规则。其他网页保存URL、实际正文与查阅时间到工作区，沿[证据](evidence.md)引用。本机数据、公开规则、用户声明与模型推断分别说明；未知国服差异保留条件。
