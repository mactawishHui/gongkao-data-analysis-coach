# AGENTS.md — 面向 AI 编码代理的协作指南

本文件为在本仓库中工作的 AI 编码代理（Codex、WorkBuddy 等）提供项目约定。**运行契约的唯一权威来源是 `.agents/skills/gongkao-data-analysis-coach/SKILL.md`**；本文件只描述工程协作规则，与 SKILL.md 冲突时以 SKILL.md 为准。

## 项目概述

「公考资料分析教练」：把 10 份花生十三资料分析随堂笔记 PDF 转化为一个 Agent Skill，提供速度优先解题、确定性验算、知识图谱、专项练习、错题持久化与可信进度定位。架构分两层：

- **知识/交互层**：`.agents/skills/gongkao-data-analysis-coach/`（SKILL.md + references/）
- **确定性计算层**：`scripts/coach.py` + `scripts/coachlib/`（纯 Python 标准库，零第三方依赖）

## 环境

- Python 3.11+，**仅标准库**（sqlite3、argparse、statistics、random、json、unittest）。禁止引入第三方依赖。
- 无构建步骤、无包管理器、无网络访问需求。

## 常用命令

```bash
# 全量测试（修改任何代码后必须运行）
python3 -m unittest discover -s tests/gongkao_coach -p 'test_*.py' -v

# 编译检查
python3 -m compileall -q .agents/skills/gongkao-data-analysis-coach/scripts

# 单测某个模块
python3 -m unittest discover -s tests/gongkao_coach -p 'test_formulas.py' -v
```

## 目录职责

| 路径 | 职责 | 注意事项 |
|---|---|---|
| `.agents/skills/…/SKILL.md` | 技能主工作流与 CLI 契约 | 控制在 500 行内；详细领域内容放 references/ |
| `.agents/skills/…/references/` | 领域知识资产 | 一级引用深度，不嵌套隐藏指令 |
| `.agents/skills/…/scripts/coachlib/` | 确定性核心 | 纯函数优先；所有分母为零或增长率 `<= -1` 抛 `ValueError` |
| `tests/gongkao_coach/` | unittest 测试 | 用 `unittest.TestCase`，**不引入 pytest** |
| `docs/superpowers/` | 设计/计划/审计文档 | 计划文档是历史记录，非当前契约 |
| 根目录 `*.pdf` | 源讲义 | **只读，禁止修改**；已被 .gitignore 排除 |
| `tmp/` | 页面渲染与文本抽取缓存 | gitignored，可随时重建 |
| `.gongkao-study/` | 运行时 SQLite 学习库 | gitignored；**代理不得擅自创建、迁移或删除** |

## 硬性规则（不可违反）

1. **TDD**：先写失败测试，再实现，再跑绿。测试用 unittest，通过 subprocess 调 CLI 时断言 stdout 为单个 JSON、错误写 stderr 且退出码非零。
2. **CLI 契约**：每次调用只输出一个 JSON 文档；成功（含 `--help`）写 stdout，失败写 stderr 且非零退出。不破坏现有子命令与回执字段。
3. **数据库安全**：读取命令不得创建/迁移数据库；写入必须走原子发布（随机 `0600` 暂存文件 + 目标不存在才重命名）；保留 `-wal/-shm/-journal` sidecar；schema v3 必须含 `schema_meta.owner=gongkao-data-analysis-coach` 与 `instance_id`；外来/损坏库一律失败关闭，不自动修复或覆盖。
4. **路径安全**：数据库与导出路径必须解析在工作区内；禁止 `..`、绝对外部路径、符号链接逃逸；导出默认不覆盖，`--force` 需用户明确确认精确路径；活动数据库及其 sidecar 永远不能作为导出目标。
5. **计算确定性**：练习生成用固定 seed 可复现；数值用 `Decimal` + `ROUND_HALF_UP`，不用二进制浮点 `round`；公式输入的增长率必须显式携带 `rate_unit`。
6. **知识图谱完整性**：节点 ID 唯一、parent/prerequisite 存在、无非法权重；图谱损坏时记录/复习/进度命令必须失败关闭，不得用兜底 ID 冒充。
7. **不伪造事实**：不得编造 `attempt_id`、复习日期、「已验证」标签或生成 seed；只有 `ok: true` 回执才可声称写入成功。
8. **Git**：PDF、`tmp/`、`.gongkao-study/`、`__pycache__`、`.worktrees/` 不入库（见 .gitignore）。提交信息沿用 `feat:` / `docs:` / `fix:` 前缀。

## 测试覆盖要点

- `test_formulas.py`：9 个公式恒等式与非法输入
- `test_progress.py`：Wilson 区间、掌握度分级（0 / 1-4 / 5+ / 8+ / 15+ 阈值）
- `test_store.py`：事务写入、回读、到期复习
- `test_cli.py`：子命令 JSON 契约
- `test_practice.py`：生成可复现、选项唯一、独立复算
- `test_knowledge_assets.py`：图谱结构、页码范围、勘误条目
- `test_knowledge_loading.py` / `test_skill_contract.py`：资产加载与 SKILL.md 契约

## 修改 SKILL.md 时的额外要求

- 同步更新 `tests/gongkao_coach/test_skill_contract.py` 中的契约断言。
- 新增领域知识优先放 references/，SKILL.md 只保留模式路由、强制执行顺序与加载条件。
- 变更后运行全量测试 + 编译检查。

## 关键背景

- 当前实现状态：已完成并通过完成度审计（`docs/superpowers/evidence/2026-08-30-completion-audit.md`）。
- 已登记勘误：第六讲 PDF p2 例 21 正确答案 C（讲义标 B）；p9 例 29 标答 A 正确、解析第③步应为「可选 A」。涉及这两题必须标 `source_conflict`。
- 复习阶梯：`wrong` 重置 1 天，`hard` 3 天，`good` 按 1/3/7/14/30 天推进。
