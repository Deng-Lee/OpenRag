from __future__ import annotations

import argparse
import json
import random
from collections import Counter
from pathlib import Path
from typing import Iterable


DEFAULT_SEED = 20260528
DEFAULT_SAMPLE_SIZE = 40
BASE_LENGTH_TARGETS = {
    "short": 16,
    "medium": 10,
    "long": None,
    "very_long": None,
}
RISK_TARGETS = {
    "javascript_void": 8,
    "duplicate_version": 6,
    "form_like": 5,
    "possible_shell_page": 5,
}
REVIEW_QUESTIONS = [
    "Source 行是否只转元数据，不进入正文？",
    "附件链接行应删除、保留附件名，还是转附件元数据？",
    "该文档是否属于仅提示下载附件的空壳页？",
    "如果属于重复版本，应保留哪一份？",
    "表单类文件是否进入 RAG 正文库，还是单独分流？",
    "是否发现新的噪声模式？",
]


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def length_bucket(char_count: int) -> str:
    if char_count <= 800:
        return "short"
    if char_count <= 3000:
        return "medium"
    if char_count <= 8000:
        return "long"
    return "very_long"


def is_possible_shell_page(row: dict) -> bool:
    return (
        row["length_bucket"] == "short"
        and row.get("has_javascript_void_link", False)
        and row.get("char_count", 0) <= 800
    )


def merge_candidates(manifest_path: Path, encoding_path: Path) -> list[dict]:
    manifest_rows = [row for row in read_jsonl(manifest_path) if row.get("is_markdown")]
    encoding_rows = {row["source_path"]: row for row in read_jsonl(encoding_path)}
    candidates: list[dict] = []
    for row in manifest_rows:
        enc = encoding_rows.get(row["source_path"])
        if not enc:
            continue
        merged = {
            **row,
            "char_count": int(enc.get("char_count", 0)),
            "utf8_read_ok": bool(enc.get("utf8_read_ok")),
            "needs_encoding_repair": bool(enc.get("needs_encoding_repair")),
        }
        merged["length_bucket"] = length_bucket(merged["char_count"])
        merged["risk_layers"] = []
        if merged.get("has_javascript_void_link"):
            merged["risk_layers"].append("javascript_void")
        if merged.get("is_duplicate_version_suspected"):
            merged["risk_layers"].append("duplicate_version")
        if merged.get("is_form_like_suspected"):
            merged["risk_layers"].append("form_like")
        if is_possible_shell_page(merged):
            merged["risk_layers"].append("possible_shell_page")
        candidates.append(merged)
    return candidates


def sample_from(rows: list[dict], count: int, rng: random.Random, selected: dict[str, dict]) -> None:
    available = [row for row in rows if row["source_path"] not in selected]
    if count <= 0 or not available:
        return
    chosen = rng.sample(available, k=min(count, len(available)))
    for row in chosen:
        selected[row["source_path"]] = row


def sample_by_plan(candidates: list[dict], sample_size: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    selected: dict[str, dict] = {}

    for bucket, target in BASE_LENGTH_TARGETS.items():
        bucket_rows = [row for row in candidates if row["length_bucket"] == bucket]
        if target is None:
            sample_from(bucket_rows, len(bucket_rows), rng, selected)
        else:
            sample_from(bucket_rows, target, rng, selected)

    risk_filters = {
        "javascript_void": lambda row: row.get("has_javascript_void_link", False),
        "duplicate_version": lambda row: row.get("is_duplicate_version_suspected", False),
        "form_like": lambda row: row.get("is_form_like_suspected", False),
        "possible_shell_page": is_possible_shell_page,
    }
    for risk, target in RISK_TARGETS.items():
        current = sum(1 for row in selected.values() if risk_filters[risk](row))
        sample_from([row for row in candidates if risk_filters[risk](row)], target - current, rng, selected)

    if len(selected) < sample_size:
        priority_order = {"medium": 0, "short": 1, "long": 2, "very_long": 3}
        fill_candidates = sorted(
            (row for row in candidates if row["source_path"] not in selected),
            key=lambda row: (priority_order.get(row["length_bucket"], 9), row["file_name"]),
        )
        sample_from(fill_candidates, sample_size - len(selected), rng, selected)

    rows = list(selected.values())
    if len(rows) > sample_size:
        priority = {
            "very_long": 0,
            "long": 1,
            "medium": 4,
            "short": 5,
        }

        def trim_key(row: dict) -> tuple[int, int, int, int, str]:
            return (
                priority.get(row["length_bucket"], 9),
                0 if row.get("is_form_like_suspected") else 1,
                0 if row.get("is_duplicate_version_suspected") else 1,
                0 if row.get("has_javascript_void_link") else 1,
                row["file_name"],
            )

        rows = sorted(rows, key=trim_key)[:sample_size]

    return assign_sample_indexes(rows)


def assign_sample_indexes(rows: list[dict]) -> list[dict]:
    ordered = sorted(rows, key=lambda row: (row["length_bucket"], row["file_name"]))
    sample_rows = []
    for idx, row in enumerate(ordered, start=1):
        sample_rows.append(
            {
                "sample_id": f"round1-{idx:03d}",
                "source_path": row["source_path"],
                "file_name": row["file_name"],
                "char_count": row["char_count"],
                "length_bucket": row["length_bucket"],
                "has_source_line": row.get("has_source_line", False),
                "has_javascript_void_link": row.get("has_javascript_void_link", False),
                "is_duplicate_version_suspected": row.get("is_duplicate_version_suspected", False),
                "duplicate_group_key": row.get("duplicate_group_key"),
                "is_form_like_suspected": row.get("is_form_like_suspected", False),
                "risk_layers": row.get("risk_layers", []),
                "sample_reason": sample_reason(row),
            }
        )
    return sample_rows


def sample_reason(row: dict) -> str:
    reasons = [f"length:{row['length_bucket']}"]
    if row.get("has_javascript_void_link"):
        reasons.append("javascript_void")
    if row.get("is_duplicate_version_suspected"):
        reasons.append("duplicate_version")
    if row.get("is_form_like_suspected"):
        reasons.append("form_like")
    if is_possible_shell_page(row):
        reasons.append("possible_shell_page")
    return ",".join(reasons)


def preview_text(path: Path, max_chars: int = 1200) -> str:
    text = path.read_text(encoding="utf-8")
    compact_lines = [line.rstrip() for line in text.splitlines()]
    compact = "\n".join(compact_lines).strip()
    return compact[:max_chars]


def write_review(path: Path, rows: list[dict]) -> None:
    lines = [
        "# 外汇文件 Markdown 首轮抽样审阅包",
        "",
        "请逐篇判断网页残留、附件链接、空壳页、重复版本和表单类分流策略。",
        "",
    ]
    for row in rows:
        lines.extend(
            [
                f"## {row['sample_id']} · {row['file_name']}",
                "",
                f"- 来源路径：`{row['source_path']}`",
                f"- 字符数：{row['char_count']}",
                f"- 长度层：`{row['length_bucket']}`",
                f"- 抽样原因：`{row['sample_reason']}`",
                f"- 重复组：`{row.get('duplicate_group_key') or ''}`",
                "",
                "### 待判断项",
                "",
            ]
        )
        lines.extend(f"- [ ] {question}" for question in REVIEW_QUESTIONS)
        lines.extend(
            [
                "",
                "### 正文预览",
                "",
                "```markdown",
                preview_text(Path(row["source_path"])),
                "```",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def summarize(candidates: list[dict], samples: list[dict], sample_size: int, seed: int) -> dict:
    candidate_buckets = Counter(row["length_bucket"] for row in candidates)
    sample_buckets = Counter(row["length_bucket"] for row in samples)
    risk_counts = {
        "javascript_void": sum(1 for row in samples if row["has_javascript_void_link"]),
        "duplicate_version": sum(1 for row in samples if row["is_duplicate_version_suspected"]),
        "form_like": sum(1 for row in samples if row["is_form_like_suspected"]),
        "possible_shell_page": sum(1 for row in samples if "possible_shell_page" in row["risk_layers"]),
    }
    return {
        "seed": seed,
        "target_sample_size": sample_size,
        "candidate_count": len(candidates),
        "sample_count": len(samples),
        "candidate_length_buckets": dict(sorted(candidate_buckets.items())),
        "sample_length_buckets": dict(sorted(sample_buckets.items())),
        "sample_risk_counts": risk_counts,
    }


def write_summary(path: Path, summary: dict) -> None:
    lines = [
        "# 外汇文件 Markdown 首轮分层抽样汇总",
        "",
        f"- 随机种子：{summary['seed']}",
        f"- 候选 Markdown 数：{summary['candidate_count']}",
        f"- 目标样本数：{summary['target_sample_size']}",
        f"- 实际样本数：{summary['sample_count']}",
        "",
        "## 长度层覆盖",
        "",
        "| 长度层 | 候选数 | 样本数 |",
        "| --- | ---: | ---: |",
    ]
    for bucket in ("short", "medium", "long", "very_long"):
        lines.append(
            f"| {bucket} | {summary['candidate_length_buckets'].get(bucket, 0)} | "
            f"{summary['sample_length_buckets'].get(bucket, 0)} |"
        )
    lines.extend(
        [
            "",
            "## 风险层覆盖",
            "",
            "| 风险层 | 样本数 |",
            "| --- | ---: |",
        ]
    )
    for risk, count in summary["sample_risk_counts"].items():
        lines.append(f"| {risk} | {count} |")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def build_round1_samples(
    manifest_path: Path | str,
    encoding_path: Path | str,
    output_dir: Path | str,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    seed: int = DEFAULT_SEED,
) -> dict:
    manifest = Path(manifest_path)
    encoding = Path(encoding_path)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    candidates = merge_candidates(manifest, encoding)
    samples = sample_by_plan(candidates, sample_size, seed)
    write_jsonl(output / "round1_sample_manifest.jsonl", samples)
    write_review(output / "round1_review.md", samples)
    summary = summarize(candidates, samples, sample_size, seed)
    write_summary(output / "round1_sample_summary.md", summary)
    return summary


def main() -> None:
    clean_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build round1 stratified review sample for Markdown cleaning.")
    parser.add_argument("--manifest", default=str(clean_root / "manifest" / "manifest.jsonl"))
    parser.add_argument("--encoding-report", default=str(clean_root / "encoding_check" / "encoding_report.jsonl"))
    parser.add_argument("--output", default=str(clean_root / "samples"))
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()

    summary = build_round1_samples(
        args.manifest,
        args.encoding_report,
        args.output,
        sample_size=args.sample_size,
        seed=args.seed,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
