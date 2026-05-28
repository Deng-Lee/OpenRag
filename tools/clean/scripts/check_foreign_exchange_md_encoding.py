from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Iterable


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


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def count_mojibake_markers(text: str) -> int:
    return sum(text.count(marker) for marker in MOJIBAKE_MARKERS)


def cjk_ratio(text: str) -> float:
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return 0.0
    cjk_count = sum(1 for ch in chars if "\u4e00" <= ch <= "\u9fff")
    return round(cjk_count / len(chars), 6)


def build_encoding_record(manifest_row: dict) -> dict:
    source_path = Path(manifest_row["source_path"])
    source_bytes = source_path.read_bytes()
    base = {
        "source_path": str(source_path),
        "file_name": manifest_row["file_name"],
        "read_encoding": "utf-8",
        "source_sha256": sha256_bytes(source_bytes),
        "utf8_read_ok": False,
        "text_sha256": None,
        "char_count": 0,
        "non_empty_line_count": 0,
        "mojibake_marker_count": 0,
        "replacement_char_count": 0,
        "cjk_char_ratio": 0.0,
        "needs_encoding_repair": True,
        "repair_attempted": False,
        "repair_status": "needs_review",
        "review_reason": None,
    }

    try:
        text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        base["review_reason"] = f"utf8_decode_error: {exc}"
        return base

    marker_count = count_mojibake_markers(text)
    replacement_count = text.count("\ufffd")
    reasons = []
    if marker_count > 0:
        reasons.append("mojibake_markers_detected")
    if replacement_count > 0:
        reasons.append("replacement_characters_detected")

    needs_repair = bool(reasons)
    return {
        **base,
        "utf8_read_ok": True,
        "text_sha256": sha256_text(text),
        "char_count": len(text),
        "non_empty_line_count": sum(1 for line in text.splitlines() if line.strip()),
        "mojibake_marker_count": marker_count,
        "replacement_char_count": replacement_count,
        "cjk_char_ratio": cjk_ratio(text),
        "needs_encoding_repair": needs_repair,
        "repair_status": "needs_review" if needs_repair else "not_needed",
        "review_reason": ";".join(reasons) if reasons else None,
    }


def summarize(records: list[dict], manifest_path: Path, output_dir: Path) -> dict:
    status_counts = Counter(row["repair_status"] for row in records)
    utf8_ok_count = sum(1 for row in records if row["utf8_read_ok"])
    needs_repair_count = sum(1 for row in records if row["needs_encoding_repair"])
    return {
        "manifest": str(manifest_path),
        "encoding_report": str(output_dir / "encoding_report.jsonl"),
        "markdown_files": len(records),
        "utf8_read_ok": utf8_ok_count,
        "needs_encoding_repair": needs_repair_count,
        "repair_attempted": sum(1 for row in records if row["repair_attempted"]),
        "status_counts": dict(sorted(status_counts.items())),
    }


def write_summary(path: Path, summary: dict, records: list[dict]) -> None:
    normal_examples = [row["file_name"] for row in records if not row["needs_encoding_repair"]][:5]
    review_examples = [row["file_name"] for row in records if row["needs_encoding_repair"]][:5]
    lines = [
        "# Markdown 编码读取校验汇总",
        "",
        f"- manifest：`{summary['manifest']}`",
        f"- encoding_report：`{summary['encoding_report']}`",
        f"- Markdown 校验文件数：{summary['markdown_files']}",
        f"- UTF-8 读取成功：{summary['utf8_read_ok']}",
        f"- 需要编码复核：{summary['needs_encoding_repair']}",
        f"- 本轮尝试修复：{summary['repair_attempted']}",
        "",
        "## 状态分布",
        "",
        "| 状态 | 数量 |",
        "| --- | ---: |",
    ]
    for status, count in summary["status_counts"].items():
        lines.append(f"| {status} | {count} |")
    lines.extend(["", "## 正常样例", ""])
    lines.extend(f"- {name}" for name in normal_examples)
    if not normal_examples:
        lines.append("- 无")
    lines.extend(["", "## 待复核样例", ""])
    lines.extend(f"- {name}" for name in review_examples)
    if not review_examples:
        lines.append("- 无")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def check_markdown_encoding(manifest_path: Path | str, output_dir: Path | str) -> dict:
    manifest = Path(manifest_path)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    rows = [row for row in read_jsonl(manifest) if row.get("is_markdown")]
    records = [build_encoding_record(row) for row in rows]
    write_jsonl(output / "encoding_report.jsonl", records)
    summary = summarize(records, manifest, output)
    write_summary(output / "encoding_summary.md", summary, records)
    return summary


def main() -> None:
    clean_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Check UTF-8 readability for foreign exchange Markdown files.")
    parser.add_argument(
        "--manifest",
        default=str(clean_root / "manifest" / "manifest.jsonl"),
        help="Manifest JSONL generated by build_foreign_exchange_manifest.py.",
    )
    parser.add_argument(
        "--output",
        default=str(clean_root / "encoding_check"),
        help="Output directory for encoding check artifacts.",
    )
    args = parser.parse_args()

    summary = check_markdown_encoding(Path(args.manifest), Path(args.output))
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
