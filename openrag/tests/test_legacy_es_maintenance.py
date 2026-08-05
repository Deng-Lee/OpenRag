import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import scripts.legacy_es_maintenance as maintenance


class _Scalars:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self.values


class _Db:
    def __init__(self, workspaces):
        self.workspaces = workspaces
        self.closed = False

    def execute(self, _query):
        return _Scalars(self.workspaces)

    def close(self):
        self.closed = True


def _preflight(workspace):
    return {
        "workspace_id": workspace.id,
        "workspace_name": workspace.name,
        "workspace_slug": workspace.slug,
        "db_active_chunk_count": 0,
        "repair_gate": "EMPTY_PASSED",
        "mapping_preflight": {
            "resource_type": "absent",
            "status": "EMPTY_INDEX_NOT_REQUIRED",
        },
        "l2_preflight": {"missing_l2_count": 0},
        "pre_legacy_audit": {
            "db_active_chunk_count": 0,
            "es_doc_count": 0,
            "missing_es_chunk_count": 0,
            "orphan_chunk_count": 0,
            "deleted_file_chunk_count": 0,
            "file_id_mismatch_count": 0,
        },
    }


def test_maintenance_script_help_works_when_invoked_by_file_path():
    app_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            str(app_root / "scripts" / "legacy_es_maintenance.py"),
            "--help",
        ],
        cwd=app_root,
        env={**os.environ, "PYTHONPATH": str(app_root / "src")},
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "audit-all" in completed.stdout
    assert "delete-orphans" in completed.stdout


def test_audit_all_records_failed_workspace_and_continues(monkeypatch, tmp_path, capsys):
    workspaces = [
        SimpleNamespace(id=1, name="Broken", slug="broken"),
        SimpleNamespace(id=2, name="Healthy", slug="healthy"),
    ]
    db = _Db(workspaces)
    monkeypatch.setattr(maintenance, "_runtime", lambda: (db, object(), object()))

    def fake_preflight(*, workspace_id, **_kwargs):
        if workspace_id == 1:
            raise RuntimeError("simulated ES timeout")
        return _preflight(workspaces[1])

    monkeypatch.setattr(maintenance, "preflight_workspace", fake_preflight)
    args = SimpleNamespace(
        run_id="run-batch",
        output_root=tmp_path,
        environment_label="external-50",
        workspace_id=None,
        batch_size=100,
    )

    exit_code = maintenance.audit_all(args)

    run_dir = tmp_path / "run-batch"
    failed = run_dir / "workspaces" / "000001-broken" / "01-preflight-error.json"
    passed = run_dir / "workspaces" / "000002-healthy" / "01-preflight.json"
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    output = capsys.readouterr().out
    assert exit_code == 2
    assert db.closed is True
    assert failed.exists()
    assert passed.exists()
    assert summary["workspace_count"] == 2
    assert summary["rows"][0]["status"] == "FAILED_WORKSPACE_AUDIT"
    assert summary["rows"][1]["next_action"] == "NONE"
    assert "broken" in output
    assert "healthy" in output
    assert "simulated ES timeout" in failed.read_text(encoding="utf-8")


def test_single_workspace_action_records_runtime_failure(monkeypatch, tmp_path):
    run_dir = tmp_path / "run-action"
    workspace_dir = run_dir / "workspaces" / "000002-healthy"
    workspace_dir.mkdir(parents=True)
    (run_dir / "environment.json").write_text(
        json.dumps({"environment_label": "external-50"}), encoding="utf-8"
    )
    (workspace_dir / "01-preflight.json").write_text(
        json.dumps(_preflight(SimpleNamespace(id=2, name="Healthy", slug="healthy"))),
        encoding="utf-8",
    )

    def fail_runtime():
        raise RuntimeError("Elasticsearch unavailable")

    monkeypatch.setattr(maintenance, "_runtime", fail_runtime)
    args = SimpleNamespace(
        run_id="run-action",
        output_root=tmp_path,
        environment_label="external-50",
        workspace_id=2,
        batch_size=100,
    )

    exit_code = maintenance._single_workspace_action(args, "prepare-missing")
    result = json.loads(
        (workspace_dir / "02-missing-plan.json").read_text(encoding="utf-8")
    )

    assert exit_code == 2
    assert result["status"] == "FAILED_PREPARE_MISSING"
    assert "Elasticsearch unavailable" in result["error"]
