"""Validate Task 8 evaluation artifacts.

This script intentionally uses only the Python standard library so the
offline eval corpus can be checked without starting OpenRag services.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = ROOT / "docs" / "eval"
DATE_TAG = "2026-05-27"

MANIFEST = EVAL_DIR / f"corpus_manifest.{DATE_TAG}.json"
CANONICAL = EVAL_DIR / f"canonical_docs.{DATE_TAG}.jsonl"
DATASET_50 = EVAL_DIR / f"eval_dataset_50.reviewed.{DATE_TAG}.jsonl"
DATASET_500 = EVAL_DIR / f"eval_dataset_500.llm_assisted.{DATE_TAG}.jsonl"
CHUNK_MAPPINGS = EVAL_DIR / f"chunk_mappings.{DATE_TAG}.jsonl"
COMPARE = EVAL_DIR / f"eval_run_abc_compare.{DATE_TAG}.json"

EXPECTED_FORMAT_SUMMARY = {
    ".md": 153,
    ".docx": 116,
    ".doc": 59,
    ".pdf": 56,
    ".xls": 6,
    ".xlsx": 2,
}
EXPECTED_50_BREAKDOWN = {
    "single_doc_fact": 15,
    "single_doc_rule_or_procedure": 8,
    "multi_doc_compare": 8,
    "multi_scenario_condition_extract": 10,
    "version_or_update_trace": 4,
    "negative_or_boundary": 5,
}
EXPECTED_500_BREAKDOWN = {
    "single_doc_fact": 150,
    "single_doc_rule_or_procedure": 80,
    "multi_doc_compare": 80,
    "multi_scenario_condition_extract": 120,
    "version_or_update_trace": 40,
    "negative_or_boundary": 30,
}
INDEX_VERSIONS = {"index_a_512_64", "index_b_1000_150", "index_c_800_100"}


def main() -> int:
    errors: list[str] = []

    manifest = _load_json(MANIFEST, errors)
    canonical = _load_jsonl(CANONICAL, errors)
    dataset_50 = _load_jsonl(DATASET_50, errors)
    dataset_500 = _load_jsonl(DATASET_500, errors)
    chunk_mappings = _load_jsonl(CHUNK_MAPPINGS, errors)
    compare = _load_json(COMPARE, errors)

    if manifest:
        _validate_manifest(manifest, errors)
    if canonical:
        _validate_canonical(canonical, errors)
    if dataset_50:
        _validate_dataset(dataset_50, 50, EXPECTED_50_BREAKDOWN, errors)
        _validate_reviewed_dataset(dataset_50, errors)
    if dataset_500:
        _validate_dataset(dataset_500, 500, EXPECTED_500_BREAKDOWN, errors)
    if chunk_mappings:
        _validate_chunk_mappings(chunk_mappings, errors)
    if compare:
        _validate_compare(compare, errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("Task8 artifact validation passed")
    print(f"manifest_files={len(manifest['files'])}")
    print(f"canonical_docs={len(canonical)}")
    print(f"dataset_50={len(dataset_50)} breakdown={dict(Counter(item['query_type'] for item in dataset_50))}")
    print(f"dataset_500={len(dataset_500)} breakdown={dict(Counter(item['query_type'] for item in dataset_500))}")
    print(f"chunk_mappings={len(chunk_mappings)}")
    print("compare_runs=" + ",".join(run["index_version"] for run in compare["runs"]))
    return 0


def _load_json(path: Path, errors: list[str]) -> dict:
    if not path.exists():
        errors.append(f"missing file: {path}")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - diagnostic path
        errors.append(f"invalid JSON {path}: {exc}")
        return {}


def _load_jsonl(path: Path, errors: list[str]) -> list[dict]:
    if not path.exists():
        errors.append(f"missing file: {path}")
        return []
    records: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                records.append(json.loads(line))
    except Exception as exc:  # pragma: no cover - diagnostic path
        errors.append(f"invalid JSONL {path}:{line_number}: {exc}")
        return []
    return records


def _validate_manifest(manifest: dict, errors: list[str]) -> None:
    files = manifest.get("files", [])
    if manifest.get("expected_file_count") != 392:
        errors.append("manifest expected_file_count must be 392")
    if len(files) != 392:
        errors.append(f"manifest files length must be 392, got {len(files)}")
    summary = manifest.get("format_summary", {})
    normalized_summary = {f".{key.lstrip('.')}": value for key, value in summary.items()}
    if normalized_summary != EXPECTED_FORMAT_SUMMARY:
        errors.append(f"manifest format_summary mismatch: {normalized_summary}")
    counted = Counter(file.get("file_ext") for file in files)
    if dict(counted) != EXPECTED_FORMAT_SUMMARY:
        errors.append(f"manifest files ext count mismatch: {dict(counted)}")
    for index, file in enumerate(files, start=1):
        if not file.get("source_doc_hash", "").startswith("sha256:"):
            errors.append(f"manifest file {index} missing sha256 source_doc_hash")
        if not file.get("local_path"):
            errors.append(f"manifest file {index} missing local_path")
        if file.get("include_in_eval") is False and not file.get("exclude_reason"):
            errors.append(f"manifest file {index} excluded without exclude_reason")


def _validate_canonical(records: list[dict], errors: list[str]) -> None:
    if len(records) != 392:
        errors.append(f"canonical_docs length must be 392, got {len(records)}")
    for record in records:
        for key in (
            "corpus_file_id",
            "source_doc_hash",
            "canonical_text_hash",
            "parser_name",
            "parser_version",
            "canonical_json_object_key",
            "canonical_md_object_key",
            "parse_status",
        ):
            if key not in record:
                errors.append(f"canonical record missing {key}: {record.get('corpus_file_id')}")
        if record.get("parse_status") == "preview_indexed" and not record.get("locators"):
            errors.append(f"canonical record missing locators: {record.get('corpus_file_id')}")


def _validate_dataset(
    records: list[dict],
    expected_count: int,
    expected_breakdown: dict[str, int],
    errors: list[str],
) -> None:
    if len(records) != expected_count:
        errors.append(f"dataset length must be {expected_count}, got {len(records)}")
    breakdown = Counter(item.get("query_type") for item in records)
    if dict(breakdown) != expected_breakdown:
        errors.append(f"dataset breakdown mismatch: {dict(breakdown)}")
    query_ids = [item.get("query_id") for item in records]
    if len(set(query_ids)) != len(query_ids):
        errors.append("dataset query_id values must be unique")
    for item in records:
        if not item.get("query_text"):
            errors.append(f"query missing query_text: {item.get('query_id')}")
        if "judgments" not in item:
            errors.append(f"query missing judgments: {item.get('query_id')}")


def _validate_reviewed_dataset(records: list[dict], errors: list[str]) -> None:
    for item in records:
        if item.get("review_status") != "approved":
            errors.append(f"reviewed query is not approved: {item.get('query_id')}")
        positive = [
            judgment
            for judgment in item.get("judgments", [])
            if int(judgment.get("relevance_grade", 0)) >= 2
        ]
        if item.get("query_type") != "negative_or_boundary" and not positive:
            errors.append(f"positive query missing grade>=2 judgment: {item.get('query_id')}")
        for judgment in positive:
            metadata = judgment.get("metadata", {})
            anchor = metadata.get("evidence_anchor", {})
            if not anchor.get("evidence_quote"):
                errors.append(f"judgment missing evidence_quote: {item.get('query_id')}")
            if "char_start" not in anchor or "char_end" not in anchor:
                errors.append(f"judgment missing char span: {item.get('query_id')}")
            mappings = metadata.get("chunk_mappings", {})
            if set(mappings) != INDEX_VERSIONS:
                errors.append(f"judgment missing A/B/C chunk mappings: {item.get('query_id')}")


def _validate_chunk_mappings(records: list[dict], errors: list[str]) -> None:
    if not records:
        errors.append("chunk mapping artifact is empty")
        return
    versions = {record.get("index_version") for record in records}
    if versions != INDEX_VERSIONS:
        errors.append(f"chunk mappings missing index versions: {versions}")
    positive_statuses = [
        record.get("mapping_status")
        for record in records
        if record.get("relevance_grade", 0) >= 2
    ]
    mapped = sum(1 for status in positive_statuses if status in {"mapped", "mapped_partial"})
    if positive_statuses and mapped / len(positive_statuses) < 0.9:
        errors.append("less than 90% positive judgments mapped automatically")


def _validate_compare(compare: dict, errors: list[str]) -> None:
    runs = compare.get("runs", [])
    if {run.get("index_version") for run in runs} != INDEX_VERSIONS:
        errors.append("compare JSON must contain A/B/C runs")
    if not compare.get("dry_run"):
        errors.append("compare JSON must explicitly mark dry_run=true")
    if not compare.get("mini_eval_completed_before_500"):
        errors.append("compare JSON must record mini eval completion before 500 generation")
    for run in runs:
        metrics = run.get("summary_metrics", {})
        for key in (
            "precision@10",
            "recall@50",
            "ndcg@10",
            "mrr@50",
            "map@50",
            "stage_recall@50",
            "rerank_delta",
            "zero_hit_rate",
        ):
            if key not in metrics:
                errors.append(f"compare run missing metric {key}: {run.get('index_version')}")
    if "query_type_breakdown" not in compare:
        errors.append("compare JSON missing query_type_breakdown")


if __name__ == "__main__":
    raise SystemExit(main())
