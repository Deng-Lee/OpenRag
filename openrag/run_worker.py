#!/usr/bin/env python
"""Worker runner script"""

import sys
import os

# Add src and ragflow to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
# Add ragflow directory so "from rag.*" imports work
ragflow_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'ragflow')
if os.path.exists(ragflow_path) and ragflow_path not in sys.path:
    sys.path.insert(0, ragflow_path)

# Paddle/RAGFlow PDF 解析链会间接 import onnxruntime；缺包时任务中途失败不易排查
try:
    import onnxruntime  # noqa: F401
except ImportError as e:
    raise SystemExit(
        "缺少依赖 onnxruntime，PDF 等解析任务会失败。\n"
        f"当前解释器: {sys.executable}\n"
        '请在本环境中安装: python -m pip install "onnxruntime>=1.16.0"\n'
        "若项目已配置 venv，请先激活 openrag\\\\venv 再运行: python run_worker.py"
    ) from e

from openrag.worker import run_worker_loop

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='OpenRag Task Worker')
    parser.add_argument('--api-url', default='http://localhost:8001', help='API base URL')
    parser.add_argument('--worker-id', default=None, help='Worker ID (auto-generated if not provided)')
    parser.add_argument('--batch-size', type=int, default=5, help='Number of tasks to pull per request')
    parser.add_argument('--poll-interval', type=int, default=5, help='Poll interval in seconds')

    args = parser.parse_args()

    print(f"Starting worker...")
    print(f"API URL: {args.api_url}")
    print(f"Worker ID: {args.worker_id or 'auto-generated'}")
    print(f"Batch size: {args.batch_size}")
    print(f"Poll interval: {args.poll_interval}s")
    print("-" * 50)

    run_worker_loop(
        api_base_url=args.api_url,
        worker_id=args.worker_id,
        batch_size=args.batch_size,
        poll_interval=args.poll_interval
    )
