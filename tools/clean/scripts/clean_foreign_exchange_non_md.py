from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Iterable


RULE_VERSION = "2026-05-28-non-md-round1"
PAGE_NUMBER_RE = re.compile(r"^\s*[-–—]?\s*(?:第\s*)?\d+\s*页\s*[-–—]?\s*$")
SEPARATOR_RE = re.compile(r"^\s*[-=_*]{3,}\s*$")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_output_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_file():
            child.unlink()


def normalize_title(line: str) -> str:
    stripped = line.strip()
    match = HEADING_RE.match(stripped)
    if match:
        stripped = match.group(1)
    return re.sub(r"\s+", "", stripped.strip("# \t"))


def is_page_or_separator(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped and (PAGE_NUMBER_RE.match(stripped) or SEPARATOR_RE.match(stripped)))


def collapse_blank_lines(lines: list[str]) -> list[str]:
    collapsed: list[str] = []
    previous_blank = False
    for line in lines:
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        collapsed.append("" if is_blank else line.rstrip())
        previous_blank = is_blank
    while collapsed and not collapsed[0].strip():
        collapsed.pop(0)
    while collapsed and not collapsed[-1].strip():
        collapsed.pop()
    return collapsed


def remove_repeated_leading_title(lines: list[str]) -> tuple[list[str], int]:
    non_empty = [idx for idx, line in enumerate(lines) if line.strip()]
    if len(non_empty) < 2:
        return lines, 0
    first, second = non_empty[0], non_empty[1]
    if normalize_title(lines[first]) and normalize_title(lines[first]) == normalize_title(lines[second]):
        return [line for idx, line in enumerate(lines) if idx != second], 1
    return lines, 0


def light_clean_text(text: str) -> tuple[str, dict]:
    stats = {
        "removed_page_or_separator_lines": 0,
        "removed_repeated_title_lines": 0,
        "removed_form_feed_count": text.count("\f"),
    }
    normalized = text.replace("\f", "\n")
    kept: list[str] = []
    for line in normalized.splitlines():
        if is_page_or_separator(line):
            stats["removed_page_or_separator_lines"] += 1
            continue
        kept.append(line.rstrip())
    kept = collapse_blank_lines(kept)
    kept, repeated = remove_repeated_leading_title(kept)
    stats["removed_repeated_title_lines"] = repeated
    kept = collapse_blank_lines(kept)
    return ("\n".join(kept) + "\n") if kept else "", stats


def compact_char_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def output_name_for(file_name: str, extension: str | None, used_names: set[str]) -> str:
    stem = Path(file_name).stem
    ext_suffix = (extension or Path(file_name).suffix).lstrip(".") or "file"
    candidate = f"{stem}.md"
    if candidate in used_names:
        candidate = f"{stem}__{ext_suffix}.md"
    counter = 2
    while candidate in used_names:
        candidate = f"{stem}__{ext_suffix}_{counter}.md"
        counter += 1
    used_names.add(candidate)
    return candidate


def output_path_for(directory: Path, row: dict, used_names_by_dir: dict[Path, set[str]]) -> Path:
    return directory / output_name_for(
        row["file_name"],
        row.get("extension"),
        used_names_by_dir.setdefault(directory, set()),
    )


def markdown_hashes(markdown_clean_dir: Path | None) -> dict[str, str]:
    if not markdown_clean_dir or not markdown_clean_dir.exists():
        return {}
    hashes: dict[str, str] = {}
    for path in sorted(markdown_clean_dir.glob("*.md"), key=lambda p: p.name):
        text = path.read_text(encoding="utf-8")
        hashes[sha256_text(text)] = path.name
    return hashes


def build_decision(
    row: dict,
    decision: str,
    reason: str,
    clean_text: str,
    stats: dict,
    clean_path: Path | None = None,
    kept_by: str | None = None,
) -> dict:
    return {
        "source_path": row.get("source_path"),
        "file_name": row["file_name"],
        "extension": row.get("extension"),
        "decision": decision,
        "reason": reason,
        "rule_version": RULE_VERSION,
        "clean_path": str(clean_path) if clean_path else None,
        "kept_by": kept_by,
        "parse_status": row.get("parse_status"),
        "clean_sha256": sha256_text(clean_text) if clean_text else None,
        "original_char_count": int(row.get("char_count") or 0),
        "clean_char_count": len(clean_text),
        "compact_char_count": compact_char_count(clean_text),
        "table_count": int(row.get("table_count") or 0),
        "worksheet_count": int(row.get("worksheet_count") or 0),
        "is_form_like_suspected": bool(row.get("is_form_like_suspected", False)),
        **stats,
    }


def write_review_stub(path: Path, row: dict, reason: str) -> None:
    lines = [
        f"# {Path(row['file_name']).stem}",
        "",
        f"- 原始路径：`{row.get('source_path') or ''}`",
        f"- 扩展名：`{row.get('extension') or ''}`",
        f"- 进入待复核原因：{reason}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def write_summary(path: Path, summary: dict) -> None:
    lines = [
        "# 非 Markdown 文档轻量清洗汇总",
        "",
        f"- parse_report：`{summary['parse_report']}`",
        f"- 总记录数：{summary['total_records']}",
        "",
        "## 决策分布",
        "",
        "| 决策 | 数量 |",
        "| --- | ---: |",
    ]
    for decision, count in summary["decision_counts"].items():
        lines.append(f"| `{decision}` | {count} |")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def clean_non_markdown_corpus(
    parse_report_path: Path | str,
    output_dir: Path | str,
    markdown_clean_dir: Path | str | None = None,
) -> dict:
    parse_report = Path(parse_report_path)
    output = Path(output_dir)
    clean_docs = output / "clean_docs"
    forms = output / "forms"
    needs_review = output / "needs_review"
    output.mkdir(parents=True, exist_ok=True)
    for directory in (clean_docs, forms, needs_review):
        clean_output_dir(directory)
    used_names_by_dir: dict[Path, set[str]] = {clean_docs: set(), forms: set(), needs_review: set()}

    md_hashes = markdown_hashes(Path(markdown_clean_dir) if markdown_clean_dir else None)
    seen_non_md_hashes: dict[str, str] = {}
    decisions: list[dict] = []
    records = read_jsonl(parse_report)

    for row in records:
        stats = {"removed_page_or_separator_lines": 0, "removed_repeated_title_lines": 0, "removed_form_feed_count": 0}
        if row.get("parse_status") != "parsed":
            review_path = output_path_for(needs_review, row, used_names_by_dir)
            reason = row.get("review_reason") or "parse_not_successful"
            write_review_stub(review_path, row, reason)
            decisions.append(build_decision(row, "needs_review_parse", reason, "", stats, review_path))
            continue

        parsed_path = Path(row.get("parsed_path") or "")
        if not parsed_path.exists():
            review_path = output_path_for(needs_review, row, used_names_by_dir)
            write_review_stub(review_path, row, "parsed_text_missing")
            decisions.append(build_decision(row, "needs_review_missing_text", "parsed_text_missing", "", stats, review_path))
            continue

        source_text = parsed_path.read_text(encoding="utf-8")
        clean_text, stats = light_clean_text(source_text)
        if compact_char_count(clean_text) < 20:
            review_path = output_path_for(needs_review, row, used_names_by_dir)
            review_path.write_text(clean_text or f"# {Path(row['file_name']).stem}\n\n", encoding="utf-8", newline="\n")
            decisions.append(build_decision(row, "needs_review_short_text", "clean_text_too_short", clean_text, stats, review_path))
            continue

        clean_hash = sha256_text(clean_text)
        if clean_hash in md_hashes:
            decisions.append(
                build_decision(
                    row,
                    "dropped_duplicate_markdown",
                    "cleaned non-Markdown text duplicates existing Markdown clean text",
                    clean_text,
                    stats,
                    kept_by=md_hashes[clean_hash],
                )
            )
            continue
        if clean_hash in seen_non_md_hashes:
            decisions.append(
                build_decision(
                    row,
                    "dropped_exact_duplicate",
                    "cleaned non-Markdown text duplicates an earlier kept non-Markdown text",
                    clean_text,
                    stats,
                    kept_by=seen_non_md_hashes[clean_hash],
                )
            )
            continue

        target_dir = forms if row.get("is_form_like_suspected") or row.get("extension") in {".xls", ".xlsx"} else clean_docs
        decision = "routed_form" if target_dir == forms else "kept"
        out_path = output_path_for(target_dir, row, used_names_by_dir)
        out_path.write_text(clean_text, encoding="utf-8", newline="\n")
        seen_non_md_hashes[clean_hash] = row["file_name"]
        decisions.append(build_decision(row, decision, "light_normalized", clean_text, stats, out_path))

    decision_path = output / "non_md_cleaning_decisions.jsonl"
    write_jsonl(decision_path, decisions)
    decision_counts = Counter(row["decision"] for row in decisions)
    summary = {
        "parse_report": str(parse_report),
        "decision_report": str(decision_path),
        "summary_report": str(output / "non_md_summary.md"),
        "total_records": len(records),
        "decision_counts": dict(sorted(decision_counts.items())),
        "clean_docs_count": decision_counts.get("kept", 0),
        "forms_count": decision_counts.get("routed_form", 0),
        "needs_review_count": sum(count for decision, count in decision_counts.items() if decision.startswith("needs_review")),
    }
    write_summary(output / "non_md_summary.md", summary)
    return summary


def main() -> None:
    clean_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Light-clean extracted non-Markdown foreign exchange documents.")
    parser.add_argument("--parse-report", default=str(clean_root / "non_md_parse" / "parse_report.jsonl"))
    parser.add_argument("--output", default=str(clean_root / "non_md_clean"))
    parser.add_argument("--markdown-clean-dir", default=str(clean_root / "clean_md"))
    args = parser.parse_args()

    summary = clean_non_markdown_corpus(args.parse_report, args.output, markdown_clean_dir=args.markdown_clean_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
