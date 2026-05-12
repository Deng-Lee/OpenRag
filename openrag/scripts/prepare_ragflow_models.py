"""Download and prepare offline RAGFlow model assets.

Usage:
    python scripts/prepare_ragflow_models.py

This script downloads:
1) InfiniFlow/deepdoc (OCR/layout/tsr onnx assets)
2) InfiniFlow/text_concat_xgb_v1.0 (updown_concat_xgb.model)

Then writes them into:
    openrag/rag/res/deepdoc
"""

from __future__ import annotations

from pathlib import Path
import os
import sys


REQUIRED_FILES = [
    "det.onnx",
    "rec.onnx",
    "ocr.res",
    "layout.onnx",
    "tsr.onnx",
    "updown_concat_xgb.model",
]


def main() -> int:
    try:
        from huggingface_hub import snapshot_download, hf_hub_download
    except Exception as exc:  # pragma: no cover
        print(f"[ERROR] huggingface_hub unavailable: {exc}", file=sys.stderr)
        print('Install first: python -m pip install "huggingface_hub>=0.20.0"', file=sys.stderr)
        return 2

    project_root = Path(__file__).resolve().parents[1]
    target_dir = project_root / "rag" / "res" / "deepdoc"
    target_dir.mkdir(parents=True, exist_ok=True)

    endpoints: list[str] = []
    env_endpoint = os.environ.get("HF_ENDPOINT", "").strip()
    if env_endpoint:
        endpoints.append(env_endpoint)
    for ep in ("https://hf-mirror.com", "https://huggingface.co"):
        if ep not in endpoints:
            endpoints.append(ep)

    print(f"[INFO] target model dir: {target_dir}")
    print(f"[INFO] trying HF endpoints in order: {endpoints}")

    deepdoc_ok = False
    deepdoc_last_err: Exception | None = None
    for ep in endpoints:
        try:
            print(f"[INFO] downloading InfiniFlow/deepdoc via {ep} ...")
            os.environ["HF_ENDPOINT"] = ep
            snapshot_download(
                repo_id="InfiniFlow/deepdoc",
                local_dir=str(target_dir),
                local_dir_use_symlinks=False,
            )
            deepdoc_ok = True
            break
        except Exception as exc:
            deepdoc_last_err = exc
            print(f"[WARN] deepdoc failed via {ep}: {exc}", file=sys.stderr)
    if not deepdoc_ok:
        print("[ERROR] failed to download InfiniFlow/deepdoc from all endpoints.", file=sys.stderr)
        if deepdoc_last_err:
            print(f"[ERROR] last error: {deepdoc_last_err}", file=sys.stderr)
        return 1

    updown_dst = target_dir / "updown_concat_xgb.model"
    updown_ok = updown_dst.exists()
    updown_last_err: Exception | None = None

    if not updown_ok:
        for ep in endpoints:
            try:
                print(f"[INFO] downloading updown_concat_xgb.model via {ep} ...")
                os.environ["HF_ENDPOINT"] = ep
                downloaded = hf_hub_download(
                    repo_id="InfiniFlow/text_concat_xgb_v1.0",
                    filename="updown_concat_xgb.model",
                    local_dir=str(target_dir),
                    local_dir_use_symlinks=False,
                )
                print(f"[INFO] updown model downloaded to: {downloaded}")
                updown_ok = updown_dst.exists()
                if updown_ok:
                    break
            except Exception as exc:
                updown_last_err = exc
                print(f"[WARN] updown model failed via {ep}: {exc}", file=sys.stderr)

    if not updown_ok:
        print("[ERROR] failed to download updown_concat_xgb.model from all endpoints.", file=sys.stderr)
        if updown_last_err:
            print(f"[ERROR] last error: {updown_last_err}", file=sys.stderr)

    missing = [name for name in REQUIRED_FILES if not (target_dir / name).exists()]
    if missing:
        print("[ERROR] model preparation incomplete. missing files:", file=sys.stderr)
        for name in missing:
            print(f"  - {name}", file=sys.stderr)
        print(
            "[HINT] You can manually copy missing files into openrag/rag/res/deepdoc, "
            "then rerun this script to verify.",
            file=sys.stderr,
        )
        return 1

    print("[OK] offline ragflow models are ready.")
    for name in REQUIRED_FILES:
        path = target_dir / name
        print(f"  - {name} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

