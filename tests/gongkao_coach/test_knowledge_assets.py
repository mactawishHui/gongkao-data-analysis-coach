from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
REFERENCES = (
    ROOT / ".agents" / "skills" / "gongkao-data-analysis-coach" / "references"
)
GRAPH = REFERENCES / "knowledge-graph.json"
TEMPLATES = REFERENCES / "practice-templates.json"

CANONICAL_ERROR_CODES = {
    "wrong_period",
    "wrong_scope",
    "wrong_indicator",
    "wrong_unit",
    "wrong_wording",
    "wrong_denominator",
    "reversed_rates",
    "missing_cross_term",
    "sign_error",
    "formula_error",
    "arithmetic_error",
    "precision_error",
    "unsafe_estimate",
    "method_mismatch",
    "source_conflict",
    "missing_question",
    "parse_uncertain",
}

TOP_LEVEL_IDS = {
    "speed",
    "abrx",
    "growth",
    "share",
    "compare",
    "mixture",
    "average",
    "special",
    "trap",
}
PAGE_COUNTS = {
    1: 14,
    2: 16,
    3: 22,
    4: 23,
    5: 17,
    6: 15,
    7: 33,
    8: 20,
    9: 17,
    10: 27,
}
PDF_NAMES = {
    1: "【花生十三】24下半年资料系统班第一讲--随堂笔记.pdf",
    2: "【花生十三】24下半年资料系统班第二讲--随堂笔记.pdf",
    3: "【花生十三】24下半年资料系统班第三讲--随堂笔记.pdf",
    4: "【花生十三】24下半年资料系统班第四讲--随堂笔记.pdf",
    5: "【花生十三】24下半年资料系统班第五讲--随堂笔记.pdf",
    6: "【花生十三】24下半年资料系统班第六讲--随堂笔.pdf",
    7: "【花生十三】24下半年资料系统班第七讲--随堂笔记.pdf",
    8: "【花生十三】24下半年资料系统班第八讲--随堂笔记.pdf",
    9: "【花生十三】24下半年资料系统班第九讲--随堂笔记.pdf",
    10: "【花生十三】24下半年资料系统班第十讲--随堂笔记.pdf",
}
MARKDOWN_FILES = (
    "knowledge-map.md",
    "formulas.md",
    "fast-methods.md",
    "question-types.md",
    "error-taxonomy.md",
    "progress-model.md",
    "source-map.md",
    "errata.md",
)


class KnowledgeGraphTests(unittest.TestCase):
    def load_json(self, path: Path) -> dict:
        self.assertTrue(path.is_file(), f"missing required asset: {path}")
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            self.fail(f"invalid JSON in {path}: {error}")
        self.assertIsInstance(result, dict)
        return result

    def load_graph_nodes(self) -> list[dict]:
        document = self.load_json(GRAPH)
        self.assertEqual(document.get("version"), 1)
        nodes = document.get("nodes")
        self.assertIsInstance(nodes, list)
        self.assertTrue(nodes)
        return nodes

    def test_graph_has_stable_unique_ids_and_all_nine_roots(self) -> None:
        nodes = self.load_graph_nodes()
        ids = [node.get("id") for node in nodes]

        self.assertEqual(len(ids), 50)
        self.assertEqual(len(ids), len(set(ids)), "knowledge IDs must be unique")
        for knowledge_id in ids:
            self.assertIsInstance(knowledge_id, str)
            self.assertRegex(knowledge_id, r"^[a-z]+(?:[.-][a-z0-9]+)*$")

        roots = {node["id"] for node in nodes if node.get("parent") is None}
        self.assertEqual(roots, TOP_LEVEL_IDS)
        self.assertEqual({knowledge_id.split(".")[0] for knowledge_id in ids}, TOP_LEVEL_IDS)
        self.assertTrue(
            all(node.get("assessable") is False for node in nodes if node["id"] in roots)
        )
        self.assertTrue(
            all(node.get("assessable", True) is True for node in nodes if node["id"] not in roots)
        )
        self.assertEqual(sum(node.get("assessable", True) for node in nodes), 41)

    def test_every_node_has_weight_sources_and_coaching_metadata(self) -> None:
        nodes = self.load_graph_nodes()
        required = {
            "id",
            "title",
            "parent",
            "prerequisites",
            "weight",
            "sources",
            "signals",
            "formula_ids",
            "fast_methods",
            "error_codes",
        }
        for node in nodes:
            with self.subTest(node=node.get("id")):
                self.assertTrue(required.issubset(node), required - set(node))
                self.assertIsInstance(node["title"], str)
                self.assertTrue(node["title"].strip())
                self.assertIsInstance(node["weight"], (int, float))
                self.assertNotIsInstance(node["weight"], bool)
                self.assertGreater(node["weight"], 0)
                self.assertIsInstance(node["prerequisites"], list)
                self.assertIsInstance(node["formula_ids"], list)
                for field in ("sources", "signals", "fast_methods", "error_codes"):
                    self.assertIsInstance(node[field], list)
                    self.assertTrue(node[field], f"{node['id']} has empty {field}")
                for field in ("signals", "fast_methods", "error_codes"):
                    self.assertTrue(
                        all(isinstance(value, str) and value.strip() for value in node[field])
                    )

    def test_dependencies_exist_and_the_graph_is_acyclic(self) -> None:
        nodes = self.load_graph_nodes()
        by_id = {node["id"]: node for node in nodes}
        edges: dict[str, list[str]] = {}
        for node in nodes:
            dependencies = list(node["prerequisites"])
            if node["parent"] is not None:
                dependencies.append(node["parent"])
            self.assertNotIn(node["id"], dependencies)
            for dependency in dependencies:
                self.assertIn(dependency, by_id, f"missing dependency {dependency}")
            edges[node["id"]] = dependencies

        state: dict[str, int] = {}

        def visit(knowledge_id: str) -> None:
            if state.get(knowledge_id) == 1:
                self.fail(f"dependency cycle reaches {knowledge_id}")
            if state.get(knowledge_id) == 2:
                return
            state[knowledge_id] = 1
            for dependency in edges[knowledge_id]:
                visit(dependency)
            state[knowledge_id] = 2

        for knowledge_id in edges:
            visit(knowledge_id)

    def test_sources_use_real_pdf_page_indexes_and_cover_all_lessons(self) -> None:
        nodes = self.load_graph_nodes()
        covered_lessons: set[int] = set()
        for node in nodes:
            for source in node["sources"]:
                with self.subTest(node=node["id"], source=source):
                    self.assertIsInstance(source, dict)
                    self.assertEqual(set(source), {"lesson", "pages"})
                    lesson = source["lesson"]
                    pages = source["pages"]
                    self.assertIn(lesson, PAGE_COUNTS)
                    self.assertIsInstance(pages, list)
                    self.assertTrue(pages)
                    self.assertEqual(pages, sorted(set(pages)))
                    self.assertTrue(all(isinstance(page, int) for page in pages))
                    self.assertTrue(all(1 <= page <= PAGE_COUNTS[lesson] for page in pages))
                    covered_lessons.add(lesson)

        self.assertEqual(covered_lessons, set(PAGE_COUNTS))

    def test_graph_covers_required_core_topics_and_practice_nodes(self) -> None:
        nodes = self.load_graph_nodes()
        ids = {node["id"] for node in nodes}
        required_topics = {
            "speed.415",
            "speed.assumed-allocation",
            "abrx.general",
            "abrx.current",
            "abrx.base",
            "abrx.rate",
            "abrx.growth-amount",
            "growth.basic",
            "growth.interval",
            "growth.ratio",
            "growth.product",
            "share.current",
            "share.base",
            "share.multilevel",
            "share.trend",
            "share.change",
            "share.value-difference",
            "compare.increment",
            "compare.increment-rate",
            "compare.catch-up",
            "compare.find-then-calculate",
            "mixture.salt-water",
            "mixture.cross",
            "average.single",
            "average.multiple",
            "average.annual-increase",
            "average.annual-rate",
            "special.pull",
            "special.contribution",
            "special.inclusion-exclusion",
            "trap.integrated",
        }
        self.assertTrue(required_topics.issubset(ids), required_topics - ids)

        templates = self.load_json(TEMPLATES)["templates"]
        practice_ids = [template["knowledge_id"] for template in templates]
        self.assertEqual(len(practice_ids), 10)
        self.assertEqual(len(practice_ids), len(set(practice_ids)))
        self.assertTrue(set(practice_ids).issubset(ids), set(practice_ids) - ids)

    def test_graph_error_codes_are_canonical(self) -> None:
        nodes = self.load_graph_nodes()
        used_codes = {
            code
            for node in nodes
            for code in node["error_codes"]
        }

        self.assertTrue(used_codes)
        self.assertTrue(
            used_codes.issubset(CANONICAL_ERROR_CODES),
            used_codes - CANONICAL_ERROR_CODES,
        )

    def test_graph_formula_ids_resolve_to_the_formula_registry(self) -> None:
        nodes = self.load_graph_nodes()
        formula_text = (REFERENCES / "formulas.md").read_text(encoding="utf-8")
        documented_ids = {
            formula_id
            for line in formula_text.splitlines()
            if "**公式 ID**" in line
            for formula_id in re.findall(r"`([a-z][a-z0-9_]*)`", line)
        }
        used_ids = {
            formula_id
            for node in nodes
            for formula_id in node["formula_ids"]
        }

        self.assertTrue(documented_ids)
        self.assertTrue(used_ids.issubset(documented_ids), used_ids - documented_ids)

    def test_boundary_sensitive_fast_methods_keep_their_conditions(self) -> None:
        nodes = {node["id"]: node for node in self.load_graph_nodes()}

        self.assertIn(
            "现期比重/(1+部分率)<1",
            " ".join(nodes["share.change"]["fast_methods"]),
        )
        self.assertIn(
            "均正增长",
            " ".join(nodes["compare.increment"]["fast_methods"]),
        )
        self.assertIn(
            "仅题目按连续同比增量定义时前推",
            " ".join(nodes["average.annual-increase"]["fast_methods"]),
        )

    def test_overlap_pages_and_continuations_are_not_dropped(self) -> None:
        nodes = {node["id"]: node for node in self.load_graph_nodes()}

        def pages(node_id: str, lesson: int) -> set[int]:
            return {
                page
                for source in nodes[node_id]["sources"]
                if source["lesson"] == lesson
                for page in source["pages"]
            }

        self.assertIn(23, pages("compare.fraction", 7))
        self.assertIn(29, pages("compare.increment-rate", 7))
        self.assertTrue({19, 20}.issubset(pages("mixture.cross", 8)))


class HumanReadableReferenceTests(unittest.TestCase):
    def read_asset(self, filename: str) -> str:
        path = REFERENCES / filename
        self.assertTrue(path.is_file(), f"missing required asset: {path}")
        text = path.read_text(encoding="utf-8")
        self.assertGreater(len(text), 300, f"placeholder-sized asset: {path}")
        self.assertNotRegex(text, r"(?i)\b(?:TODO|TBD)\b|待补充|占位")
        return text

    def test_all_human_readable_assets_are_substantive(self) -> None:
        documents = {filename: self.read_asset(filename) for filename in MARKDOWN_FILES}
        combined = "\n".join(documents.values())
        for keyword in ("偏大", "偏小", "误差", "选项", "失效", "精算"):
            self.assertIn(keyword, combined)

    def test_fast_methods_follow_the_speed_route_and_safety_contract(self) -> None:
        text = self.read_asset("fast-methods.md")
        route = (
            "定性",
            "范围量级",
            "特征数字",
            "常用分数/份数",
            "拆分假设",
            "截位直除",
            "精算",
        )
        positions = [text.index(label) for label in route]
        self.assertEqual(positions, sorted(positions))
        for field in (
            "适用信号",
            "操作",
            "偏大偏小/误差控制",
            "选项安全条件",
            "失效条件",
            "人类心算示例",
        ):
            self.assertGreaterEqual(text.count(field), len(route), field)
        for phrase in ("现期×r/(1+r)", "快速修正", "决策余量", "选项边界"):
            self.assertIn(phrase, text)

    def test_question_types_use_recognition_to_transfer_contract(self) -> None:
        text = self.read_asset("question-types.md")
        for field in ("识别", "最快法", "稳妥法", "陷阱", "迁移"):
            self.assertGreaterEqual(text.count(field), 10, field)
        self.assertRegex(
            text,
            r"增速、分数与基期比较（`compare\.fraction`）",
        )
        self.assertRegex(
            text,
            r"增长量一大一小比较（`compare\.increment-rate`）",
        )
        increment_rate_section = text.split(
            "增长量一大一小比较（`compare.increment-rate`）",
            1,
        )[1].split("\n## ", 1)[0]
        for token in ("B×R/(1+R)", "growth_amount", "选项过密", "compare.fraction"):
            self.assertIn(token, increment_rate_section)

    def test_formula_reference_records_units_boundaries_and_script_mapping(self) -> None:
        text = self.read_asset("formulas.md")
        for field in ("公式 ID", "公式", "量纲", "边界", "脚本函数映射"):
            self.assertIn(field, text)
        for function_name in (
            "base_period",
            "growth_amount",
            "interval_growth",
            "ratio_growth",
            "product_growth",
            "current_share",
            "base_share",
            "share_change",
            "contribution_rate",
            "assess_estimate_safety",
        ):
            self.assertIn(function_name, text)
        contribution_section = text.split("增量贡献率", 1)[1].split("\n### ", 1)[0]
        self.assertIn("整体增量为正", contribution_section)
        self.assertIn("整体增量为负", contribution_section)
        self.assertIn("反序", contribution_section)

    def test_contribution_ranking_documents_denominator_sign(self) -> None:
        text = self.read_asset("question-types.md")
        contribution_section = text.split("拉动增长与增量贡献率", 1)[1].split(
            "\n## ", 1
        )[0]

        for phrase in ("整体增量为正", "整体增量为负", "反序", "整体增量为 0"):
            self.assertIn(phrase, contribution_section)

    def test_error_taxonomy_is_root_cause_coded(self) -> None:
        text = self.read_asset("error-taxonomy.md")
        for field in ("错误代码", "根因", "识别证据", "纠正动作", "复发预防"):
            self.assertIn(field, text)
        for code in (
            "wrong_period",
            "wrong_scope",
            "wrong_unit",
            "reversed_rates",
            "missing_cross_term",
            "unsafe_estimate",
            "source_conflict",
        ):
            self.assertIn(code, text)

        documented_codes = set(
            re.findall(r"^\| `([a-z_]+)` \|", text, flags=re.MULTILINE)
        )
        self.assertEqual(documented_codes, CANONICAL_ERROR_CODES)

    def test_progress_model_documents_six_dimensions_wilson_and_review_rules(self) -> None:
        text = self.read_asset("progress-model.md")
        for dimension in ("准确性", "速度", "方法选择", "审题", "验算", "迁移"):
            self.assertIn(dimension, text)
        for phrase in (
            "Wilson",
            "未评估",
            "样本不足",
            "学习中",
            "基本掌握",
            "稳定掌握",
            "提前复习不计",
            "复习间隔",
        ):
            self.assertIn(phrase, text)

    def test_knowledge_map_documents_order_and_dependency_tree(self) -> None:
        text = self.read_asset("knowledge-map.md")
        for phrase in (
            "学习顺序",
            "依赖树",
            "速算基础",
            "ABRX",
            "增长率",
            "比重",
            "比较",
            "混合",
            "平均",
            "特殊",
            "陷阱",
            "compare.catch-up",
            "compare.find-then-calculate",
        ):
            self.assertIn(phrase, text)

    def test_source_map_uses_exact_filenames_page_counts_and_chapter_ranges(self) -> None:
        text = self.read_asset("source-map.md")
        self.assertIn("PDF 实际页索引", text)
        self.assertIn("章节页段", text)
        for lesson, filename in PDF_NAMES.items():
            with self.subTest(lesson=lesson):
                self.assertIn(filename, text)
                row_pattern = rf"\|\s*{lesson}\s*\|[^\n]*\|\s*{PAGE_COUNTS[lesson]}\s*\|"
                self.assertRegex(text, row_pattern)
        self.assertIn("p19–23 特殊阈值与折线趋势比较", text)

    def test_errata_has_two_structured_authoritative_entries(self) -> None:
        text = self.read_asset("errata.md")
        for field in ("记录 ID", "讲次", "PDF 实际页", "对象", "原文", "更正", "判定依据", "证据优先级", "状态"):
            self.assertIn(field, text)
        for record_id in ("ERRATA-06-021", "ERRATA-06-029"):
            self.assertIn(record_id, text)
        self.assertRegex(text, r"例\s*21[^\n]*(?:B[^\n]*C|C[^\n]*B)")
        self.assertRegex(text, r"例\s*29[^\n]*(?:可选\s*B[^\n]*可选\s*A|可选\s*A[^\n]*可选\s*B)")
        self.assertIn("PDF p2", text)
        self.assertIn("PDF p9", text)
        self.assertIn("数学恒等式", text)
        self.assertIn("高于讲义标答", text)


if __name__ == "__main__":
    unittest.main()
