from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class CleanScriptTests(unittest.TestCase):
    def test_build_manifest_writes_stage_outputs(self) -> None:
        from build_foreign_exchange_manifest import build_manifest

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            output = root / "tools" / "clean" / "manifest"
            source.mkdir()
            (source / "交易指南.md").write_text(
                "# 交易指南\n\nSource: https://example.test/doc\n\n- [附件.docx](javascript:void(0);)\n",
                encoding="utf-8",
            )
            (source / "交易指南_1.pdf").write_bytes(b"%PDF-1.4")
            (source / "开户申请表.docx").write_bytes(b"docx")

            summary = build_manifest(source, output)

            manifest_path = output / "manifest.jsonl"
            summary_path = output / "manifest_summary.md"
            rows = read_jsonl(manifest_path)

            self.assertEqual(summary["total_files"], 3)
            self.assertTrue(manifest_path.exists())
            self.assertTrue(summary_path.exists())
            self.assertEqual(len(rows), 3)

            md = next(row for row in rows if row["file_name"] == "交易指南.md")
            pdf = next(row for row in rows if row["file_name"] == "交易指南_1.pdf")
            form = next(row for row in rows if row["file_name"] == "开户申请表.docx")

            self.assertIs(md["is_markdown"], True)
            self.assertIs(md["has_source_line"], True)
            self.assertIs(md["has_javascript_void_link"], True)
            self.assertIs(md["is_mojibake_suspected"], False)
            self.assertIs(md["is_duplicate_version_suspected"], True)
            self.assertIs(pdf["is_duplicate_version_suspected"], True)
            self.assertIs(form["is_form_like_suspected"], True)
            self.assertTrue(all(row["processing_status"] == "manifested" for row in rows))

    def test_check_encoding_writes_machine_and_human_reports(self) -> None:
        from build_foreign_exchange_manifest import build_manifest
        from check_foreign_exchange_md_encoding import check_markdown_encoding

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            manifest_dir = root / "tools" / "clean" / "manifest"
            encoding_dir = root / "tools" / "clean" / "encoding_check"
            source.mkdir()
            (source / "正常.md").write_text("# 交易确认\n\n银行间外汇市场交易确认。\n", encoding="utf-8")
            (source / "误读.md").write_text("# 2026骞磋鐢熷搧浜ゆ槗鍐查攢鏃ュ巻\n", encoding="utf-8")

            build_manifest(source, manifest_dir)
            summary = check_markdown_encoding(manifest_dir / "manifest.jsonl", encoding_dir)

            report_path = encoding_dir / "encoding_report.jsonl"
            summary_path = encoding_dir / "encoding_summary.md"
            rows = read_jsonl(report_path)

            self.assertEqual(summary["markdown_files"], 2)
            self.assertTrue(report_path.exists())
            self.assertTrue(summary_path.exists())
            self.assertEqual(len(rows), 2)

            normal = next(row for row in rows if row["file_name"] == "正常.md")
            mojibake = next(row for row in rows if row["file_name"] == "误读.md")

            self.assertEqual(normal["read_encoding"], "utf-8")
            self.assertIs(normal["utf8_read_ok"], True)
            self.assertIs(normal["needs_encoding_repair"], False)
            self.assertIs(normal["repair_attempted"], False)
            self.assertEqual(normal["repair_status"], "not_needed")

            self.assertIs(mojibake["utf8_read_ok"], True)
            self.assertGreater(mojibake["mojibake_marker_count"], 0)
            self.assertIs(mojibake["needs_encoding_repair"], True)
            self.assertIs(mojibake["repair_attempted"], False)
            self.assertEqual(mojibake["repair_status"], "needs_review")

    def test_stratified_sampling_covers_lengths_and_risk_layers(self) -> None:
        from stratified_sample_markdown import build_round1_samples

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            manifest_dir = root / "tools" / "clean" / "manifest"
            encoding_dir = root / "tools" / "clean" / "encoding_check"
            sample_dir = root / "tools" / "clean" / "samples"
            source.mkdir()

            def write_md(name: str, body: str) -> Path:
                path = source / name
                path.write_text(body, encoding="utf-8")
                return path

            files = [
                write_md(
                    "短附件.md",
                    "# 短附件\n\nSource: https://example.test/a\n\n相关稿件请在附件列表中点击下载后阅读\n\n- [附件.docx](javascript:void(0);)\n",
                ),
                write_md("短正文.md", "# 短正文\n\nSource: https://example.test/b\n\n银行间外汇市场。\n"),
                write_md("中正文.md", "# 中正文\n\nSource: https://example.test/c\n\n" + "交易确认规则。" * 120),
                write_md("长正文.md", "# 长正文\n\nSource: https://example.test/d\n\n" + "债券市场业务规则。" * 250),
                write_md("超长正文.md", "# 超长正文\n\nSource: https://example.test/e\n\n" + "外汇市场交易服务。" * 500),
                write_md("重复指南.md", "# 重复指南\n\nSource: https://example.test/f\n\n" + "正文。" * 80),
                write_md("重复指南_1.md", "# 重复指南\n\nSource: https://example.test/g\n\n" + "正文。" * 80),
                write_md("开户申请表.md", "# 开户申请表\n\nSource: https://example.test/h\n\n机构名称：\n"),
            ]

            manifest_rows = []
            duplicate_groups = {"重复指南": 2}
            for path in files:
                text = path.read_text(encoding="utf-8")
                key = path.stem.removesuffix("_1")
                manifest_rows.append(
                    {
                        "source_path": str(path),
                        "file_name": path.name,
                        "extension": ".md",
                        "size_bytes": path.stat().st_size,
                        "is_markdown": True,
                        "is_mojibake_suspected": False,
                        "has_source_line": True,
                        "has_javascript_void_link": "javascript:void(0)" in text,
                        "is_duplicate_version_suspected": duplicate_groups.get(key, 1) > 1,
                        "duplicate_group_key": key,
                        "is_form_like_suspected": "申请表" in path.name,
                        "processing_status": "manifested",
                    }
                )
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "manifest.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in manifest_rows) + "\n",
                encoding="utf-8",
            )

            encoding_rows = []
            char_counts = {
                "短附件.md": 200,
                "短正文.md": 300,
                "中正文.md": 1200,
                "长正文.md": 4000,
                "超长正文.md": 9000,
                "重复指南.md": 1100,
                "重复指南_1.md": 1100,
                "开户申请表.md": 260,
            }
            for path in files:
                encoding_rows.append(
                    {
                        "source_path": str(path),
                        "file_name": path.name,
                        "read_encoding": "utf-8",
                        "utf8_read_ok": True,
                        "source_sha256": "source",
                        "text_sha256": "text",
                        "char_count": char_counts[path.name],
                        "non_empty_line_count": 3,
                        "mojibake_marker_count": 0,
                        "replacement_char_count": 0,
                        "cjk_char_ratio": 0.5,
                        "needs_encoding_repair": False,
                        "repair_attempted": False,
                        "repair_status": "not_needed",
                        "review_reason": None,
                    }
                )
            encoding_dir.mkdir(parents=True)
            (encoding_dir / "encoding_report.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in encoding_rows) + "\n",
                encoding="utf-8",
            )

            summary = build_round1_samples(
                manifest_dir / "manifest.jsonl",
                encoding_dir / "encoding_report.jsonl",
                sample_dir,
                sample_size=8,
                seed=20260528,
            )
            sample_rows = read_jsonl(sample_dir / "round1_sample_manifest.jsonl")

            self.assertEqual(summary["target_sample_size"], 8)
            self.assertEqual(summary["sample_count"], 8)
            self.assertTrue((sample_dir / "round1_review.md").exists())
            self.assertTrue((sample_dir / "round1_sample_summary.md").exists())
            self.assertEqual(len(sample_rows), 8)
            self.assertEqual(len({row["source_path"] for row in sample_rows}), 8)
            self.assertTrue(any(row["length_bucket"] == "long" for row in sample_rows))
            self.assertTrue(any(row["length_bucket"] == "very_long" for row in sample_rows))
            self.assertTrue(any(row["has_javascript_void_link"] for row in sample_rows))
            self.assertTrue(any(row["is_duplicate_version_suspected"] for row in sample_rows))
            self.assertTrue(any(row["is_form_like_suspected"] for row in sample_rows))

    def test_clean_markdown_applies_confirmed_noise_rules(self) -> None:
        from build_foreign_exchange_manifest import build_manifest
        from check_foreign_exchange_md_encoding import check_markdown_encoding
        from clean_foreign_exchange_md import clean_markdown_corpus

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            manifest_dir = root / "tools" / "clean" / "manifest"
            encoding_dir = root / "tools" / "clean" / "encoding_check"
            output_root = root / "tools" / "clean"
            source.mkdir()

            (source / "实质通知.md").write_text(
                "\n".join(
                    [
                        "# 关于业务优化功能上线的通知",
                        "",
                        "Source: https://example.test/substantive",
                        "",
                        "关于业务优化功能上线的通知",
                        "",
                        "为推进业务发展，交易中心将于2024年5月20日上线新功能。现就业务有关事项通知如下：",
                        "",
                        "一、参与机构可通过交易系统提交申请。",
                        "",
                        "附件：1. 操作流程",
                        "",
                        "- [附件1：操作流程.docx](javascript:void(0);)",
                    ]
                ),
                encoding="utf-8",
            )
            (source / "空壳下载页.md").write_text(
                "\n".join(
                    [
                        "# 债券借贷业务操作指南",
                        "",
                        "Source: https://example.test/download-only",
                        "",
                        "债券借贷业务操作指南",
                        "",
                        "相关稿件请在附件列表中点击下载后阅读",
                        "",
                        "- [债券借贷业务操作指南.docx](javascript:void(0);)",
                    ]
                ),
                encoding="utf-8",
            )
            (source / "简短发布公告.md").write_text(
                "\n".join(
                    [
                        "# 关于发布《银行间外汇市场主经纪业务指引》修订版的公告",
                        "",
                        "Source: https://example.test/brief",
                        "",
                        "关于发布《银行间外汇市场主经纪业务指引》修订版的公告",
                        "",
                        "中汇交公告〔2023〕20号",
                        "",
                        "为落实相关管理规定，现发布《银行间外汇市场主经纪业务指引》修订版。",
                        "",
                        "附件：《银行间外汇市场主经纪业务指引》修订版",
                        "",
                        "中国外汇交易中心",
                        "",
                        "2023年4月20日",
                        "",
                        "- [银行间外汇市场主经纪业务指引.docx](javascript:void(0);)",
                    ]
                ),
                encoding="utf-8",
            )
            duplicate_body = "\n".join(
                [
                    "# 重复正文",
                    "",
                    "Source: https://example.test/dup",
                    "",
                    "重复正文",
                    "",
                    "本文件包含可独立检索的业务规则内容。",
                ]
            )
            (source / "重复正文.md").write_text(duplicate_body, encoding="utf-8")
            (source / "重复正文_1.md").write_text(duplicate_body, encoding="utf-8")

            build_manifest(source, manifest_dir)
            check_markdown_encoding(manifest_dir / "manifest.jsonl", encoding_dir)
            summary = clean_markdown_corpus(
                manifest_dir / "manifest.jsonl",
                encoding_dir / "encoding_report.jsonl",
                output_root,
            )

            clean_dir = output_root / "clean_md"
            report_dir = output_root / "noise_report"
            decisions = read_jsonl(report_dir / "cleaning_decisions.jsonl")

            self.assertTrue((clean_dir / "实质通知.md").exists())
            substantive = (clean_dir / "实质通知.md").read_text(encoding="utf-8")
            self.assertNotIn("Source:", substantive)
            self.assertNotIn("javascript:void(0)", substantive)
            self.assertNotIn("附件：1. 操作流程", substantive)
            self.assertIn("一、参与机构可通过交易系统提交申请。", substantive)

            self.assertFalse((clean_dir / "空壳下载页.md").exists())
            self.assertFalse((clean_dir / "简短发布公告.md").exists())

            duplicate_outputs = sorted(path.name for path in clean_dir.glob("重复正文*.md"))
            self.assertEqual(len(duplicate_outputs), 1)

            statuses = {row["file_name"]: row["decision"] for row in decisions}
            self.assertEqual(statuses["实质通知.md"], "kept")
            self.assertEqual(statuses["空壳下载页.md"], "dropped_download_only_page")
            self.assertEqual(statuses["简短发布公告.md"], "dropped_brief_publish_notice")
            self.assertIn("dropped_exact_duplicate", statuses.values())
            self.assertEqual(summary["total_markdown"], 5)
            self.assertEqual(summary["kept"], 2)

    def test_clean_markdown_removes_numbered_attachment_title_block(self) -> None:
        from clean_foreign_exchange_md import clean_markdown_text

        source = "\n".join(
            [
                "# 关于业务上线的通知",
                "",
                "正文保留。",
                "",
                "附件：1. 交易员信息登记表",
                "",
                "2. 外汇交易系统用户实名认证操作手册",
                "",
                "中国外汇交易中心",
                "",
                "2024年4月4日",
                "",
                "附件：",
                "",
                "- [附件1-交易员信息登记表.xls](javascript:void(0);)",
            ]
        )

        clean_text, stats = clean_markdown_text(source)

        self.assertIn("正文保留。", clean_text)
        self.assertIn("中国外汇交易中心", clean_text)
        self.assertIn("2024年4月4日", clean_text)
        self.assertNotIn("附件：", clean_text)
        self.assertNotIn("交易员信息登记表", clean_text)
        self.assertNotIn("实名认证操作手册", clean_text)
        self.assertGreaterEqual(stats["removed_attachment_prompt_lines"], 3)

    def test_clean_md_quality_sample_flags_residual_noise(self) -> None:
        from review_clean_md_quality import build_clean_md_quality_sample

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            clean_dir = root / "tools" / "clean" / "clean_md"
            report_dir = root / "tools" / "clean" / "noise_report"
            quality_dir = root / "tools" / "clean" / "quality_check"
            clean_dir.mkdir(parents=True)
            report_dir.mkdir(parents=True)

            (clean_dir / "正常正文.md").write_text(
                "# 正常正文\n\n一、参与机构应按规则提交交易确认。\n",
                encoding="utf-8",
            )
            (clean_dir / "残留噪声.md").write_text(
                "# 残留噪声\n\nSource: https://example.test\n\n- [附件.doc](javascript:void(0);)\n",
                encoding="utf-8",
            )
            decisions = [
                {
                    "file_name": "正常正文.md",
                    "source_path": str(root / "source" / "正常正文.md"),
                    "decision": "kept",
                    "removed_source_lines": 1,
                    "removed_javascript_void_lines": 0,
                    "removed_attachment_prompt_lines": 0,
                    "removed_download_prompt_lines": 0,
                    "removed_repeated_title_lines": 1,
                    "original_char_count": 120,
                    "clean_char_count": 31,
                },
                {
                    "file_name": "残留噪声.md",
                    "source_path": str(root / "source" / "残留噪声.md"),
                    "decision": "kept",
                    "removed_source_lines": 0,
                    "removed_javascript_void_lines": 0,
                    "removed_attachment_prompt_lines": 0,
                    "removed_download_prompt_lines": 0,
                    "removed_repeated_title_lines": 0,
                    "original_char_count": 80,
                    "clean_char_count": 67,
                },
            ]
            (report_dir / "cleaning_decisions.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in decisions) + "\n",
                encoding="utf-8",
            )

            summary = build_clean_md_quality_sample(
                clean_dir,
                report_dir / "cleaning_decisions.jsonl",
                quality_dir,
                sample_size=2,
                seed=20260528,
            )
            rows = read_jsonl(quality_dir / "round1_clean_md_review.jsonl")

            self.assertEqual(summary["sample_count"], 2)
            self.assertTrue((quality_dir / "round1_clean_md_review.md").exists())
            self.assertEqual(len(rows), 2)
            noisy = next(row for row in rows if row["file_name"] == "残留噪声.md")
            self.assertIn("source_line_residue", noisy["leakage_flags"])
            self.assertIn("javascript_void_residue", noisy["leakage_flags"])
            self.assertGreater(summary["flagged_sample_count"], 0)


if __name__ == "__main__":
    unittest.main()
