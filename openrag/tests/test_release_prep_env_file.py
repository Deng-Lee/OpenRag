from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def read_repo_file(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_release_prep_script_accepts_external_env_file() -> None:
    script = read_repo_file(
        "skills/openrag-docker-compose-release-prep/scripts/prepare-openrag-compose-release.ps1"
    )

    assert '[string]$EnvFile = "docker\\.env"' in script
    assert 'Require-File $EnvFile' in script
    assert "Get-Content -Encoding utf8 $EnvFile" in script
    assert '"--env-file" $EnvFile' in script
    assert 'Invoke-Native scp @ScpOptions $EnvFile "$SshTarget`:$ServerHome/shared/openrag.env"' in script
    assert 'Write-Host "EnvFile=$EnvFile"' in script


def test_external_env_template_documents_preview_settings() -> None:
    template = read_repo_file("docker/.env.external-192.168.100.33.example")

    assert "PREVIEW_PUBLIC_WEB_BASE_URL=http://192.168.100.33" in template
    assert (
        'PREVIEW_FRAME_ANCESTORS="\'self\' http://192.168.100.32:2026 '
        'http://192.168.100.33:2026"'
    ) in template
    assert "POSTGRES_PASSWORD=<强密码>" in template
    assert "SECRET_KEY=<32位以上随机字符串>" in template


def test_external_deploy_guide_uses_env_file_parameter() -> None:
    guide = read_repo_file("docs/外网DockerCompose部署指南.md")

    assert '$EnvFile = "docker\\.env.external-192.168.100.33"' in guide
    assert "Copy-Item docker\\.env.external-192.168.100.33.example $EnvFile" in guide
    assert '--env-file $EnvFile -f docker\\docker-compose.prod.yml config --quiet' in guide
    assert 'scp $EnvFile "$SshTarget`:$ServerHome/shared/openrag.env"' in guide
    assert "-EnvFile $EnvFile" in guide
