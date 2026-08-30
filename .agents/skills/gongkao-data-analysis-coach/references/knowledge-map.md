# 知识地图与学习路径

本地图与 `knowledge-graph.json` v1 的 50 个稳定节点一致，其中九大根节点只组织结构、`assessable: false`，其余 41 个具体节点才进入作答标签、掌握度和覆盖率。树中的缩进表示父子归属，箭头表示必须先掌握的前置关系；做题时应定位到最小、最具体的节点，而不是只停在九大分支名称。

## 依赖树

```text
速算基础 speed
├─ speed.trailing-digit
├─ speed.high-order-sum
├─ speed.round-benchmark
├─ speed.segmented-subtraction
├─ speed.fraction
├─ speed.decomposition        ← speed.fraction
├─ speed.division
├─ speed.415                  ← speed.fraction + abrx.general
└─ speed.assumed-allocation   ← abrx.general

ABRX abrx                    ← speed
├─ abrx.general
├─ abrx.current               ← abrx.general
├─ abrx.base                  ← abrx.general
├─ abrx.rate                  ← abrx.general
└─ abrx.growth-amount         ← abrx.general

增长率 growth                ← abrx
├─ growth.basic               ← abrx.rate
├─ growth.interval            ← growth.basic
├─ growth.ratio               ← growth.basic
└─ growth.product             ← growth.basic

比重 share                   ← growth
├─ share.current              ← abrx.current
├─ share.base                 ← share.current + abrx.base
├─ share.multilevel           ← share.current
├─ share.trend                ← growth.ratio
├─ share.change               ← share.current + share.trend
└─ share.value-difference     ← growth.ratio

比较 compare                 ← share
├─ compare.fraction           ← speed.fraction + growth.basic
├─ compare.increment          ← abrx.growth-amount
├─ compare.increment-rate     ← compare.increment + growth.basic
├─ compare.catch-up           ← compare.increment
└─ compare.find-then-calculate ← compare.fraction

混合 mixture                 ← growth
├─ mixture.salt-water
├─ mixture.three-rates        ← mixture.salt-water
└─ mixture.cross              ← mixture.salt-water

平均 average                 ← growth
├─ average.single             ← growth.ratio
├─ average.multiple           ← average.single
├─ average.annual-increase    ← abrx.growth-amount
└─ average.annual-rate        ← growth.interval

特殊 special                 ← growth
├─ special.pull               ← abrx.growth-amount
├─ special.contribution       ← abrx.growth-amount
└─ special.inclusion-exclusion

陷阱 trap                    ← speed
├─ trap.time-scope
└─ trap.integrated            ← trap.time-scope
```

顶层主干是“速算基础 → ABRX → 增长率 → 比重 → 比较”。混合、平均、特殊从增长率体系分叉；陷阱在速算之后即可介入，并贯穿之后每一类题。父节点只表示分类与建议学习顺序，不是掌握门槛；只有具体可评估节点之间的 `prerequisites` 前置边，才需要作答证据来判断是否先补前置。九大不可评估根节点不可能也不需要被“做题通过”。

## 推荐学习顺序

1. **速算基础**：先学 `speed.trailing-digit`、`speed.high-order-sum`、`speed.round-benchmark`、`speed.segmented-subtraction`、`speed.fraction`、`speed.decomposition`、`speed.division`。目标不是盲目求快，而是能根据选项间距决定停止位置。
2. **ABRX 骨架**：学 `abrx.general`，再分别训练 `abrx.current`、`abrx.base`、`abrx.rate`、`abrx.growth-amount`。此后返回学习 `speed.415` 与 `speed.assumed-allocation`；这两法依赖 ABRX，不能只背步骤。
3. **增长率四型**：依次学 `growth.basic`、`growth.interval`，再并行学 `growth.ratio` 与 `growth.product`。每次先写关系式，避免把比值模型和乘积模型选反。
4. **比重主线**：从 `share.current` 开始，进入 `share.base`、`share.multilevel`；掌握 `growth.ratio` 后学习 `share.trend`、`share.change`、`share.value-difference`。
5. **比较强化**：先用 `compare.fraction` 训练只比不算，再学 `compare.increment` 与 `compare.increment-rate` 的一大一小边界判断；随后用 `compare.catch-up` 判断差距变化，用 `compare.find-then-calculate` 训练“先找后算、避免反复读题”。
6. **并行迁移分支**：混合按 `mixture.salt-water` → `mixture.three-rates` / `mixture.cross`；平均按 `average.single` → `average.multiple`，并补 `average.annual-increase`、`average.annual-rate`；特殊学习 `special.pull`、`special.contribution`、`special.inclusion-exclusion`。
7. **陷阱贯穿**：速算基础后立即加入 `trap.time-scope`，综合阶段再训练 `trap.integrated`。每次错题都先排除时间、对象、指标、单位和措辞错误，再归因到公式或计算。

## 已解题目的节点映射

一题只设一个**主节点**：直接决定题型、公式或定性结论的最具体可评估节点。可设多个**次节点**：实际用到的速算方法、前置知识和触发的审题陷阱。九大根节点不可作为持久化主/次标签。材料缺失到只能判断根分支，或无法可靠判分时，只在本轮讲解中报告 `parse_uncertain` / `missing_question` 和待补字段，**不调用 `record`**；补足到能确认唯一可评估主节点与真实对错后才可持久化，避免把临时诊断标签写成掌握证据。

示例：

- “已知现期量和增速求上年量”：主节点 `abrx.base`；若用份数，次节点 `speed.415`；若年份口径险些看错，再加 `trap.time-scope`。
- “上年比重比本年高还是低”：主节点 `share.trend`；次节点 `growth.ratio` 与 `trap.integrated`。
- “求比重提高多少个百分点”：主节点 `share.change`；次节点 `share.current`、`share.trend`，选项宽时可加 `speed.decomposition`，选项近则加 `speed.division`。
- “比较四项同比增量”：主节点 `compare.increment`；次节点 `abrx.growth-amount`。现期量和增速一大一小时，主节点改为 `compare.increment-rate`。
- “顺差、逆差或两对象差值扩大/缩小”：主节点 `compare.catch-up`；次节点 `compare.increment`，先比较两者带符号增量。
- “先找前三项再求其合计占比”：主节点 `compare.find-then-calculate`；次节点 `compare.fraction` 与 `share.current`，查找阶段和计算阶段分开记录。
- “两部分增速和整体增速求人数比”：主节点 `mixture.cross`；次节点 `mixture.salt-water` 与 `trap.time-scope`。
- “判断当月增速是否快于本月累计增速”：主节点 `mixture.three-rates`；把“上月累计、本月累计、当月”视为两部分与整体，只比较相邻两期累计增速；次节点 `mixture.salt-water` 与 `trap.time-scope`。
- “判断 7 月业务量是否超过上半年月均”：主节点 `mixture.three-rates`；只需比较 1—7 月累计月均与上半年累计月均，次节点 `average.single` 与 `trap.time-scope`。
- “十三五年均增加量”：主节点 `average.annual-increase`；次节点 `abrx.growth-amount` 和 `trap.time-scope`。
- “某产业对整体增量的贡献百分比”：主节点 `special.contribution`；次节点 `abrx.growth-amount` 与 `speed.fraction`。

映射完成后，答案应同时给出主节点、关键次节点和实际错误代码；“算错了”不能替代根因定位。

## 弱节点的补前置与迁移

1. 对弱节点沿前置箭头向上追溯，找到最靠前且仍不稳定的节点；先练该节点，而不是反复刷当前难题。
2. 前置稳定后回到原弱节点做同构题；通过后选择把它列为前置的后继节点做迁移题，检验能否在新问法中自主识别。
3. 若知识会但速度慢，回到同一题的速算次节点；若结果方向反复错，优先回查 `trap.integrated`，不要用更多计算掩盖审题问题。

具体迁移链：

- `share.change` 弱：依次检查 `share.current`、`share.trend`、`growth.ratio`、`growth.basic`；修复后用 `compare.fraction` 做跨题型比较迁移。
- `growth.basic` 弱：修复 `abrx.rate` 与 `abrx.general`；稳定后进入 `growth.interval`、`growth.ratio`、`growth.product`，最后用 `compare.increment-rate` 检验联合判断。
- `abrx.growth-amount` 弱：先回到 `abrx.general`，再用 `speed.415` 或 `speed.assumed-allocation` 提速；后继迁移依次选 `compare.increment`、`average.annual-increase`、`special.pull`、`special.contribution`。
- `share.current` 弱：先查部分/整体口径，再迁移到 `share.base`、`share.multilevel`、`share.change`。
- `mixture.salt-water` 弱：先只做居中定性，稳定后进入 `mixture.three-rates` 与 `mixture.cross`。

迁移成功的标准是：换材料、换措辞或换目标后仍能在计算前指出主节点、前置关系和失效边界，而不是记住上一题数字。
