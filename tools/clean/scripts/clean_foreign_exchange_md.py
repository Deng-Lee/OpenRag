from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Iterable


RULE_VERSION = "2026-05-28-round1"

SOURCE_RE = re.compile(r"^\s*Source:\s*", re.IGNORECASE)
SEPARATOR_RE = re.compile(r"^\s*[-=_*]{3,}\s*$")
PAGE_NUMBER_RE = re.compile(r"^\s*[-–—]?\s*(?:第\s*)?\d+\s*页\s*[-–—]?\s*$")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
ATTACHMENT_PROMPT_RE = re.compile(
    r"^\s*(?:附件|附件列表|相关附件|下载附件|附表)\s*[:：]\s*.*$"
)
ATTACHMENT_REFERENCE_RE = re.compile(r"^\s*(?:详见附件|请见附件|见附件)\s*[:：]?.*$")
NUMBERED_ATTACHMENT_TITLE_RE = re.compile(r"^\s*(?:[-*]\s*)?(?:\d+|[一二三四五六七八九十]+)[.、．]\s*\S+")

DOWNLOAD_PROMPTS = (
    "相关稿件请在附件列表中点击下载后阅读",
    "请在附件列表中点击下载后阅读",
    "在附件列表中点击下载后阅读",
    "点击下载后阅读",
)

BRIEF_NOTICE_KEYWORDS = (
    "发布",
    "印发",
    "修订",
    "公告",
    "通知",
)

SUBSTANTIVE_MARKERS = (
    "一、",
    "二、",
    "三、",
    "四、",
    "五、",
    "第一条",
    "第二条",
    "第三条",
    "如下：",
    "如下:",
    "业务流程",
    "操作步骤",
    "申请材料",
    "办理流程",
    "交易规则",
    "管理办法",
    "实施细则",
    "具体",
)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_clean_dir(path: Path) -> None:
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


def has_download_prompt(line: str) -> bool:
    return any(prompt in line for prompt in DOWNLOAD_PROMPTS)


def is_attachment_prompt(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return bool(ATTACHMENT_PROMPT_RE.match(stripped) or ATTACHMENT_REFERENCE_RE.match(stripped))


def is_numbered_attachment_title(line: str) -> bool:
    return bool(NUMBERED_ATTACHMENT_TITLE_RE.match(line.strip()))


def is_page_or_separator(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return bool(SEPARATOR_RE.match(stripped) or PAGE_NUMBER_RE.match(stripped))


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
    non_empty_indexes = [idx for idx, line in enumerate(lines) if line.strip()]
    if len(non_empty_indexes) < 2:
        return lines, 0

    first_idx, second_idx = non_empty_indexes[0], non_empty_indexes[1]
    first_title = normalize_title(lines[first_idx])
    second_title = normalize_title(lines[second_idx])
    if first_title and first_title == second_title:
        return [line for idx, line in enumerate(lines) if idx != second_idx], 1
    return lines, 0


def clean_markdown_text(text: str) -> tuple[str, dict]:
    stats = {
        "removed_source_lines": 0,
        "removed_javascript_void_lines": 0,
        "removed_attachment_prompt_lines": 0,
        "removed_download_prompt_lines": 0,
        "removed_page_or_separator_lines": 0,
        "removed_repeated_title_lines": 0,
    }
    kept_lines: list[str] = []
    in_attachment_title_block = False
    for line in text.splitlines():
        if in_attachment_title_block:
            if not line.strip():
                continue
            if is_numbered_attachment_title(line):
                stats["removed_attachment_prompt_lines"] += 1
                continue
            in_attachment_title_block = False

        if SOURCE_RE.match(line):
            stats["removed_source_lines"] += 1
            continue
        if "javascript:void(0)" in line:
            stats["removed_javascript_void_lines"] += 1
            continue
        if has_download_prompt(line):
            stats["removed_download_prompt_lines"] += 1
            continue
        if is_attachment_prompt(line):
            stats["removed_attachment_prompt_lines"] += 1
            in_attachment_title_block = True
            continue
        if is_page_or_separator(line):
            stats["removed_page_or_separator_lines"] += 1
            continue
        kept_lines.append(line.rstrip())

    kept_lines = collapse_blank_lines(kept_lines)
    kept_lines, repeated_count = remove_repeated_leading_title(kept_lines)
    stats["removed_repeated_title_lines"] = repeated_count
    kept_lines = collapse_blank_lines(kept_lines)
    return ("\n".join(kept_lines) + "\n") if kept_lines else "", stats


def effective_lines(clean_text: str) -> list[str]:
    values = []
    for line in clean_text.splitlines():
        normalized = normalize_title(line)
        if normalized:
            values.append(normalized)
    return values


def is_download_only_page(clean_text: str, stats: dict, manifest_row: dict) -> bool:
    had_attachment_or_download_noise = (
        stats["removed_javascript_void_lines"] > 0
        or stats["removed_attachment_prompt_lines"] > 0
        or stats["removed_download_prompt_lines"] > 0
        or manifest_row.get("has_javascript_void_link", False)
    )
    if not had_attachment_or_download_noise:
        return False

    lines = effective_lines(clean_text)
    unique_lines = set(lines)
    clean_chars = len("".join(lines))
    if not lines:
        return True
    if len(unique_lines) <= 1:
        return True
    return clean_chars <= 80 and stats["removed_download_prompt_lines"] > 0


def is_brief_publish_notice(clean_text: str, stats: dict, manifest_row: dict) -> bool:
    had_attachment_noise = (
        stats["removed_javascript_void_lines"] > 0
        or stats["removed_attachment_prompt_lines"] > 0
        or manifest_row.get("has_javascript_void_link", False)
    )
    if not had_attachment_noise:
        return False

    compact = re.sub(r"\s+", "", clean_text)
    if len(compact) > 600:
        return False
    if not any(keyword in compact for keyword in BRIEF_NOTICE_KEYWORDS):
        return False
    if any(marker in compact for marker in SUBSTANTIVE_MARKERS):
        return False

    non_heading_lines = [
        line.strip()
        for line in clean_text.splitlines()
        if line.strip() and not HEADING_RE.match(line.strip())
    ]
    return len(non_heading_lines) <= 8


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
        "file_name": row["file_name"],
        "source_path": row["source_path"],
        "decision": decision,
        "reason": reason,
        "rule_version": RULE_VERSION,
        "clean_path": str(clean_path) if clean_path else None,
        "kept_by": kept_by,
        "clean_sha256": sha256_text(clean_text) if clean_text else None,
        "original_char_count": int(row.get("char_count", 0)),
        "clean_char_count": len(clean_text),
        **stats,
    }


def write_report(path: Path, summary: dict, decisions: list[dict]) -> None:
    counts = Counter(row["decision"] for row in decisions)
    removed_totals = Counter()
    for row in decisions:
        for key, value in row.items():
            if key.startswith("removed_") and key.endswith("_lines"):
                removed_totals[key] += int(value)

    lines = [
        "# 外汇文件 Markdown 规则清洗报告",
        "",
        f"- 规则版本：`{RULE_VERSION}`",
        f"- Markdown 总数：{summary['total_markdown']}",
        f"- 保留 clean Markdown：{summary['kept']}",
        f"- 删除或分流：{summary['dropped_or_review']}",
        f"- clean 输出目录：`{summary['clean_dir']}`",
        f"- 决策明细：`{summary['decision_report']}`",
        "",
        "## 决策分布",
        "",
        "| 决策 | 数量 |",
        "| --- | ---: |",
    ]
    for decision, count in sorted(counts.items()):
        lines.append(f"| `{decision}` | {count} |")

    lines.extend(["", "## 删除规则命中行数", "", "| 规则计数 | 行数 |", "| --- | ---: |"])
    for key, count in sorted(removed_totals.items()):
        lines.append(f"| `{key}` | {count} |")

    dropped_examples = [row for row in decisions if row["decision"] != "kept"][:20]
    lines.extend(["", "## 删除或分流样例", ""])
    if dropped_examples:
        for row in dropped_examples:
            lines.append(f"- `{row['file_name']}`：`{row['decision']}`，{row['reason']}")
    else:
        lines.append("- 无")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def merge_manifest_and_encoding(manifest_path: Path, encoding_path: Path) -> list[dict]:
    manifest_rows = [row for row in read_jsonl(manifest_path) if row.get("is_markdown")]
    encoding_rows = {row["source_path"]: row for row in read_jsonl(encoding_path)}
    merged = []
    for row in manifest_rows:
        enc = encoding_rows.get(row["source_path"], {})
        merged.append({**row, **enc, "file_name": row["file_name"], "source_path": row["source_path"]})
    return sorted(merged, key=lambda item: item["file_name"])


def clean_markdown_corpus(
    manifest_path: Path | str,
    encoding_report_path: Path | str,
    output_root: Path | str,
) -> dict:
    manifest = Path(manifest_path)
    encoding = Path(encoding_report_path)
    output = Path(output_root)
    clean_dir = output / "clean_md"
    report_dir = output / "noise_report"
    needs_review_dir = output / "needs_review"
    ensure_clean_dir(clean_dir)
    ensure_clean_dir(report_dir)
    ensure_clean_dir(needs_review_dir)

    rows = merge_manifest_and_encoding(manifest, encoding)
    decisions: list[dict] = []
    seen_hashes: dict[str, dict] = {}

    for row in rows:
        stats = {
            "removed_source_lines": 0,
            "removed_javascript_void_lines": 0,
            "removed_attachment_prompt_lines": 0,
            "removed_download_prompt_lines": 0,
            "removed_page_or_separator_lines": 0,
            "removed_repeated_title_lines": 0,
        }

        if not row.get("utf8_read_ok", True) or row.get("needs_encoding_repair", False):
            source_text = Path(row["source_path"]).read_text(encoding="utf-8", errors="replace")
            review_path = needs_review_dir / row["file_name"]
            review_path.write_text(source_text, encoding="utf-8", newline="\n")
            decisions.append(
                build_decision(
                    row,
                    "needs_review_encoding",
                    "encoding check did not pass clean UTF-8 criteria",
                    "",
                    stats,
                    clean_path=review_path,
                )
            )
            continue

        source_text = Path(row["source_path"]).read_text(encoding="utf-8")
        clean_text, stats = clean_markdown_text(source_text)

        if is_download_only_page(clean_text, stats, row):
            decisions.append(
                build_decision(
                    row,
                    "dropped_download_only_page",
                    "remaining text is only title or very short attachment download notice",
                    clean_text,
                    stats,
                )
            )
            continue

        if is_brief_publish_notice(clean_text, stats, row):
            decisions.append(
                build_decision(
                    row,
                    "dropped_brief_publish_notice",
                    "brief announcement only points readers to attachment content",
                    clean_text,
                    stats,
                )
            )
            continue

        clean_hash = sha256_text(clean_text)
        if clean_hash in seen_hashes:
            keeper = seen_hashes[clean_hash]
            decisions.append(
                build_decision(
                    row,
                    "dropped_exact_duplicate",
                    "cleaned content is identical to an earlier kept Markdown",
                    clean_text,
                    stats,
                    kept_by=keeper["file_name"],
                )
            )
            continue

        clean_path = clean_dir / row["file_name"]
        clean_path.write_text(clean_text, encoding="utf-8", newline="\n")
        seen_hashes[clean_hash] = {"file_name": row["file_name"], "clean_path": str(clean_path)}
        decisions.append(build_decision(row, "kept", "contains retained body text", clean_text, stats, clean_path))

    decision_path = report_dir / "cleaning_decisions.jsonl"
    write_jsonl(decision_path, decisions)
    decision_counts = Counter(row["decision"] for row in decisions)
    summary = {
        "manifest": str(manifest),
        "encoding_report": str(encoding),
        "clean_dir": str(clean_dir),
        "decision_report": str(decision_path),
        "noise_report": str(report_dir / "noise_report.md"),
        "total_markdown": len(rows),
        "kept": decision_counts.get("kept", 0),
        "dropped_or_review": len(rows) - decision_counts.get("kept", 0),
        "decision_counts": dict(sorted(decision_counts.items())),
    }
    write_report(report_dir / "noise_report.md", summary, decisions)
    return summary


def main() -> None:
    clean_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Clean foreign exchange Markdown files with confirmed noise rules.")
    parser.add_argument("--manifest", default=str(clean_root / "manifest" / "manifest.jsonl"))
    parser.add_argument("--encoding-report", default=str(clean_root / "encoding_check" / "encoding_report.jsonl"))
    parser.add_argument("--output-root", default=str(clean_root))
    args = parser.parse_args()

    summary = clean_markdown_corpus(args.manifest, args.encoding_report, args.output_root)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
