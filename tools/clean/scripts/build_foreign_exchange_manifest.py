from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Iterable


MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdx"}
IGNORED_DIRECTORY_NAMES = {"filter"}
MOJIBAKE_MARKERS = (
    "鍏",
    "涓",
    "骞",
    "佸",
    "绋",
    "鏂",
    "瀹",
    "搧",
    "槗",
    "攢",
    "€",
)
FORM_LIKE_KEYWORDS = (
    "申请",
    "申请表",
    "登记",
    "登记表",
    "开户",
    "应急",
    "权限",
    "信息登记",
    "变更",
    "注销",
)


def duplicate_group_key(path: Path) -> str:
    stem = path.stem.strip().lower()
    stem = re.sub(r"(?:[_-]\d+)+$", "", stem)
    stem = re.sub(r"\s+", " ", stem)
    return stem


def count_markers(text: str, markers: Iterable[str] = MOJIBAKE_MARKERS) -> int:
    return sum(text.count(marker) for marker in markers)


def read_markdown_text(path: Path) -> tuple[str, bool]:
    try:
        return path.read_text(encoding="utf-8"), True
    except UnicodeDecodeError:
        return "", False


def discover_files(source_dir: Path) -> list[Path]:
    files = []
    for path in source_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative_parts = path.relative_to(source_dir).parts[:-1]
        except ValueError:
            relative_parts = path.parts[:-1]
        if any(part.lower() in IGNORED_DIRECTORY_NAMES for part in relative_parts):
            continue
        files.append(path)
    return sorted(files, key=lambda p: str(p).lower())


def build_records(source_dir: Path) -> list[dict]:
    files = discover_files(source_dir)
    group_counts = Counter(duplicate_group_key(path) for path in files)
    records: list[dict] = []

    for path in files:
        ext = path.suffix.lower()
        is_markdown = ext in MARKDOWN_EXTENSIONS
        text = ""
        utf8_ok = False
        if is_markdown:
            text, utf8_ok = read_markdown_text(path)

        group_key = duplicate_group_key(path)
        file_name = path.name
        records.append(
            {
                "source_path": str(path),
                "file_name": file_name,
                "extension": ext,
                "size_bytes": path.stat().st_size,
                "is_markdown": is_markdown,
                "is_mojibake_suspected": bool(is_markdown and (not utf8_ok or count_markers(text) > 0)),
                "has_source_line": bool(is_markdown and re.search(r"(?m)^Source:\s*", text)),
                "has_javascript_void_link": bool(is_markdown and "javascript:void(0)" in text),
                "is_duplicate_version_suspected": group_counts[group_key] > 1,
                "duplicate_group_key": group_key,
                "is_form_like_suspected": any(keyword in file_name for keyword in FORM_LIKE_KEYWORDS),
                "processing_status": "manifested",
            }
        )

    return records


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def summarize(records: list[dict], source_dir: Path, output_dir: Path) -> dict:
    ext_counts = Counter(row["extension"] or "<none>" for row in records)
    markdown_count = sum(1 for row in records if row["is_markdown"])
    mojibake_count = sum(1 for row in records if row["is_mojibake_suspected"])
    source_line_count = sum(1 for row in records if row["has_source_line"])
    js_void_count = sum(1 for row in records if row["has_javascript_void_link"])
    web_residue_count = sum(1 for row in records if row["has_source_line"] or row["has_javascript_void_link"])
    duplicate_count = sum(1 for row in records if row["is_duplicate_version_suspected"])
    form_count = sum(1 for row in records if row["is_form_like_suspected"])

    return {
        "source_dir": str(source_dir),
        "manifest": str(output_dir / "manifest.jsonl"),
        "total_files": len(records),
        "markdown_files": markdown_count,
        "mojibake_suspected": mojibake_count,
        "web_residue_suspected": web_residue_count,
        "source_line_count": source_line_count,
        "javascript_void_count": js_void_count,
        "duplicate_version_suspected": duplicate_count,
        "form_like_suspected": form_count,
        "extension_counts": dict(sorted(ext_counts.items())),
    }


def write_summary(path: Path, summary: dict) -> None:
    lines = [
        "# 外汇文件 manifest 汇总",
        "",
        f"- 源目录：`{summary['source_dir']}`",
        f"- manifest：`{summary['manifest']}`",
        f"- 文件总数：{summary['total_files']}",
        f"- Markdown 文件数：{summary['markdown_files']}",
        f"- 乱码疑似数：{summary['mojibake_suspected']}",
        (
            "- 网页残留疑似数："
            f"{summary['web_residue_suspected']}（Source 行：{summary['source_line_count']}；"
            f"javascript:void(0)：{summary['javascript_void_count']}）"
        ),
        f"- 重复版本疑似数：{summary['duplicate_version_suspected']}",
        f"- 表单类疑似数：{summary['form_like_suspected']}",
        "",
        "## 类型分布",
        "",
        "| 类型 | 数量 |",
        "| --- | ---: |",
    ]
    for ext, count in summary["extension_counts"].items():
        lines.append(f"| {ext} | {count} |")
    lines.extend(
        [
            "",
            "## 字段说明",
            "",
            "- `is_mojibake_suspected`：对 Markdown 文本显式按 UTF-8 读取后，用常见乱码片段做启发式检测。",
            "- `has_source_line` / `has_javascript_void_link`：仅对 Markdown 文本检测网页抓取残留。",
            "- `is_duplicate_version_suspected`：按去扩展名、去末尾数字版本号后的文件名归组，同组多于 1 个即标记。",
            "- `is_form_like_suspected`：按文件名中的表单、申请、登记、开户等关键词启发式标记。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def build_manifest(source_dir: Path | str, output_dir: Path | str) -> dict:
    source_path = Path(source_dir)
    output_path = Path(output_dir)
    if not source_path.exists():
        raise FileNotFoundError(f"source directory does not exist: {source_path}")
    output_path.mkdir(parents=True, exist_ok=True)

    records = build_records(source_path)
    write_jsonl(output_path / "manifest.jsonl", records)
    summary = summarize(records, source_path, output_path)
    write_summary(output_path / "manifest_summary.md", summary)
    return summary


def main() -> None:
    default_output = Path(__file__).resolve().parents[1] / "manifest"
    parser = argparse.ArgumentParser(description="Build manifest for foreign exchange source files.")
    parser.add_argument("--source", default=r"E:\外汇文件", help="Source directory to scan.")
    parser.add_argument("--output", default=str(default_output), help="Output directory for manifest artifacts.")
    args = parser.parse_args()

    summary = build_manifest(Path(args.source), Path(args.output))
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
