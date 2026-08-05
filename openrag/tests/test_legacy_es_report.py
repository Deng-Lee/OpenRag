import json

from scripts.legacy_es_report import render_terminal_table, write_run_reports


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _preflight(workspace_id, slug, *, gate, missing=0, orphan=0):
    return {
        "workspace_id": workspace_id,
        "workspace_name": f"Workspace {workspace_id}",
        "workspace_slug": slug,
        "db_active_chunk_count": 2,
        "repair_gate": gate,
        "mapping_preflight": {
            "resource_type": "physical_index",
            "status": "MAPPING_COMPATIBLE",
        },
        "l2_preflight": {"missing_l2_count": 0},
        "pre_legacy_audit": {
            "db_active_chunk_count": 2,
            "es_doc_count": 2 - missing + orphan,
            "missing_es_chunk_count": missing,
            "orphan_chunk_count": orphan,
            "deleted_file_chunk_count": 0,
            "file_id_mismatch_count": 0,
        },
    }


def test_write_run_reports_builds_terminal_csv_markdown_and_workspace_evidence(tmp_path):
    run_dir = tmp_path / "run-001"
    first = run_dir / "workspaces" / "000001-中文"
    second = run_dir / "workspaces" / "000002-broken"
    _write_json(
        first / "01-preflight.json",
        _preflight(
            1,
            "中文",
            gate="READY_FOR_MISSING_ONLY_UPSERT",
            missing=1,
        ),
    )
    _write_json(
        second / "01-preflight-error.json",
        {
            "status": "FAILED_WORKSPACE_AUDIT",
            "workspace_id": 2,
            "workspace_name": "Broken",
            "workspace_slug": "broken",
            "error": "network unavailable",
        },
    )

    rows = write_run_reports(run_dir, environment_label="external-50")
    table = render_terminal_table(rows)

    assert [row["workspace_id"] for row in rows] == [1, 2]
    assert rows[0]["next_action"] == "PREPARE_MISSING"
    assert "未出现在 legacy ES" in rows[0]["reason"]
    assert rows[1]["next_action"] == "RETRY_AFTER_DIAGNOSIS"
    assert rows[1]["reason"] == "network unavailable"
    assert "中文" in table
    assert "FAILED_WORKSPACE_AUDIT" in table
    assert (run_dir / "summary.json").exists()
    assert (run_dir / "summary.csv").exists()
    assert (run_dir / "summary.md").exists()
    assert (first / "execution.md").exists()
    assert "01-preflight.json" in (first / "execution.md").read_text(
        encoding="utf-8"
    )
    assert "解决方案" in (first / "execution.md").read_text(encoding="utf-8")


def test_latest_stage_changes_next_action_without_overwriting_prior_evidence(tmp_path):
    run_dir = tmp_path / "run-002"
    workspace_dir = run_dir / "workspaces" / "000001-demo"
    _write_json(
        workspace_dir / "01-preflight.json",
        _preflight(
            1,
            "demo",
            gate="READY_FOR_MISSING_ONLY_UPSERT",
            missing=1,
        ),
    )
    plan = {
        "status": "AWAITING_MISSING_REPAIR_APPROVAL",
        "workspace_id": 1,
        "workspace_slug": "demo",
        "preflight": _preflight(
            1,
            "demo",
            gate="READY_FOR_MISSING_ONLY_UPSERT",
            missing=1,
        ),
    }
    _write_json(workspace_dir / "02-missing-plan.json", plan)

    awaiting = write_run_reports(run_dir, environment_label="internal-16")
    assert awaiting[0]["next_action"] == "APPLY_MISSING_AFTER_APPROVAL"

    _write_json(
        workspace_dir / "04-post-missing-audit.json",
        {
            "anomaly_count": 1,
            "db_active_chunk_count": 2,
            "es_doc_count": 3,
            "missing_es_chunk_count": 0,
            "orphan_chunk_count": 1,
            "deleted_file_chunk_count": 0,
            "file_id_mismatch_count": 0,
        },
    )
    repaired = write_run_reports(run_dir, environment_label="internal-16")

    assert repaired[0]["next_action"] == "PREPARE_ORPHANS"
    assert (workspace_dir / "01-preflight.json").exists()
    assert (workspace_dir / "02-missing-plan.json").exists()
