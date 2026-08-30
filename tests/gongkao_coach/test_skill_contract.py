"""Static contract tests for the user-facing coaching skill."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = ROOT / ".agents/skills/gongkao-data-analysis-coach"
SKILL = SKILL_DIR / "SKILL.md"
PROTOCOL = SKILL_DIR / "references/answer-protocol.md"
OPENAI_YAML = SKILL_DIR / "agents/openai.yaml"


def read_skill() -> tuple[str, str]:
    text = SKILL.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    if match is None:
        raise AssertionError("SKILL.md must start with YAML frontmatter")
    return match.group(1), text


class SkillContractTests(unittest.TestCase):
    def test_frontmatter_is_minimal_and_discoverable(self) -> None:
        frontmatter, text = read_skill()
        keys = [
            line.split(":", 1)[0].strip()
            for line in frontmatter.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

        self.assertEqual(keys, ["name", "description"])
        self.assertIn("name: gongkao-data-analysis-coach", frontmatter)
        description = next(
            line.split(":", 1)[1].strip()
            for line in frontmatter.splitlines()
            if line.startswith("description:")
        )
        self.assertTrue(description.startswith("Use when"), description)
        self.assertNotIn("TODO", text)
        self.assertLess(len(text.splitlines()), 500)

    def test_all_seven_modes_and_input_gate_are_explicit(self) -> None:
        _, text = read_skill()
        for token in (
            "输入完整性闸门",
            "解题模式",
            "陪练模式",
            "记录模式",
            "专项练习模式",
            "诊断模式",
            "错题复习模式",
            "进度模式",
            "时间",
            "对象",
            "指标",
            "单位",
            "口径",
            "选项",
            "不猜数",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_speed_route_is_ordered_and_option_driven(self) -> None:
        _, text = read_skill()
        route = (
            "定性秒杀",
            "范围与量级",
            "特征数字",
            "常用分数与份数",
            "拆分与假设分配",
            "截位直除",
            "完整精算",
        )
        positions = [text.index(step) for step in route]

        self.assertEqual(positions, sorted(positions))
        for token in ("选项间距", "决策边界", "命中即停", "45 秒"):
            self.assertIn(token, text)

    def test_approximation_and_verification_rules_are_mandatory(self) -> None:
        _, text = read_skill()
        for token in (
            "近似假设",
            "偏大还是偏小",
            "决策余量",
            "不会跨越",
            "失效边界",
            "升级精度",
            "确定性脚本",
            "独立验算",
            "单位复核",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_learning_loop_never_fakes_state_or_mastery(self) -> None:
        _, text = read_skill()
        for token in (
            "JSON 成功回执",
            "不得声称“已记录”",
            "同类题总结",
            "迁移题",
            "知识图谱位置",
            "1、3、7、14、30",
            "样本不足",
            "未充分评估",
            "Wilson 90%",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)

        self.assertIn("`knowledge_ids[0]`", text)
        self.assertIn("只计第 1 项", text)

    def test_documented_cli_flow_matches_safe_runtime_contract(self) -> None:
        _, skill_text = read_skill()
        protocol_text = PROTOCOL.read_text(encoding="utf-8")

        self.assertIn("record --json-file -", skill_text)
        self.assertNotIn("record --json '<attempt JSON object>'", skill_text)
        self.assertIn("calculate --formula FORMULA_NAME --json-file -", skill_text)
        self.assertIn("check-estimate --json-file -", skill_text)
        self.assertIn('rate_unit: "decimal"', skill_text)
        self.assertIn('rate_unit: "percent"', skill_text)
        self.assertIn("bound_verified: true", skill_text)
        self.assertIn("safe: true", skill_text)
        self.assertIn("当前 `review_id`", skill_text)
        self.assertIn("默认不覆盖已有文件", skill_text)
        self.assertIn("活动数据库本身永远不能作为导出目标", skill_text)
        self.assertIn("标准输入", protocol_text)
        self.assertIn("attempt <attempt_id>", protocol_text)

    def test_graph_failure_is_closed_not_faked(self) -> None:
        _, text = read_skill()

        self.assertIn("必须失败关闭", text)
        self.assertIn("不得使用内置兜底 ID", text)

    def test_persistence_requires_explicit_user_consent(self) -> None:
        _, skill_text = read_skill()
        protocol_text = PROTOCOL.read_text(encoding="utf-8")
        interface_text = OPENAI_YAML.read_text(encoding="utf-8")

        self.assertIn("首次写入必须获得明确同意", skill_text)
        self.assertIn("提供答案、用时或信心不等于授权写入", skill_text)
        self.assertIn("明确要求记录", interface_text)
        self.assertIn("不等于写入授权", protocol_text)
        self.assertIn("高=4", skill_text)
        self.assertIn("“高信心”是 4，不是 5", protocol_text)

    def test_source_precedence_errata_and_reference_routing_are_explicit(self) -> None:
        _, text = read_skill()
        for token in (
            "数学恒等式或确定性脚本",
            "题目原始数据和图表口径",
            "讲义解析",
            "讲义标答",
            "第六讲 PDF 第 2 页",
            "第六讲 PDF 第 9 页",
            "references/errata.md",
            "references/fast-methods.md",
            "references/answer-protocol.md",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)

        links = re.findall(r"\]\(([^)]+)\)", text)
        reference_links = [link for link in links if link.startswith("references/")]
        self.assertTrue(reference_links)
        self.assertTrue(
            all(link.count("/") == 1 for link in reference_links),
            reference_links,
        )

    def test_every_direct_reference_link_exists(self) -> None:
        _, text = read_skill()
        links = re.findall(r"\]\((references/[^)]+)\)", text)

        self.assertTrue(links)
        for link in links:
            with self.subTest(link=link):
                self.assertTrue((SKILL_DIR / link).is_file(), link)

    def test_answer_protocol_covers_outputs_and_real_receipts(self) -> None:
        text = PROTOCOL.read_text(encoding="utf-8")
        for token in (
            "解题输出",
            "陪练输出",
            "记录回执",
            "专项练习输出",
            "诊断输出",
            "错题复习输出",
            "进度输出",
            "审题卡",
            "估算安全卡",
            "JSON",
            "attempt_id",
            "database",
            "next_review_date",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
