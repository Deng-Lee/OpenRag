#!/usr/bin/env python3
"""
将本地目录树批量上传到 OpenRag 工作区，逻辑路径与相对目录结构一致。

依赖（与项目一致）: pip install httpx
用法示例:
  python scripts/bulk_import_folder.py \\
    --api-base http://127.0.0.1:8000 \\
    --email you@example.com --password secret \\
    --workspace-id 1 \\
    --root "D:/datasets/knowledge" \\
    --remote-prefix /imports/knowledge

仅上传「在根目录下的相对路径」；远程父路径为 remote-prefix + 相对父目录（POSIX）。
"""

from __future__ import annotations

import argparse
import asyncio
import mimetypes
import os
import sys
from pathlib import Path
from typing import Iterable

import httpx

# 与 openrag.services.file_ingest 一致，便于本机预检
MAX_FILE_SIZE = 50 * 1024 * 1024


def _normalize_remote_prefix(prefix: str) -> str:
    p = (prefix or "/").strip().replace("\\", "/")
    if not p:
        return "/"
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/") or "/"


def _parent_logical_path(remote_prefix: str, relative_file: Path) -> str:
    """relative_file 为相对根的文件路径；返回 Form 字段 path（父目录逻辑路径）。"""
    prefix = _normalize_remote_prefix(remote_prefix)
    parts = relative_file.parent.as_posix()
    if parts in (".", ""):
        return prefix if prefix != "/" else "/"
    child = parts.lstrip("/")
    if prefix == "/":
        return "/" + child
    return f"{prefix}/{child}"


def _iter_files(
    root: Path,
    *,
    follow_symlinks: bool,
    skip_hidden: bool,
) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        if skip_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if skip_hidden and name.startswith("."):
                continue
            yield Path(dirpath) / name


def _guess_parser(path: Path, default: str) -> str:
    if default != "auto":
        return default
    ext = path.suffix.lower()
    mapping = {
        ".pdf": "pdf",
        ".docx": "docx",
        ".doc": "docx",
        ".xlsx": "xlsx",
        ".xls": "xlsx",
        ".pptx": "pptx",
        ".ppt": "pptx",
        ".txt": "txt",
        ".md": "md",
        ".markdown": "md",
        ".html": "html",
        ".htm": "html",
        ".json": "json",
        ".csv": "csv",
        ".epub": "epub",
    }
    return mapping.get(ext, "auto")


async def _login(client: httpx.AsyncClient, api_base: str, email: str, password: str) -> str:
    r = await client.post(
        f"{api_base.rstrip('/')}/users/login",
        data={"email": email, "password": password},
    )
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token")
    if not token:
        raise SystemExit("登录响应中缺少 access_token")
    return token


def _api_base_candidates(api_base: str) -> list[str]:
    """
    生成 API Base 候选列表：
    - 原始 base
    - 若未带 /api，则自动追加 /api 作为网关兼容候选
    """
    base = api_base.rstrip("/")
    if not base:
        base = "http://127.0.0.1:8000"
    candidates = [base]
    if not base.endswith("/api"):
        candidates.append(f"{base}/api")
    # 去重并保持顺序
    return list(dict.fromkeys(candidates))


async def _resolve_api_base_for_user_auth(
    client: httpx.AsyncClient, api_base: str, email: str, password: str
) -> tuple[str, str]:
    """
    尝试登录并自动识别 API base（兼容 Nginx 的 /api 前缀代理）。
    返回: (token, resolved_api_base)
    """
    last_error: Exception | None = None
    for candidate in _api_base_candidates(api_base):
        try:
            token = await _login(client, candidate, email, password)
            if candidate != api_base.rstrip("/"):
                print(f"检测到网关前缀，自动使用 API Base: {candidate}")
            return token, candidate
        except httpx.HTTPStatusError as e:
            last_error = e
            continue
    if isinstance(last_error, httpx.HTTPStatusError):
        raise last_error
    raise SystemExit("无法完成登录：请检查 API 地址与网络连通性")


async def _upload_one(
    client: httpx.AsyncClient,
    api_base: str,
    token: str,
    workspace_id: int,
    local_path: Path,
    relative: Path,
    remote_prefix: str,
    parser_type: str,
    dry_run: bool,
) -> tuple[str, bool, str]:
    logical_parent = _parent_logical_path(remote_prefix, relative)
    parser = _guess_parser(local_path, parser_type)
    size = local_path.stat().st_size
    if size > MAX_FILE_SIZE:
        return (relative.as_posix(), False, f"超过 {MAX_FILE_SIZE // (1024 * 1024)}MB 限制，跳过")

    ctype = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"

    if dry_run:
        return (relative.as_posix(), True, f"DRY-RUN path={logical_parent!r} parser={parser}")

    with local_path.open("rb") as f:
        files = {"file": (local_path.name, f, ctype)}
        data = {
            "workspace_id": str(workspace_id),
            "path": logical_parent,
            "parser_type": parser,
        }
        r = await client.post(
            f"{api_base.rstrip('/')}/files/upload",
            headers={"Authorization": f"Bearer {token}"},
            data=data,
            files=files,
            timeout=httpx.Timeout(600.0),
        )
    if r.is_success:
        return (relative.as_posix(), True, f"uri={r.json().get('uri', '')}")
    return (relative.as_posix(), False, f"{r.status_code} {r.text[:500]}")


async def _run(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"根目录不存在或不是文件夹: {root}", file=sys.stderr)
        return 2

    remote_prefix = _normalize_remote_prefix(args.remote_prefix)

    files = list(
        _iter_files(
            root,
            follow_symlinks=args.follow_symlinks,
            skip_hidden=not args.include_hidden,
        )
    )
    if args.extensions:
        allowed = {e.lower().lstrip(".") for e in args.extensions}
        files = [p for p in files if p.suffix.lower().lstrip(".") in allowed]

    if not files:
        print("没有可上传的文件。", file=sys.stderr)
        return 1

    limits = httpx.Limits(max_keepalive_connections=args.workers, max_connections=args.workers)
    api_base = args.api_base.rstrip("/")
    async with httpx.AsyncClient(limits=limits) as client:
        if args.token:
            token = args.token.strip()
            # token 模式下也尽量兼容 /api 前缀
            bases = _api_base_candidates(api_base)
            api_base = bases[0]
            if len(bases) > 1:
                # 先探测上传接口可达性（不上传文件，仅做极轻量健康试探）
                # /docs 通常在同一路由前缀下；若失败则回退下一个候选
                for candidate in bases:
                    try:
                        resp = await client.get(f"{candidate}/docs", timeout=httpx.Timeout(10.0))
                        if resp.status_code in (200, 301, 302, 307, 308):
                            api_base = candidate
                            if candidate != bases[0]:
                                print(f"检测到网关前缀，自动使用 API Base: {candidate}")
                            break
                    except Exception:
                        continue
        else:
            token, api_base = await _resolve_api_base_for_user_auth(
                client, api_base, args.email, args.password
            )

        sem = asyncio.Semaphore(args.workers)

        total = len(files)

        async def guarded(index: int, fp: Path) -> tuple[str, bool, str]:
            try:
                rel = fp.relative_to(root)
            except ValueError:
                return (str(fp), False, "不在 root 之下")
            async with sem:
                print(f"[{index}/{total}] 正在导入 {rel.as_posix()}", flush=True)
                return await _upload_one(
                    client,
                    api_base,
                    token,
                    args.workspace_id,
                    fp,
                    rel,
                    remote_prefix,
                    args.parser_type,
                    args.dry_run,
                )

        ok, fail = 0, 0
        for coro in asyncio.as_completed(
            [guarded(index, f) for index, f in enumerate(files, start=1)]
        ):
            rel, success, msg = await coro
            if success:
                ok += 1
                print(f"[OK] {rel}  {msg}")
            else:
                fail += 1
                print(f"[FAIL] {rel}  {msg}", file=sys.stderr)

    print(f"\n完成: 成功 {ok}, 失败 {fail}, 合计 {len(files)}")
    return 0 if fail == 0 else 1


def main() -> None:
    p = argparse.ArgumentParser(description="批量导入文件夹并保持目录结构（OpenRag /files/upload）")
    p.add_argument("--api-base", default=os.environ.get("OPENRAG_API", "http://127.0.0.1:8000"))
    p.add_argument("--workspace-id", type=int, required=True)
    p.add_argument("--root", required=True, help="本地要导入的根目录")
    p.add_argument(
        "--remote-prefix",
        default="/",
        help="工作区逻辑路径前缀，例如 /imports/batch1；文件会出现在此前缀下",
    )
    auth = p.add_mutually_exclusive_group(required=True)
    auth.add_argument("--token", help="JWT（与网页登录后一致）；不设则用邮箱密码登录")
    auth.add_argument("--email", help="与 --password 一起用于 /users/login")
    p.add_argument("--password", help="登录密码")
    p.add_argument("--parser-type", default="auto", help="固定解析器类型，默认 auto 按扩展名推断")
    p.add_argument("--workers", type=int, default=4, help="并发上传数")
    p.add_argument("--extensions", nargs="*", help="仅上传这些扩展名，如 pdf docx md（不含点）")
    p.add_argument("--follow-symlinks", action="store_true")
    p.add_argument("--include-hidden", action="store_true", help="包含以 . 开头的文件/目录")
    p.add_argument("--dry-run", action="store_true", help="只打印将使用的逻辑路径，不上传")
    args = p.parse_args()

    if args.token is None:
        if not args.email or not args.password:
            p.error("使用邮箱登录时必须同时提供 --email 与 --password")

    try:
        raise SystemExit(asyncio.run(_run(args)))
    except httpx.HTTPStatusError as e:
        print(f"HTTP 错误: {e.response.status_code} {e.response.text[:800]}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
