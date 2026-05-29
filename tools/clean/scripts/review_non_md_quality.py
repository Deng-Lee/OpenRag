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
MOJIBAKE_MARKERS = ("鍏", "涓", "骞", "佸", "鐢", "閾", "鈥", "锟")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def compact_char_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def preview_text(text: str, max_chars: int = 1200) -> str:
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()[:max_chars]


def quality_flags(text: str, decision: dict) -> list[str]:
    flags: list[str] = []
    compact = re.sub(r"\s+", "", text)
    if not compact:
        flags.append("empty_clean_text")
    elif len(compact) < 50:
        flags.append("very_short_clean_text")
    if any(marker in text for marker in MOJIBAKE_MARKERS):
        flags.append("mojibake_marker_residue")
    if int(decision.get("table_count") or 0) > 0 and "|" not in text:
        flags.append("table_structure_maybe_lost")
    if int(decision.get("worksheet_count") or 0) > 0 and "## Sheet:" not in text:
        flags.append("worksheet_marker_missing")
    repeated_short_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and len(line.strip()) <= 30
    ]
    counts = Counter(repeated_short_lines)
    if any(count >= 4 for count in counts.values()):
        flags.append("possible_repeated_header_footer")
    return flags


def sample_decisions(decisions: list[dict], sample_size: int, seed: int) -> list[dict]:
    candidates = [
        row
        for row in decisions
        if row.get("decision") in {"kept", "routed_form"} and row.get("clean_path") and Path(row["clean_path"]).exists()
    ]
    rng = random.Random(seed)
    return rng.sample(candidates, k=min(sample_size, len(candidates)))


def build_rows(sampled: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for idx, decision in enumerate(sampled, start=1):
        path = Path(decision["clean_path"])
        text = path.read_text(encoding="utf-8")
        flags = quality_flags(text, decision)
        rows.append(
            {
                "sample_id": f"non-md-review-{idx:03d}",
                "file_name": decision["file_name"],
                "decision": decision["decision"],
                "source_path": decision.get("source_path"),
                "clean_path": str(path),
                "clean_char_count": compact_char_count(text),
                "table_count": int(decision.get("table_count") or 0),
                "worksheet_count": int(decision.get("worksheet_count") or 0),
                "quality_flags": flags,
                "review_status": "needs_manual_review" if flags else "auto_pass",
                "clean_preview": preview_text(text),
            }
        )
    return rows


def write_review_markdown(path: Path, summary: dict, rows: list[dict]) -> None:
    lines = [
        "# 非 Markdown clean 首轮抽样复验报告",
        "",
        f"- 随机种子：{summary['seed']}",
        f"- 目标样本数：{summary['target_sample_size']}",
        f"- 实际样本数：{summary['sample_count']}",
        f"- 需人工关注样本数：{summary['flagged_sample_count']}",
        "",
        "## 质量标记分布",
        "",
        "| 标记 | 数量 |",
        "| --- | ---: |",
    ]
    for flag, count in summary["quality_flag_counts"].items():
        lines.append(f"| `{flag}` | {count} |")
    if not summary["quality_flag_counts"]:
        lines.append("| 无 | 0 |")

    lines.extend(["", "## 样本明细", ""])
    for row in rows:
        lines.extend(
            [
                f"### {row['sample_id']} {row['file_name']}",
                "",
                f"- 决策：`{row['decision']}`",
                f"- clean 路径：`{row['clean_path']}`",
                f"- 原始路径：`{row.get('source_path') or ''}`",
                f"- 质量标记：`{', '.join(row['quality_flags']) if row['quality_flags'] else 'none'}`",
                "",
                "```markdown",
                row["clean_preview"],
                "```",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def summarize(rows: list[dict], sample_size: int, seed: int, output_dir: Path) -> dict:
    flag_counts = Counter(flag for row in rows for flag in row["quality_flags"])
    return {
        "seed": seed,
        "target_sample_size": sample_size,
        "sample_count": len(rows),
        "flagged_sample_count": sum(1 for row in rows if row["quality_flags"]),
        "quality_flag_counts": dict(sorted(flag_counts.items())),
        "review_jsonl": str(output_dir / "round1_non_md_review.jsonl"),
        "review_markdown": str(output_dir / "round1_non_md_review.md"),
    }


def build_non_md_quality_sample(
    decisions_path: Path | str,
    output_dir: Path | str,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = DEFAULT_SEED,
) -> dict:
    decisions_file = Path(decisions_path)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    decisions = read_jsonl(decisions_file)
    rows = build_rows(sample_decisions(decisions, sample_size, seed))
    write_jsonl(output / "round1_non_md_review.jsonl", rows)
    summary = summarize(rows, sample_size, seed, output)
    write_review_markdown(output / "round1_non_md_review.md", summary, rows)
    (output / "round1_non_md_review_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
        newline="\n",
    )
    return summary


def main() -> None:
    clean_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Sample non-Markdown clean outputs for quality review.")
    parser.add_argument("--decisions", default=str(clean_root / "non_md_clean" / "non_md_cleaning_decisions.jsonl"))
    parser.add_argument("--output", default=str(clean_root / "non_md_quality"))
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    summary = build_non_md_quality_sample(args.decisions, args.output, args.sample_size, args.seed)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
