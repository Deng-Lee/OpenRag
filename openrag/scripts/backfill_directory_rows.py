#!/usr/bin/env python3
"""回填缺失的目录行 (backfill directory rows)。

背景
----
历史上经 Web ``/files/upload`` 上传的文件，只落了带完整深层 ``uri`` 的文件行，
没有创建中间目录行 (``is_directory=true``)。这导致 Web 目录树 / 服务令牌目录
接口看不到这些目录。本脚本对**存量文件**追溯补齐其所有祖先目录行，等价于
「方案二」在上传时所做的事。

特性
----
- **幂等**：可反复运行；已存在的目录不会重复创建。
- **安全**：默认 dry-run（只预览、不写库）；必须显式 ``--apply`` 才写入。
- **版本健壮**：新版本若存在 ``ensure_directory_path`` 就复用它；旧版本（如 1.1.x，
  尚无该函数）自动回退为**直接按 File 模型建目录行**——两条路结果一致。
- **离线**：仅依赖**已安装的 openrag 包 + Python 标准库**，无需联网、无需 pip
  安装——可直接在已部署的 api / worker 容器（或 K8s Pod）内运行（内网友好）。

数据库
------
复用应用配置（``POSTGRES_*`` 环境变量 / ``docker/.env`` / ``openrag/.env``），
与 api / worker 同一个库。在容器 / Pod 内运行时无需额外配置。

用法（详见 docs/2026-06-23-backfill-runbook.md）
-----------------------------------------------
  # 预览单个空间（dry-run，不写库）
  python backfill_directory_rows.py --workspace-id 7
  # 确认后实际回填
  python backfill_directory_rows.py --workspace-id 7 --apply
  # 所有空间
  python backfill_directory_rows.py --all-workspaces            # 预览
  python backfill_directory_rows.py --all-workspaces --apply    # 写库
"""
from __future__ import annotations

import argparse
from typing import List, Set

from sqlalchemy.exc import IntegrityError

from openrag.database import SessionLocal, get_engine
from openrag.models.file import File
from openrag.models.workspace import Workspace

# 新版本走应用自身的 mkdir -p；旧版本（无此函数）回退到 _create_dir_row_inline。
try:
    from openrag.services.file_ingest import ensure_directory_path  # type: ignore
except Exception:  # pragma: no cover - 取决于部署版本
    ensure_directory_path = None  # type: ignore


def _ancestor_dirs(file_uri: str) -> List[str]:
    """该文件 uri 需要的所有祖先目录路径（不含根 ``/``），由浅到深。

    例：``/a/b/c.md`` -> ``['/a', '/a/b']``。
    """
    u = (file_uri or "").replace("\\", "/")
    parent = u.rsplit("/", 1)[0]  # 去掉最后一段（文件名）
    out: List[str] = []
    cur = ""
    for seg in (p for p in parent.split("/") if p):
        cur = f"{cur}/{seg}"
        out.append(cur)
    return out


def _immediate_parent(file_uri: str) -> str:
    u = (file_uri or "").replace("\\", "/")
    parent = u.rsplit("/", 1)[0]
    return parent or "/"


def _existing_dir_uris(db, workspace_id: int) -> Set[str]:
    return {
        r.uri
        for r in db.query(File).filter(
            File.workspace_id == workspace_id, File.is_directory.is_(True)
        )
    }


def _create_dir_row_inline(db, ws: Workspace, uri: str) -> None:
    """旧版本回退：直接建一行目录。复用部署版 File 模型的列默认值。

    每行独立提交，重复（唯一约束冲突）则跳过——天然幂等。
    """
    name = uri.rsplit("/", 1)[-1] or "root"  # 根 "/" 用 name="root"
    row = File(
        uri=uri,
        name=name,
        owner_id=ws.owner_id,
        workspace_id=ws.id,
        is_directory=True,
        size=0,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # 已存在 / 并发创建 → 跳过


def _target_workspaces(db, workspace_ids: List[int], all_workspaces: bool) -> List[Workspace]:
    q = db.query(Workspace)
    if all_workspaces:
        return q.order_by(Workspace.id).all()
    return q.filter(Workspace.id.in_(workspace_ids)).order_by(Workspace.id).all()


def process_workspace(db, ws: Workspace, apply: bool) -> dict:
    """预览（apply=False）或回填（apply=True）单个空间，返回统计字典。"""
    files = (
        db.query(File)
        .filter(File.workspace_id == ws.id, File.is_directory.is_(False))
        .all()
    )
    existing_dirs = _existing_dir_uris(db, ws.id)
    file_uris = {f.uri for f in files}

    needed: Set[str] = set()
    for f in files:
        needed.update(_ancestor_dirs(f.uri))
    missing = sorted(needed - existing_dirs)
    # 应为目录的路径已被同名「文件」占用 → 无法建目录，需人工确认
    conflicts = sorted(d for d in missing if d in file_uris)
    creatable = [d for d in missing if d not in file_uris]

    mode_note = "" if ensure_directory_path is not None else "  [回退: 直接建目录行]"
    print(
        f"[ws {ws.id} / {ws.slug}] 文件 {len(files)} 个, "
        f"已有目录 {len(existing_dirs)} 个, 缺失目录 {len(missing)} 个{mode_note}"
    )
    for m in missing:
        flag = "   <= 冲突: 已被同名文件占用，跳过" if m in file_uris else ""
        print(f"    + {m}{flag}")

    created = 0
    skipped: List[tuple] = []
    if apply and creatable:
        if ensure_directory_path is not None:
            # 新版本：对每个不同的「直接父目录」调用 mkdir -p（建全部缺失祖先）。
            parents = sorted({_immediate_parent(f.uri) for f in files} - {"/"})
            for p in parents:
                try:
                    ensure_directory_path(db, ws, p)
                except Exception as exc:  # HTTPException(409 占用 / 503 争用) 等
                    skipped.append((p, getattr(exc, "detail", str(exc))))
        else:
            # 旧版本：浅到深直接建目录行。
            for uri in sorted(creatable, key=lambda u: u.count("/")):
                _create_dir_row_inline(db, ws, uri)
        created = len(_existing_dir_uris(db, ws.id) - existing_dirs)
        tail = f", 跳过 {len(skipped)} 个" if skipped else ""
        print(f"    => 实际创建目录 {created} 个{tail}")
        for p, detail in skipped:
            print(f"       - 跳过 {p}: {detail}")

    return {
        "files": len(files),
        "missing": len(missing),
        "conflicts": len(conflicts),
        "created": created,
        "skipped": len(skipped),
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="回填缺失的目录行（为存量深层文件补建 is_directory 目录行）。默认 dry-run。",
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--workspace-id", type=int, action="append", dest="workspace_ids",
        help="目标空间 id，可重复指定以处理多个空间",
    )
    grp.add_argument("--all-workspaces", action="store_true", help="处理所有空间")
    p.add_argument(
        "--apply", action="store_true",
        help="实际写库；不加该参数时只预览（dry-run）",
    )
    args = p.parse_args()

    SessionLocal.configure(bind=get_engine())
    db = SessionLocal()
    mode = "APPLY（写库）" if args.apply else "DRY-RUN（仅预览）"
    backend = "ensure_directory_path" if ensure_directory_path is not None else "inline（旧版本回退）"
    print(f"== 回填目录行 [{mode}] 建目录方式: {backend} ==")
    try:
        workspaces = _target_workspaces(db, args.workspace_ids or [], args.all_workspaces)
        if not workspaces:
            print("没有匹配的空间。")
            return 1
        totals = {"files": 0, "missing": 0, "conflicts": 0, "created": 0, "skipped": 0}
        for ws in workspaces:
            r = process_workspace(db, ws, args.apply)
            for k in totals:
                totals[k] += r[k]
        print("---- 汇总 ----")
        print(
            f"空间 {len(workspaces)} 个, 文件 {totals['files']} 个, "
            f"缺失目录 {totals['missing']} 个, 冲突 {totals['conflicts']} 个, "
            f"已创建 {totals['created']} 个, 跳过 {totals['skipped']} 个"
        )
        if not args.apply and totals["missing"]:
            print("提示：以上为预览。确认无误后加 --apply 实际写库。")
        if totals["conflicts"]:
            print("警告：存在「目录路径被同名文件占用」的冲突，需人工确认处理。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
