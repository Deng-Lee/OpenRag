from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = REPO_ROOT / "docs/04-外部系统接入与API.md"


def read_external_api_doc() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def test_external_api_doc_describes_preview_link_and_iframe_integration() -> None:
    doc = read_external_api_doc()

    required_snippets = [
        "POST /workspaces/{workspace_name}/preview-links",
        "`file_id`",
        "`chunk_id`",
        "`chunk_index`",
        "`ttl_seconds`",
        "`preview_url`",
        "`expires_at`",
        "`ttl_seconds`",
        "`X-OpenRag-Token` 只在接入方后端使用，浏览器 iframe 只使用短期 preview token",
        "X-OpenRag-Preview-Token",
        "/embed/document-preview#token=",
        'sandbox="allow-scripts allow-same-origin"',
        "PREVIEW_FRAME_ANCESTORS",
        "默认 15 分钟",
        "900 秒",
        "不提供显式下载按钮",
        "iframe 内部预览 API 会获取渲染所需内容",
        "不是 DRM",
        "接入方不要自己拼 preview token/preview_url",
    ]

    missing = [snippet for snippet in required_snippets if snippet not in doc]
    assert missing == []
