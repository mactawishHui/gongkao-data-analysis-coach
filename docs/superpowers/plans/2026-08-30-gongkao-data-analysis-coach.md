# 公考资料分析教练 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在工作区内交付一个可被 Codex 发现的资料分析专项技能，具备速度优先解题、确定性验算、知识图谱、专项练习、真实错题持久化和可信进度定位。

**Architecture:** 以 `.agents/skills/gongkao-data-analysis-coach` 作为交互与领域知识层，以纯 Python 标准库作为确定性公式、练习、SQLite 和统计层。语言模型负责题意和图像理解；脚本负责所有可确定计算与状态变更，并通过 JSON 回执连接两层。

**Tech Stack:** Codex Agent Skills、Markdown/JSON、Python 3 标准库（sqlite3、argparse、statistics、random、json）、unittest、Git。

---

## 文件结构

```text
.agents/skills/gongkao-data-analysis-coach/
  SKILL.md                         # 模式选择、审题、快算、验算、落盘协议
  agents/openai.yaml              # Codex UI 元数据
  references/knowledge-graph.json # 稳定知识节点与依赖
  references/knowledge-map.md      # 人类可读知识树与学习顺序
  references/formulas.md           # 公式、边界与公式引擎映射
  references/fast-methods.md       # 快算路由、偏差与失效条件
  references/question-types.md     # 题型信号、标准路径与变式
  references/answer-protocol.md    # 解题/陪练/记录输出契约
  references/error-taxonomy.md     # 错因编码与判定
  references/progress-model.md     # 多维进度模型
  references/source-map.md         # 10 讲 204 页来源索引
  references/errata.md             # 已核验勘误
  references/practice-templates.json # 参数化专项题模板
  scripts/coach.py                 # JSON CLI 入口
  scripts/coachlib/formulas.py     # 确定性公式
  scripts/coachlib/store.py        # SQLite 状态与复习
  scripts/coachlib/progress.py     # Wilson 区间与定位
  scripts/coachlib/practice.py     # 可复现练习生成和复核
tests/gongkao_coach/               # 行为和确定性单元测试
.gongkao-study/                    # 运行时数据库（gitignored）
```

## Task 1: 初始化版本库与技能骨架

**Files:**
- Create: `.agents/skills/gongkao-data-analysis-coach/SKILL.md`
- Create: `.agents/skills/gongkao-data-analysis-coach/agents/openai.yaml`
- Create: `.agents/skills/gongkao-data-analysis-coach/references/`
- Create: `.agents/skills/gongkao-data-analysis-coach/scripts/`

- [ ] **Step 1: 提交已批准设计、基线证据和计划**

Run:

```bash
git init -b main
git add .gitignore docs/superpowers
git commit -m "docs: design data analysis coach"
```

Expected: `main` 包含设计、实施计划与三组无技能基线证据，PDF 和 `tmp/` 未被跟踪。

- [ ] **Step 2: 创建隔离工作树**

Run:

```bash
git check-ignore -q .worktrees
git worktree add .worktrees/gongkao-coach -b feature/gongkao-coach
```

Expected: `.worktrees/gongkao-coach` 位于独立分支且 `.worktrees` 被忽略。

- [ ] **Step 3: 用官方脚手架初始化技能**

Run from the worktree:

```bash
python3 /Users/mactawish/.codex/skills/.system/skill-creator/scripts/init_skill.py gongkao-data-analysis-coach \
  --path .agents/skills \
  --resources scripts,references \
  --interface display_name="公考资料分析教练" \
  --interface short_description="快速解题、错题复习与可信进度定位" \
  --interface default_prompt="使用 $gongkao-data-analysis-coach 解答或训练这道资料分析题，并记录我的学习进度。"
```

Expected: `SKILL.md`、`agents/openai.yaml`、`scripts/` 和 `references/` 均生成。

## Task 2: 以测试驱动确定性公式与统计

**Files:**
- Create: `.agents/skills/gongkao-data-analysis-coach/scripts/coachlib/__init__.py`
- Create: `.agents/skills/gongkao-data-analysis-coach/scripts/coachlib/formulas.py`
- Create: `.agents/skills/gongkao-data-analysis-coach/scripts/coachlib/progress.py`
- Test: `tests/gongkao_coach/test_formulas.py`
- Test: `tests/gongkao_coach/test_progress.py`

- [ ] **Step 1: 写公式失败测试**

测试必须调用期望 API：

```python
from coachlib.formulas import (
    base_period, growth_amount, interval_growth,
    ratio_growth, product_growth, current_share, base_share,
    share_change, contribution_rate,
)

def test_growth_amount_from_current_and_rate():
    assert round(growth_amount(72414, 0.058), 1) == 3970.1

def test_ratio_growth_preserves_small_positive_direction():
    assert ratio_growth(0.089, 0.087) > 0

def test_interval_growth_keeps_cross_term():
    assert interval_growth(0.10, 0.20) == 0.32
```

Run: `python3 -m unittest discover -s tests/gongkao_coach -p 'test_formulas.py' -v`

Expected: FAIL because `coachlib.formulas` does not exist.

- [ ] **Step 2: 实现最小公式 API**

函数签名和恒等式固定为：

```python
def base_period(current: float, rate: float) -> float: return current / (1 + rate)
def growth_amount(current: float, rate: float) -> float: return current * rate / (1 + rate)
def interval_growth(r1: float, r2: float) -> float: return r1 + r2 + r1 * r2
def ratio_growth(num_rate: float, den_rate: float) -> float: return (num_rate - den_rate) / (1 + den_rate)
def product_growth(r1: float, r2: float) -> float: return r1 + r2 + r1 * r2
def current_share(part: float, whole: float) -> float: return part / whole
def base_share(part: float, whole: float, part_rate: float, whole_rate: float) -> float:
    return (part / whole) * (1 + whole_rate) / (1 + part_rate)
def share_change(part: float, whole: float, part_rate: float, whole_rate: float) -> float:
    return (part / whole) * (part_rate - whole_rate) / (1 + part_rate)
def contribution_rate(part_delta: float, whole_delta: float) -> float: return part_delta / whole_delta
```

所有分母为零或增长率 `<= -1` 的非法场景抛出 `ValueError`。

- [ ] **Step 3: 写进度失败测试**

```python
from coachlib.progress import wilson_interval, classify_mastery

def test_two_perfect_answers_remain_insufficient():
    result = classify_mastery(total=2, correct=2, recent_accuracy=1.0,
                              method_low_rate=0.0, speed_met=True,
                              transfer_passed=False, review_passes=0)
    assert result == "样本不足"

def test_wilson_interval_is_not_fake_one_hundred_percent():
    low, high = wilson_interval(2, 2, z=1.6448536269514722)
    assert low < 0.6 and high == 1.0
```

Run: `python3 -m unittest discover -s tests/gongkao_coach -p 'test_progress.py' -v`

Expected: FAIL because `coachlib.progress` does not exist.

- [ ] **Step 4: 实现进度 API 并跑绿**

`wilson_interval(successes, total, z)` 返回二元组；`classify_mastery(...)` 严格执行设计文档的 0、1-4、5+、8+、15+ 阈值。空样本不得除零。

Run: `python3 -m unittest discover -s tests/gongkao_coach -p 'test_*.py' -v`

Expected: all formula/progress tests PASS.

## Task 3: 以测试驱动错题库、复习和 CLI

**Files:**
- Create: `.agents/skills/gongkao-data-analysis-coach/scripts/coachlib/store.py`
- Create: `.agents/skills/gongkao-data-analysis-coach/scripts/coach.py`
- Test: `tests/gongkao_coach/test_store.py`
- Test: `tests/gongkao_coach/test_cli.py`

- [ ] **Step 1: 写持久化失败测试**

```python
def test_attempt_round_trip(tmp_path):
    store = StudyStore(tmp_path / "study.sqlite3", {"growth.ratio"})
    receipt = store.record_attempt({
        "question": "亩产值与价格增速判断亩产趋势",
        "user_answer": "B", "correct_answer": "C", "is_correct": False,
        "duration_seconds": 85, "confidence": 4,
        "knowledge_ids": ["growth.ratio"],
        "errors": ["source_conflict"],
        "method_used": "照抄讲义", "recommended_method": "比较分子分母增速",
        "method_quality": "low", "source_ref": "第六讲 PDF p2 例21"
    })
    assert receipt["attempt_id"] > 0
    assert store.get_attempt(receipt["attempt_id"])["correct_answer"] == "C"
    assert len(store.due_reviews(on_date=receipt["next_review_date"])) == 1
```

Run: `python3 -m unittest discover -s tests/gongkao_coach -p 'test_store.py' -v`

Expected: FAIL because `StudyStore` is missing.

- [ ] **Step 2: 实现 SQLite schema 与事务写入**

使用外键、`PRAGMA journal_mode=WAL` 和显式事务创建 `schema_meta`、`attempts`、`attempt_tags`、`attempt_errors`、`reviews`、`sessions`。标签不在知识图谱集合中时抛出 `ValueError`；失败事务不得留下半条记录。

- [ ] **Step 3: 写 CLI 失败测试**

CLI 子命令固定为：

```text
init
record --json '<payload>'
attempt <id>
mistakes [--knowledge-id ID] [--limit N]
due [--date YYYY-MM-DD]
review --attempt-id N --result wrong|hard|good
progress [--knowledge-id ID]
export --output PATH
generate --knowledge-id ID --count N --seed N
```

测试以 subprocess 调用并断言 stdout 是单个 JSON 对象、错误写 stderr 且非零退出。

- [ ] **Step 4: 实现 CLI 并跑绿**

默认数据库路径从脚本位置解析到工作区 `.gongkao-study/study.sqlite3`，也接受全局 `--db` 覆盖。任何成功写入回执至少含 `ok`、`database` 和对象 ID。

Run: `python3 -m unittest discover -s tests/gongkao_coach -p 'test_*.py' -v`

Expected: store and CLI tests PASS.

## Task 4: 以测试驱动参数化专项练习

**Files:**
- Create: `.agents/skills/gongkao-data-analysis-coach/scripts/coachlib/practice.py`
- Create: `.agents/skills/gongkao-data-analysis-coach/references/practice-templates.json`
- Test: `tests/gongkao_coach/test_practice.py`

- [ ] **Step 1: 写生成器失败测试**

```python
def test_generation_is_reproducible_and_verified():
    first = generate_set("abrx.base", count=5, seed=42)
    second = generate_set("abrx.base", count=5, seed=42)
    assert first == second
    assert all(q["options"].count(q["correct_value"]) == 1 for q in first)
    assert all(verify_question(q)["ok"] for q in first)

def test_unknown_node_is_rejected():
    with pytest.raises(ValueError):
        generate_set("not.real", count=1, seed=1)
```

使用 `unittest.TestCase.assertRaises`，不引入 pytest 依赖。

Run: `python3 -m unittest discover -s tests/gongkao_coach -p 'test_practice.py' -v`

Expected: FAIL because generator is missing.

- [ ] **Step 2: 实现首批可验证模板**

至少覆盖 `abrx.base`、`abrx.growth-amount`、`growth.interval`、`growth.ratio`、`share.current`、`share.base`、`share.trend`、`average.single`、`average.annual-increase`、`special.contribution`。每题返回题干、A-D 数值、正确项、精确值、快算提示、公式 ID 和 seed。

- [ ] **Step 3: 增加错项安全与边距校验**

正确值只能对应一个选项；相邻选项距离必须大于模板声明的粗估误差，否则重采样。固定 seed 经过重采样仍可复现。

Run: `python3 -m unittest discover -s tests/gongkao_coach -p 'test_*.py' -v`

Expected: all tests PASS.

## Task 5: 建立知识资产并进行结构校验

**Files:**
- Create: `.agents/skills/gongkao-data-analysis-coach/references/knowledge-graph.json`
- Create: `.agents/skills/gongkao-data-analysis-coach/references/knowledge-map.md`
- Create: `.agents/skills/gongkao-data-analysis-coach/references/formulas.md`
- Create: `.agents/skills/gongkao-data-analysis-coach/references/fast-methods.md`
- Create: `.agents/skills/gongkao-data-analysis-coach/references/question-types.md`
- Create: `.agents/skills/gongkao-data-analysis-coach/references/error-taxonomy.md`
- Create: `.agents/skills/gongkao-data-analysis-coach/references/progress-model.md`
- Create: `.agents/skills/gongkao-data-analysis-coach/references/source-map.md`
- Create: `.agents/skills/gongkao-data-analysis-coach/references/errata.md`
- Test: `tests/gongkao_coach/test_knowledge_assets.py`

- [ ] **Step 1: 写知识资产失败测试**

测试断言：节点 ID 唯一；所有 parent/prerequisite 存在；九大顶层前缀齐全；每个节点有 source、signals、fast_methods；PDF 页码在各讲实际页数内；两个已知勘误都有结构化条目；练习模板引用的节点存在。

Run: `python3 -m unittest discover -s tests/gongkao_coach -p 'test_knowledge_assets.py' -v`

Expected: FAIL because JSON/reference assets are absent.

- [ ] **Step 2: 根据 10 讲逐页分析写入知识图谱**

每个节点结构固定为：

```json
{
  "id": "share.base",
  "title": "基期比重",
  "parent": "share",
  "prerequisites": ["share.current", "abrx.base"],
  "sources": [{"lesson": 5, "pages": [12, 13, 14]}],
  "signals": ["上年占比", "前期比重"],
  "formula_ids": ["base_share"],
  "fast_methods": ["先判断修正因子方向", "选项宽时估算"],
  "error_codes": ["wrong_period", "reversed_rates"]
}
```

- [ ] **Step 3: 写人类可读参考**

`fast-methods.md` 必须对每种方法记录适用信号、操作、偏差方向、失效条件；`question-types.md` 必须提供“识别→最快法→稳妥法→陷阱→迁移”；`source-map.md` 必须列出 10 份文件、页数和章节覆盖。

- [ ] **Step 4: 跑知识校验**

Run: `python3 -m unittest discover -s tests/gongkao_coach -p 'test_*.py' -v`

Expected: all knowledge and engine tests PASS.

## Task 6: 写技能主工作流并前向测试

**Files:**
- Modify: `.agents/skills/gongkao-data-analysis-coach/SKILL.md`
- Modify: `.agents/skills/gongkao-data-analysis-coach/agents/openai.yaml`
- Create: `.agents/skills/gongkao-data-analysis-coach/references/answer-protocol.md`
- Test: `tests/gongkao_coach/test_skill_contract.py`

- [ ] **Step 1: 写技能契约失败测试**

测试读取 `SKILL.md` 并要求存在：输入完整性闸门、速度优先路由、估算偏差、失效边界、公式验算、真实回执后才可声称记录、同类总结、知识位置、复习和进度样本不足规则；同时验证前置 YAML 仅有 name/description 且 description 以 `Use when` 开头。

- [ ] **Step 2: 写最小 SKILL.md**

主文件控制在 500 行内，只保留模式路由、强制执行顺序、脚本调用和参考文件加载条件。详细领域内容放入一级 references，不重复堆叠。

- [ ] **Step 3: 运行官方技能校验**

Run:

```bash
python3 /Users/mactawish/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/gongkao-data-analysis-coach
```

Expected: `Skill is valid!`

- [ ] **Step 4: 使用全新子代理进行三组 GREEN 前向测试**

分别测试：讲义错答+真实落盘、2/2 全对的谨慎定位、45 秒快算含偏差方向与失效边界。代理只拿技能路径和原始用户请求，不提供预期答案或本计划结论。

- [ ] **Step 5: 修正前向测试发现的漏洞并重测**

若代理声称记录但无 JSON 回执、跳过选项分析、只给完整精算、虚报掌握度或遗漏同类总结，则更新 SKILL.md 对应规则并重新运行同场景直至通过。

## Task 7: 集成验收与交付

**Files:**
- Create: `docs/superpowers/evidence/2026-08-30-completion-audit.md`

- [ ] **Step 1: 运行全量测试和静态检查**

Run:

```bash
python3 -m unittest discover -s tests/gongkao_coach -p 'test_*.py' -v
python3 -m compileall -q .agents/skills/gongkao-data-analysis-coach/scripts
python3 /Users/mactawish/.codex/skills/.system/skill-creator/scripts/quick_validate.py .agents/skills/gongkao-data-analysis-coach
```

Expected: 0 failures, compile exit 0, `Skill is valid!`.

- [ ] **Step 2: 运行端到端烟雾测试**

在临时数据库执行 `init → record → attempt → progress → due → review → export → generate`，逐条解析 JSON 并确认记录可回读、样本为 1 时状态为“样本不足”、生成题正确项唯一。

- [ ] **Step 3: 要求覆盖审计**

完成表格逐项映射：准确理解、详细思路、同类总结、举一反三、整体结构、错题本、题目归类、专项练习、准确进度、速度优先快算、来源和勘误。每项写明文件/命令证据，不以“测试通过”替代未覆盖的广泛要求。

- [ ] **Step 4: 独立最终代码与内容审查**

审查所有提交相对基线的 diff；Critical/Important 问题必须修复并重新验证。

- [ ] **Step 5: 提交实现**

Run:

```bash
git add .agents tests docs/superpowers/evidence
git commit -m "feat: add gongkao data analysis coach"
```

Expected: 工作树干净，功能提交包含技能、脚本、知识资产、测试和完成审计。

