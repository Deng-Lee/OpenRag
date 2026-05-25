from openrag.config import StorageConfig


def test_storage_prefix_is_loaded_from_storage_env(monkeypatch):
    monkeypatch.setenv("STORAGE_BUCKET", "rag-kb")
    monkeypatch.setenv("STORAGE_PREFIX", "openrag")

    cfg = StorageConfig()

    assert cfg.bucket == "rag-kb"
    assert cfg.prefix == "openrag"


def test_storage_prefix_defaults_to_none_when_unset(monkeypatch):
    monkeypatch.setenv("STORAGE_BUCKET", "rag-kb")
    monkeypatch.delenv("STORAGE_PREFIX", raising=False)

    cfg = StorageConfig()

    assert cfg.bucket == "rag-kb"
    assert cfg.prefix is None


def test_storage_prefix_empty_value_is_old_compatible(monkeypatch):
    monkeypatch.setenv("STORAGE_BUCKET", "rag-kb")
    monkeypatch.setenv("STORAGE_PREFIX", "   /  ")

    cfg = StorageConfig()

    assert cfg.bucket == "rag-kb"
    assert cfg.prefix is None
