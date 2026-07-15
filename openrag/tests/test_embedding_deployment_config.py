"""Deployment manifests enforce identical required embedding settings."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_k8s_api_and_worker_require_embedding_secret_and_shared_settings():
    api = read("k8s/09-api.yaml")
    worker = read("k8s/10-task-worker.yaml")
    for manifest in (api, worker):
        assert "optional: false" in manifest
        for name in (
            "EMBEDDING_MODEL",
            "EMBEDDING_DIMENSION",
            "EMBEDDING_BATCH_SIZE",
            "EMBEDDING_TIMEOUT_SECONDS",
            "EMBEDDING_MAX_ATTEMPTS",
            "EMBEDDING_PROBE_INTERVAL_SECONDS",
        ):
            assert f"name: {name}" in manifest


def test_compose_rejects_empty_embedding_key():
    compose = read("docker/docker-compose.prod.yml")

    assert "${OPENAI_API_KEY:?OPENAI_API_KEY is required}" in compose
    assert "OPENAI_API_KEY=${OPENAI_API_KEY:-}" not in compose


def test_secret_example_does_not_use_empty_key():
    secret = read("k8s/01-secret.example.yaml")

    assert 'OPENAI_API_KEY: ""' not in secret
