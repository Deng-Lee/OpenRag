import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIEW_FRAME_ANCESTORS = (
    "'self' http://192.168.100.33:2026 http://192.168.100.32:2026 "
    "http://172.16.31.61:2026 http://172.16.31.156:2026"
)


def read_repo_file(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_nginx_preview_route_has_iframe_security_headers() -> None:
    nginx_conf = read_repo_file("docker/nginx/nginx.conf")

    assert "Content-Security-Policy" in nginx_conf
    assert "frame-ancestors ${PREVIEW_FRAME_ANCESTORS}" in nginx_conf
    assert 'Referrer-Policy "no-referrer"' in nginx_conf
    assert "X-Frame-Options" not in nginx_conf


def test_web_image_renders_nginx_template_at_runtime() -> None:
    dockerfile = read_repo_file("docker/Dockerfile.web")

    assert f'PREVIEW_FRAME_ANCESTORS="{PREVIEW_FRAME_ANCESTORS}"' in dockerfile
    assert "nginx.conf.template" in dockerfile
    assert "envsubst" in dockerfile
    assert "/etc/nginx/nginx.conf" in dockerfile


def test_deployment_configs_define_preview_env_defaults() -> None:
    compose = read_repo_file("docker/docker-compose.prod.yml")
    api_k8s = read_repo_file("k8s/09-api.yaml")
    web_k8s = read_repo_file("k8s/11-web.yaml")
    combined = "\n".join([compose, api_k8s, web_k8s])

    assert (
        "PREVIEW_PUBLIC_WEB_BASE_URL=${PREVIEW_PUBLIC_WEB_BASE_URL:-https://openrag.guozhijishu.com}"
        in compose
    )
    assert "PREVIEW_TOKEN_TTL_SECONDS=${PREVIEW_TOKEN_TTL_SECONDS:-900}" in compose
    assert "PREVIEW_TOKEN_MAX_TTL_SECONDS=${PREVIEW_TOKEN_MAX_TTL_SECONDS:-1800}" in compose
    assert f"PREVIEW_FRAME_ANCESTORS=${{PREVIEW_FRAME_ANCESTORS:-{PREVIEW_FRAME_ANCESTORS}}}" in compose

    assert "name: PREVIEW_PUBLIC_WEB_BASE_URL" in api_k8s
    assert "value: https://openrag.guozhijishu.com" in api_k8s
    assert "name: PREVIEW_TOKEN_TTL_SECONDS" in api_k8s
    assert 'value: "900"' in api_k8s
    assert "name: PREVIEW_TOKEN_MAX_TTL_SECONDS" in api_k8s
    assert 'value: "1800"' in api_k8s

    assert "name: PREVIEW_FRAME_ANCESTORS" in web_k8s
    assert f'value: "{PREVIEW_FRAME_ANCESTORS}"' in web_k8s

    assert not re.search(r"CORS_ORIGINS\s*=\s*(?:\$\{[^}]*:-)?\*", combined)
    assert not re.search(r"name:\s*CORS_ORIGINS\s+value:\s*['\"]?\*", combined)
