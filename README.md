# 公考资料分析教练（gongkao-data-analysis-coach）

一个面向公务员考试**行测·资料分析**模块的 AI Agent 技能（Agent Skill）。它把「花生十三 24 下半年资料系统班」10 份随堂笔记 PDF（共 204 页）提炼为结构化的知识图谱与确定性计算引擎，让 AI 助手在解题时做到：**速度优先、近似可证、错因可归、进度可信**。

## 核心能力

| 能力 | 说明 |
|---|---|
| 速度优先解题 | 默认目标约 45 秒出答案；按「定性秒杀 → 范围量级 → 特征数字 → 份数法 → 拆分假设 → 截位直除 → 精算」路由，命中即停 |
| 估算安全验证 | 每次近似都输出「估算安全卡」（假设、偏差方向、误差界、决策边界、失效条件），并由 CLI 独立复核真实误差 |
| 确定性验算 | 9 个公式恒等式由纯 Python 标准库复算，讲解与验证严格分离 |
| 知识图谱 | 50 个稳定节点（9 个结构根 + 41 个可评估节点），含父子、前置、来源页码与迁移链 |
| 专项练习 | 10 个确定性参数化模板，支持 easy/medium/hard 三档难度与固定 seed 复现，逐题独立复算验证 |
| 错题本与复习 | SQLite 事务化持久化；17 个根因编码；1/3/7/14/30 天间隔复习，防旧 review ID 重放 |
| 可信进度定位 | 正确率附 Wilson 90% 区间；样本不足明确标注；低追溯记录不进入进度统计 |
| 来源与勘误 | 证据优先级：数学恒等式 > 题目原始数据 > 讲义解析 > 讲义标答；已登记第六讲两处勘误 |

## 目录结构

```text
.
├── 【花生十三】…随堂笔记.pdf        # 10 份源讲义（不入 Git，只读）
├── .agents/skills/gongkao-data-analysis-coach/
│   ├── SKILL.md                     # 技能主工作流：模式路由、强制执行顺序、CLI 契约
│   ├── agents/openai.yaml           # Agent UI 元数据
│   ├── references/                  # 领域知识资产（一级引用深度）
│   │   ├── knowledge-graph.json     # 稳定知识节点与依赖
│   │   ├── knowledge-map.md         # 人类可读知识树
│   │   ├── formulas.md              # 公式、边界与公式引擎映射
│   │   ├── fast-methods.md          # 快算路由、偏差与失效条件
│   │   ├── question-types.md        # 题型信号与标准路径
│   │   ├── answer-protocol.md       # 解题/陪练/记录输出契约
│   │   ├── error-taxonomy.md        # 错因编码与判定
│   │   ├── progress-model.md        # 多维进度模型
│   │   ├── source-map.md            # 10 讲 204 页来源索引
│   │   ├── errata.md                # 已核验勘误
│   │   └── practice-templates.json  # 参数化专项题模板
│   └── scripts/
│       ├── coach.py                 # JSON CLI 入口
│       └── coachlib/                # 确定性核心（纯标准库）
│           ├── formulas.py          # 公式恒等式
│           ├── store.py             # SQLite 状态与复习
│           ├── progress.py          # Wilson 区间与掌握度分级
│           └── practice.py          # 可复现练习生成与复核
├── tests/gongkao_coach/             # 行为与确定性单元测试（unittest）
├── docs/superpowers/                # 设计文档、实施计划、基线与完成审计
└── .gongkao-study/                  # 运行时学习数据库（gitignored，首次授权后创建）
```

## 快速开始

环境要求：Python 3.11+（仅标准库，无第三方依赖）。

所有命令从工作区根目录执行，CLI 每次调用只输出一个 JSON 文档：

```bash
COACH=.agents/skills/gongkao-data-analysis-coach/scripts/coach.py

# 无状态：公式复核 / 估算验证 / 出题（不触碰数据库）
python3 $COACH calculate --formula growth_amount --json-file - <<<'{"current": 72414, "rate": 0.058, "rate_unit": "decimal"}'
python3 $COACH check-estimate --json-file - <<<'{"estimate": 3970, "absolute_error_bound": 5, "exact_value": 3969.8, "options": {"A": 3969.8, "B": 4200.5}, "bias": "high", "error_bound_derivation": "截位误差 < 5"}'
python3 $COACH practice-catalog
python3 $COACH generate --knowledge-id abrx.base --count 3 --difficulty medium --seed 42

# 写入（需用户明确授权）
python3 $COACH init
python3 $COACH record --json-file - <<<'{"question": "…", "user_answer": "B", "correct_answer": "C", "is_correct": false, "knowledge_ids": ["growth.ratio"]}'

# 只读
python3 $COACH mistakes --knowledge-id growth.ratio
python3 $COACH due
python3 $COACH progress
python3 $COACH export --output backup.json
```

## 运行测试与校验

```bash
# 全量单元测试
python3 -m unittest discover -s tests/gongkao_coach -p 'test_*.py' -v

# 语法编译检查
python3 -m compileall -q .agents/skills/gongkao-data-analysis-coach/scripts
```

## 设计原则

- **两层架构**：语言模型负责题意与图像理解；脚本负责一切可确定计算与状态变更，通过 JSON 回执连接。
- **未授权不写入**：首次持久化必须获得用户明确同意；无状态命令永远不创建数据库。
- **真实回执才可声称**：只有看到 `ok: true` 的 JSON 成功回执才能说「已记录」。
- **证据优先级**：数学恒等式 > 题目原始数据 > 讲义解析 > 讲义标答；已确认勘误优先于原标答。
- **安全文件操作**：数据库经原子发布、硬链接复核与 sidecar 校验保护；路径禁止逃逸工作区。

## 文档索引

- 技能运行契约：`.agents/skills/gongkao-data-analysis-coach/SKILL.md`
- 完成度审计：`docs/superpowers/evidence/2026-08-30-completion-audit.md`
- 设计文档：`docs/superpowers/specs/2026-08-30-data-analysis-coach-design.md`
- 实施计划（历史记录）：`docs/superpowers/plans/2026-08-30-gongkao-data-analysis-coach.md`
- 面向 AI 编码代理的协作指南：`AGENTS.md`
