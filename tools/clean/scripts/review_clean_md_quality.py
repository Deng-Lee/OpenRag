from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter
from pathlib import Path
from typing import Iterable


DEFAULT_SEED = 20260528
DEFAULT_SAMPLE_SIZE = 30
SOURCE_RE = re.compile(r"^\s*Source:\s*", re.IGNORECASE | re.MULTILINE)
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
ATTACHMENT_PROMPT_RE = re.compile(
    r"^\s*(?:附件|附件列表|相关附件|下载附件|附表)\s*[:：]\s*.*$"
)
ATTACHMENT_REFERENCE_RE = re.compile(r"^\s*(?:详见附件|请见附件|见附件)\s*[:：]?.*$")
DOWNLOAD_PROMPTS = (
    "相关稿件请在附件列表中点击下载后阅读",
    "请在附件列表中点击下载后阅读",
    "在附件列表中点击下载后阅读",
    "点击下载后阅读",
)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def normalize_title(line: str) -> str:
    stripped = line.strip()
    match = HEADING_RE.match(stripped)
    if match:
        stripped = match.group(1)
    return re.sub(r"\s+", "", stripped.strip("# \t"))


def clean_char_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def preview_text(text: str, max_chars: int = 1200) -> str:
    compact = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return compact[:max_chars]


def leakage_flags(text: str) -> list[str]:
    flags: list[str] = []
    if SOURCE_RE.search(text):
        flags.append("source_line_residue")
    if "javascript:void(0)" in text:
        flags.append("javascript_void_residue")

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(ATTACHMENT_PROMPT_RE.match(line) or ATTACHMENT_REFERENCE_RE.match(line) for line in lines):
        flags.append("attachment_prompt_residue")
    if any(any(prompt in line for prompt in DOWNLOAD_PROMPTS) for line in lines):
        flags.append("download_prompt_residue")

    non_empty = [normalize_title(line) for line in lines]
    if len(non_empty) >= 2 and non_empty[0] and non_empty[0] == non_empty[1]:
        flags.append("repeated_title_residue")
    return flags


def overdelete_flags(text: str, decision: dict) -> list[str]:
    flags: list[str] = []
    chars = clean_char_count(text)
    if chars == 0:
        flags.append("empty_clean_text")
    elif chars < 100:
        flags.append("very_short_clean_text")

    original = int(decision.get("original_char_count") or 0)
    clean = int(decision.get("clean_char_count") or len(text))
    if original > 0 and clean > 0 and clean / original < 0.35:
        flags.append("large_cleaning_ratio")

    removed_body_adjacent_lines = sum(
        int(decision.get(key) or 0)
        for key in (
            "removed_attachment_prompt_lines",
            "removed_download_prompt_lines",
            "removed_javascript_void_lines",
            "removed_repeated_title_lines",
        )
    )
    if removed_body_adjacent_lines >= 6:
        flags.append("many_removed_noise_lines")
    return flags


def sample_clean_files(clean_dir: Path, decisions: list[dict], sample_size: int, seed: int) -> list[Path]:
    kept_names = {row["file_name"] for row in decisions if row.get("decision") == "kept"}
    files = sorted(clean_dir.glob("*.md"), key=lambda path: path.name)
    if kept_names:
        files = [path for path in files if path.name in kept_names]
    rng = random.Random(seed)
    return rng.sample(files, k=min(sample_size, len(files)))


def build_review_rows(sample_files: list[Path], decisions_by_name: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    for idx, path in enumerate(sample_files, start=1):
        text = path.read_text(encoding="utf-8")
        decision = decisions_by_name.get(path.name, {})
        leaks = leakage_flags(text)
        overdelete = overdelete_flags(text, decision)
        rows.append(
            {
                "sample_id": f"clean-review-{idx:03d}",
                "file_name": path.name,
                "clean_path": str(path),
                "source_path": decision.get("source_path"),
                "clean_char_count": clean_char_count(text),
                "decision_clean_char_count": decision.get("clean_char_count"),
                "original_char_count": decision.get("original_char_count"),
                "removed_source_lines": decision.get("removed_source_lines", 0),
                "removed_javascript_void_lines": decision.get("removed_javascript_void_lines", 0),
                "removed_attachment_prompt_lines": decision.get("removed_attachment_prompt_lines", 0),
                "removed_download_prompt_lines": decision.get("removed_download_prompt_lines", 0),
                "removed_repeated_title_lines": decision.get("removed_repeated_title_lines", 0),
                "leakage_flags": leaks,
                "overdelete_review_flags": overdelete,
                "review_status": "needs_manual_review" if leaks or overdelete else "auto_pass",
                "clean_preview": preview_text(text),
            }
        )
    return rows


def write_review_markdown(path: Path, summary: dict, rows: list[dict]) -> None:
    lines = [
        "# clean_md 首轮抽样复验报告",
        "",
        f"- 随机种子：{summary['seed']}",
        f"- 目标样本数：{summary['target_sample_size']}",
        f"- 实际样本数：{summary['sample_count']}",
        f"- 需人工关注样本数：{summary['flagged_sample_count']}",
        "",
        "## 自动检查汇总",
        "",
        "| 指标 | 数量 |",
        "| --- | ---: |",
        f"| Source 残留 | {summary['leakage_flag_counts'].get('source_line_residue', 0)} |",
        f"| javascript:void(0) 残留 | {summary['leakage_flag_counts'].get('javascript_void_residue', 0)} |",
        f"| 附件提示残留 | {summary['leakage_flag_counts'].get('attachment_prompt_residue', 0)} |",
        f"| 下载提示残留 | {summary['leakage_flag_counts'].get('download_prompt_residue', 0)} |",
        f"| 重复标题残留 | {summary['leakage_flag_counts'].get('repeated_title_residue', 0)} |",
        "",
        "## 样本明细",
        "",
    ]
    for row in rows:
        lines.extend(
            [
                f"### {row['sample_id']} {row['file_name']}",
                "",
                f"- clean 路径：`{row['clean_path']}`",
                f"- 原始路径：`{row.get('source_path') or ''}`",
                f"- clean 字符数：{row['clean_char_count']}",
                f"- 漏删标记：`{', '.join(row['leakage_flags']) if row['leakage_flags'] else 'none'}`",
                (
                    "- 误删复验标记："
                    f"`{', '.join(row['overdelete_review_flags']) if row['overdelete_review_flags'] else 'none'}`"
                ),
                "",
                "```markdown",
                row["clean_preview"],
                "```",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def summarize(rows: list[dict], sample_size: int, seed: int, output_dir: Path) -> dict:
    leakage_counts = Counter(flag for row in rows for flag in row["leakage_flags"])
    overdelete_counts = Counter(flag for row in rows for flag in row["overdelete_review_flags"])
    flagged_count = sum(1 for row in rows if row["review_status"] == "needs_manual_review")
    return {
        "seed": seed,
        "target_sample_size": sample_size,
        "sample_count": len(rows),
        "flagged_sample_count": flagged_count,
        "leakage_flag_counts": dict(sorted(leakage_counts.items())),
        "overdelete_review_flag_counts": dict(sorted(overdelete_counts.items())),
        "review_jsonl": str(output_dir / "round1_clean_md_review.jsonl"),
        "review_markdown": str(output_dir / "round1_clean_md_review.md"),
    }


def build_clean_md_quality_sample(
    clean_dir: Path | str,
    decisions_path: Path | str,
    output_dir: Path | str,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = DEFAULT_SEED,
) -> dict:
    clean_path = Path(clean_dir)
    decisions_file = Path(decisions_path)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    decisions = read_jsonl(decisions_file)
    decisions_by_name = {row["file_name"]: row for row in decisions}
    sample_files = sample_clean_files(clean_path, decisions, sample_size, seed)
    rows = build_review_rows(sample_files, decisions_by_name)
    write_jsonl(output / "round1_clean_md_review.jsonl", rows)
    summary = summarize(rows, sample_size, seed, output)
    write_review_markdown(output / "round1_clean_md_review.md", summary, rows)
    (output / "round1_clean_md_review_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    return summary


def main() -> None:
    clean_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Sample clean Markdown files for manual quality review.")
    parser.add_argument("--clean-dir", default=str(clean_root / "clean_md"))
    parser.add_argument("--decisions", default=str(clean_root / "noise_report" / "cleaning_decisions.jsonl"))
    parser.add_argument("--output", default=str(clean_root / "quality_check"))
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    summary = build_clean_md_quality_sample(
        args.clean_dir,
        args.decisions,
        args.output,
        sample_size=args.sample_size,
        seed=args.seed,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
