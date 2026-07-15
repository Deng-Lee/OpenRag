"""Task Worker for pulling and executing tasks from broker

This module provides a worker that:
1. Pulls tasks from the broker via HTTP
2. Maintains heartbeat during execution
3. Reports task status back to the API
4. Supports multi-process mode for parallel execution
"""

import os
import sys
import time
import uuid
import signal
import argparse
import tempfile
import requests
import multiprocessing
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta, timezone

from openrag.processors.document_processor import DocumentProcessor
from openrag.parsers.parser_registry import ParserRegistry
from openrag.chunking.chunk_engine import ChunkEngine
from openrag.chunking.document_type import normalize_document_type
from openrag.embedding.embedding_engine import EmbeddingEngine
from openrag.embedding.errors import (
    EmbeddingConfigurationError,
    EmbeddingProviderError,
)
from openrag.hierarchy.hierarchy_storage import HierarchyStorage
from openrag.storage.minio_storage import MinioStorage
from openrag.database import SessionLocal, get_engine
from openrag.services.trace_service import TraceService
from openrag.tracing.context import get_trace_context, reset_trace_context, set_trace_context

import logging as _logging

_logging.basicConfig(
    level=_logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_logger = _logging.getLogger(__name__)


def _create_vector_store(embedding_engine: EmbeddingEngine):
    """Create the required chunk vector store with the configured dimension."""
    from openrag.vectorstore.milvus_store import MilvusStore

    return MilvusStore(dimension=embedding_engine.dimension)


def _create_layer_store(embedding_engine: EmbeddingEngine, required: bool):
    """Create the layer vector store only when L0/L1 retrieval is enabled."""
    if not required:
        return None
    from openrag.vectorstore.milvus_layer_store import MilvusLayerStore

    return MilvusLayerStore(dimension=embedding_engine.dimension)


def _create_es_chunk_store():
    """Elasticsearch chunk fulltext; None if disabled or unreachable."""
    try:
        from openrag.search.es_chunk_store import create_es_chunk_store_from_config

        return create_es_chunk_store_from_config()
    except Exception as exc:
        _logger.warning("Elasticsearch chunk store unavailable: %s", exc)
        return None


# Configure SessionLocal with engine
SessionLocal.configure(bind=get_engine())


class TaskWorker:
    """Task worker for pulling and executing tasks

    This worker runs as a separate process and continuously pulls tasks
    from the broker, executes them, and reports results.
    """

    def __init__(
        self,
        api_base_url: str = "http://localhost:8001",
        worker_id: Optional[str] = None,
        batch_size: int = 5,
        poll_interval: int = 5
    ):
        self.api_base_url = api_base_url.rstrip('/')
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        self.running = False
        self.embedding_engine = None
        self.vector_store = None
        self.layer_store = None
        self.parser_registry = None
        self.chunk_engine = None
        self.hierarchy_storage = None
        self.chunk_fulltext_store = None
        self.require_layer_vectors = False
        self.dependencies_ready = False
        self.next_dependency_probe_at = 0.0

    _MAX_PROCESSING_ERROR_LEN = 4096

    @staticmethod
    def _sanitize_processing_error(exc: BaseException) -> str:
        code = getattr(exc, "code", "PROCESSING_FAILED")
        message = getattr(exc, "public_message", "Document processing failed")
        return f"{code}: {message}"

    def _mark_file_failed(self, file_id: int, error: str) -> None:
        """Persist failed processing status and error text for a file (separate session)."""
        from openrag.models.file import File, ProcessingStatus

        msg = (error or "")[: self._MAX_PROCESSING_ERROR_LEN]
        db = SessionLocal()
        try:
            row = db.query(File).filter(File.id == file_id).first()
            if not row or row.is_directory:
                return
            row.processing_status = ProcessingStatus.failed
            row.processing_error = msg or None
            db.commit()
        finally:
            db.close()

    def _mark_file_pending_for_retry(self, file_id: int, error: str) -> None:
        from openrag.models.file import File, ProcessingStatus

        db = SessionLocal()
        try:
            row = db.query(File).filter(File.id == file_id).first()
            if not row or row.is_directory:
                return
            row.processing_status = ProcessingStatus.pending
            row.processing_error = error[: self._MAX_PROCESSING_ERROR_LEN]
            db.commit()
        finally:
            db.close()

    def _initialize_processing_dependencies(self) -> None:
        from openrag.retrieval.retrieval_service import l0_l1_retrieval_enabled

        self.embedding_engine = EmbeddingEngine()
        self.parser_registry = ParserRegistry()
        self.chunk_engine = ChunkEngine()
        self.hierarchy_storage = HierarchyStorage()
        self.chunk_fulltext_store = _create_es_chunk_store()
        self.require_layer_vectors = l0_l1_retrieval_enabled()
        self._preflight_processing_dependencies()

    def _preflight_processing_dependencies(self) -> bool:
        if self.embedding_engine is None:
            raise EmbeddingConfigurationError(
                "EMBEDDING_CONFIG_INVALID", "Embedding dependency is not initialized"
            )
        try:
            self.embedding_engine.probe()
            if self.vector_store is None:
                self.vector_store = _create_vector_store(self.embedding_engine)
            self.vector_store.probe()
            if self.require_layer_vectors and self.layer_store is None:
                self.layer_store = _create_layer_store(
                    self.embedding_engine, self.require_layer_vectors
                )
            if self.require_layer_vectors:
                self.layer_store.probe()
        except EmbeddingConfigurationError:
            raise
        except Exception as exc:
            self.dependencies_ready = False
            self.next_dependency_probe_at = (
                time.monotonic() + self.embedding_engine.config.probe_interval_seconds
            )
            _logger.warning(
                "embedding_probe_failure worker_id=%s error_code=%s",
                self.worker_id,
                getattr(exc, "code", "PROCESSING_DEPENDENCY_UNAVAILABLE"),
            )
            return False
        self.dependencies_ready = True
        self.next_dependency_probe_at = 0.0
        return True

    def _ensure_processing_dependencies_ready(self) -> bool:
        if self.dependencies_ready:
            return True
        if time.monotonic() < self.next_dependency_probe_at:
            return False
        return self._preflight_processing_dependencies()

    def _invalidate_embedding_readiness(self) -> None:
        self.dependencies_ready = False
        interval = (
            self.embedding_engine.config.probe_interval_seconds
            if self.embedding_engine is not None
            else 30
        )
        self.next_dependency_probe_at = time.monotonic() + interval

    def start(self):
        """Start the worker loop — pulls one task at a time, executes it, then polls again."""
        self.running = True
        self._initialize_processing_dependencies()
        print(f"Worker {self.worker_id} started")

        while self.running:
            try:
                if not self._ensure_processing_dependencies_ready():
                    time.sleep(self.poll_interval)
                    continue
                # Pull a single task from broker
                task = self._pull_one_task()

                if not task:
                    # No tasks available, wait before polling again
                    time.sleep(self.poll_interval)
                    continue

                # Execute the task
                self._execute_task(task)

            except Exception as e:
                print(f"Worker error: {e}")
                time.sleep(self.poll_interval)

    def stop(self):
        """Stop the worker"""
        self.running = False
        print(f"Worker {self.worker_id} stopped")

    def _pull_one_task(self) -> Optional[Dict[str, Any]]:
        """Pull a single task from broker

        Returns:
            Task dict if available, None otherwise
        """
        try:
            response = requests.get(
                f"{self.api_base_url}/broker/get-tasks",
                params={
                    "worker_id": self.worker_id,
                    "limit": 1
                },
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            tasks = data.get("tasks", [])
            return tasks[0] if tasks else None
        except requests.RequestException as e:
            print(f"Failed to pull tasks: {e}")
            return []

    def _execute_task(self, task: Dict[str, Any]):
        """Execute a single task

        Args:
            task: Task dict from broker
        """
        task_id = task["id"]
        print(f"Executing task {task_id}")
        set_trace_context(
            trace_id=task.get("trace_id") or uuid.uuid4().hex,
            trace_type="document_processing",
            workspace_id=task.get("workspace_id"),
            user_id=task.get("user_id"),
            file_id=task.get("file_id"),
            task_id=str(task_id),
            sampling_reason="worker_task",
        )

        # Start heartbeat process
        heartbeat_proc = multiprocessing.Process(
            target=_heartbeat_loop,
            args=(self.api_base_url, task_id),
        )
        heartbeat_proc.start()

        try:
            # Update task to started status
            self._update_task_status(task_id, "started", progress=0)

            task_type = task.get("task_type") or "process_document"
            if task_type == "delete_file":
                result = self._delete_file_task(task)
            elif task_type == "delete_path_prefix":
                result = self._delete_path_prefix_task(task)
            else:
                result = self._process_document(task, task_id)

            # Report success
            self._update_task_status(
                task_id,
                "success",
                progress=100,
                result=result
            )
            print(f"Task {task_id} completed successfully")

        except Exception as e:
            self._handle_task_failure(task, e)

        finally:
            # Stop heartbeat
            heartbeat_proc.terminate()
            heartbeat_proc.join(timeout=5)
            reset_trace_context()

    def _process_document(self, task: Dict[str, Any], task_id: int) -> Dict[str, Any]:
        """Process a document task

        Args:
            task: Task dict with file info

        Returns:
            Processing result
        """
        file_id = task.get("file_id")
        workspace_id = task.get("workspace_id")
        user_id = task.get("user_id")

        # Get database session
        db = SessionLocal()
        file_path = ""
        trace_service = TraceService(db)
        trace_started = False

        try:
            from openrag.models.file import File
            from openrag.models import TraceRun
            from openrag.services.workspace_service import WorkspaceService

            # Get file info
            file = db.query(File).filter(File.id == file_id).first()
            if not file:
                raise Exception(f"File {file_id} not found")

            # Get parser type from file record
            parser_type = file.parser_type if file.parser_type else "auto"
            document_type = normalize_document_type(
                getattr(file, "document_type", None)
            )

            # Get workspace
            ws_service = WorkspaceService(db)
            workspace = ws_service.get_workspace(workspace_id)
            if not workspace:
                raise Exception(f"Workspace {workspace_id} not found")

            ctx = get_trace_context()
            trace_id = ctx["trace_id"] or uuid.uuid4().hex
            if not db.query(TraceRun).filter(TraceRun.trace_id == trace_id).first():
                trace_service.start_run(
                    trace_id=trace_id,
                    trace_type="document_processing",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    file_id=file_id,
                    task_id=str(task_id),
                    sampling_reason=ctx["sampling_reason"] or "worker_task",
                )
            trace_started = True

            # Download file from MinIO
            minio_storage = MinioStorage()
            temp_dir = tempfile.mkdtemp()
            file_path = os.path.join(
                temp_dir,
                f"doc_{file_id}_{os.path.basename(file.uri)}"
            )
            download_span = None
            try:
                download_span = trace_service.start_span(
                    "worker.download_file",
                    input_summary={
                        "bucket": workspace.slug,
                        "object_key": file.uri,
                        "file_id": file_id,
                    },
                )
            except Exception:
                download_span = None
            try:
                minio_storage.get_file_to_path(workspace.slug, file.uri, file_path)
                if download_span is not None:
                    try:
                        trace_service.finish_span(
                            span_id=download_span.span_id,
                            output_summary={
                                "local_path": os.path.basename(file_path),
                                "size_bytes": os.path.getsize(file_path)
                                if os.path.exists(file_path)
                                else None,
                                "status": "downloaded",
                            },
                        )
                    except Exception:
                        pass
            except Exception as exc:
                if download_span is not None:
                    try:
                        trace_service.fail_span(
                            span_id=download_span.span_id,
                            error_message=str(exc),
                        )
                    except Exception:
                        pass
                raise

            # Create processor
            processor = DocumentProcessor(
                db=db,
                parser_registry=self.parser_registry,
                chunk_engine=self.chunk_engine,
                embedding_engine=self.embedding_engine,
                hierarchy_storage=self.hierarchy_storage,
                minio_storage=minio_storage,
                vector_store=self.vector_store,
                layer_store=self.layer_store,
                chunk_fulltext_store=self.chunk_fulltext_store,
                require_layer_vectors=self.require_layer_vectors,
            )

            # Process document
            print(f"  [DEBUG] Starting processing: file_id={file_id}, parser={parser_type}, path={file_path}")
            print(f"  [DEBUG] file.uri={file.uri}")

            def _on_progress(pct: int):
                self._update_task_status(task_id, "started", progress=pct)

            processing_result = processor.process_document(
                file_path=file_path,
                file_id=file_id,
                user_id=user_id,
                parser_type=parser_type,
                document_type=document_type,
                progress_callback=_on_progress,
            )
            print(f"  [DEBUG] Processing result: {processing_result}")

            # DocumentProcessor already updates file status, paths, and
            # commits inside process_document(). Only refresh parser_type
            # from the result if the processor chose a different one.
            file.parser_type = processing_result.get("parser_type", parser_type)
            db.commit()

            # A2: 自底向上更新祖先目录的 L0/L1（MinIO + Milvus layers）
            db.refresh(file)
            if not file.is_directory:
                try:
                    from openrag.hierarchy.parent_propagation import (
                        propagate_parent_directory_hierarchies,
                    )

                    n_dir = propagate_parent_directory_hierarchies(
                        db=db,
                        bucket=workspace.slug,
                        minio=minio_storage,
                        layer_store=self.layer_store,
                        embedding_engine=self.embedding_engine,
                        leaf_file_id=file_id,
                        hstorage=self.hierarchy_storage,
                    )
                    if n_dir:
                        print(f"  [DEBUG] Propagated directory L0/L1 for {n_dir} ancestor(s)")
                except Exception as exc:
                    _logger.warning("Directory hierarchy propagation failed: %s", exc)

            print(f"  [DEBUG] Final paths: l0={file.l0_path}, l1={file.l1_path}, l2={file.l2_path}")

            try:
                trace_service.finish_run()
            except Exception:
                pass

            return {
                "file_id": file_id,
                "status": "success",
                "parser_type": processing_result.get("parser_type", parser_type),
                "document_type": processing_result.get(
                    "document_type", document_type
                ),
                "l0_path": file.l0_path,
                "l1_path": file.l1_path,
                "l2_path": file.l2_path,
            }

        except Exception as exc:
            if trace_started:
                try:
                    trace_service.fail_run(
                        error_stage="document_processing",
                        error_message=str(exc),
                    )
                except Exception:
                    pass
            raise
        finally:
            db.close()
            # Cleanup temp file
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    os.rmdir(os.path.dirname(file_path))
                except Exception:
                    pass

    def _delete_file_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """后台删除文件（MinIO + Milvus + DB），与 API 同步删除语义一致。"""
        file_id = task.get("file_id")
        if not file_id:
            raise ValueError("delete_file task missing file_id")

        db = SessionLocal()
        try:
            from openrag.models.file import File
            from openrag.services.file_deletion import delete_file_with_storage
            from openrag.services.workspace_service import WorkspaceService

            file = db.query(File).filter(File.id == file_id).first()
            if not file:
                _logger.info("delete_file task: file %s already gone, skip", file_id)
                return {"file_id": file_id, "status": "skipped", "reason": "not_found"}
            if file.deleted_at is None:
                _logger.warning("delete_file task: file %s not soft-deleted, skip", file_id)
                return {"file_id": file_id, "status": "skipped", "reason": "active"}

            ws_service = WorkspaceService(db)
            workspace = ws_service.get_workspace(file.workspace_id)
            if not workspace:
                raise ValueError(f"Workspace {file.workspace_id} not found")

            delete_file_with_storage(db, file, workspace)
            return {"file_id": file_id, "status": "deleted"}
        finally:
            db.close()

    def _delete_path_prefix_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """异步按路径前缀级联删除（与 API 同步删除语义一致，逐条复用 delete_file_with_storage）。"""
        workspace_id = task.get("workspace_id")
        payload = task.get("payload") or {}
        path = payload.get("path")
        if not workspace_id or not path:
            raise ValueError("delete_path_prefix task missing workspace_id or payload.path")

        # Codex round-4 #1: never let a root/empty/"/" prefix trigger workspace-wide
        # physical cleanup, even from a legacy/manual/malformed task. Guard BEFORE opening
        # a session (no DB needed to reject); skip gracefully so the worker doesn't crash
        # on a poisoned task. The helper raises ValueError too as a second line of defense.
        if not path or path.rstrip("/") == "":
            _logger.error(
                "delete_path_prefix task with root/empty path=%r; skipping workspace-wide "
                "cleanup (workspace_id=%s)", path, workspace_id,
            )
            return {
                "workspace_id": workspace_id,
                "path": path,
                "deleted_count": 0,
                "deleted_ids": [],
                "status": "skipped",
                "reason": "root_prefix",
            }

        db = SessionLocal()
        try:
            from datetime import datetime
            from openrag.services.file_deletion import (
                physically_delete_soft_deleted_under_prefix,
            )
            from openrag.services.workspace_service import WorkspaceService

            ws_service = WorkspaceService(db)
            workspace = ws_service.get_workspace(workspace_id)
            if not workspace:
                raise ValueError(f"Workspace {workspace_id} not found")

            deleted_before_raw = payload.get("deleted_before")
            if not deleted_before_raw:
                # Codex #1: fail closed. A prefix task without a watermark must NOT
                # fall back to prefix-wide physical delete (that would drop active
                # rows created after enqueue). Phase 2 always writes deleted_before;
                # a task missing it is malformed/legacy — skip and let an operator requeue.
                _logger.error(
                    "delete_path_prefix task missing payload.deleted_before; skipping "
                    "(workspace_id=%s path=%s)", workspace_id, path,
                )
                return {
                    "workspace_id": workspace_id,
                    "path": path,
                    "deleted_count": 0,
                    "deleted_ids": [],
                    "status": "skipped",
                    "reason": "missing_deleted_before",
                }

            deleted_before = datetime.fromisoformat(deleted_before_raw)
            deleted_ids = physically_delete_soft_deleted_under_prefix(
                db, workspace_id, path, deleted_before, workspace
            )
            return {
                "workspace_id": workspace_id,
                "path": path,
                "deleted_count": len(deleted_ids),
                "deleted_ids": deleted_ids,
                "status": "deleted",
            }
        finally:
            db.close()

    @staticmethod
    def _task_retry_delay(retry_count: int) -> int:
        delays = (30, 120, 300)
        return delays[min(max(retry_count, 0), len(delays) - 1)]

    def _handle_task_failure(self, task: Dict[str, Any], exc: BaseException) -> None:
        task_id = task["id"]
        task_type = task.get("task_type") or "process_document"
        file_id = task.get("file_id")
        error = self._sanitize_processing_error(exc)
        error_code = getattr(exc, "code", "PROCESSING_FAILED")
        retryable = bool(getattr(exc, "retryable", False))
        retry_count = int(task.get("retry_count") or 0)
        max_retries = int(task.get("max_retries") or 0)

        if isinstance(exc, EmbeddingProviderError):
            self._invalidate_embedding_readiness()

        if retryable and retry_count < max_retries:
            delay = self._task_retry_delay(retry_count)
            if task_type == "process_document" and file_id is not None:
                self._mark_file_pending_for_retry(int(file_id), error)
            self._update_task_status(
                task_id,
                "retry",
                progress=0,
                error=error,
                error_code=error_code,
                error_retryable=True,
                next_retry_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
            )
            _logger.warning(
                "document_embedding_failed task_id=%s error_code=%s retryable=true retry_delay_seconds=%d",
                task_id,
                error_code,
                delay,
            )
            return

        if task_type == "process_document" and file_id is not None:
            self._mark_file_failed(int(file_id), error)
        self._update_task_status(
            task_id,
            "failure",
            error=error,
            error_code=error_code,
            error_retryable=retryable,
        )
        _logger.warning(
            "document_embedding_failed task_id=%s error_code=%s retryable=%s final=true",
            task_id,
            error_code,
            retryable,
        )

    def _update_task_status(
        self,
        task_id: int,
        status: str,
        progress: Optional[int] = None,
        result: Optional[Dict] = None,
        error: Optional[str] = None,
        error_code: Optional[str] = None,
        error_retryable: Optional[bool] = None,
        next_retry_at: Optional[datetime] = None,
    ):
        """Update task status via API

        This is a placeholder - in production, this should call the tasks API
        """
        # For now, we update directly in database
        # In production, this should be an API call
        db = SessionLocal()
        try:
            from openrag.models.task import Task, TaskStatus

            task = db.query(Task).filter(Task.id == task_id).first()
            if task:
                task.status = TaskStatus(status)
                if progress is not None:
                    task.progress = max(task.progress, min(100, progress))
                if result:
                    task.result = result
                if error:
                    task.error = error
                if error_code is not None:
                    task.error_code = error_code
                if error_retryable is not None:
                    task.error_retryable = error_retryable

                if status == "success":
                    task.completed_at = datetime.now(timezone.utc)
                    task.error = None
                    task.error_code = None
                    task.error_retryable = False
                    task.next_retry_at = None
                elif status == "retry":
                    task.retry_count += 1
                    task.progress = 0
                    task.next_retry_at = next_retry_at
                    task.worker_id = None
                    task.assigned_at = None
                    task.heartbeat_at = None
                    task.completed_at = None
                elif status == "failure":
                    task.completed_at = datetime.now(timezone.utc)

                db.commit()
        finally:
            db.close()


def _heartbeat_loop(api_base_url: str, task_id: int, interval: int = 30):
    """Heartbeat loop running in separate process"""
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    while True:
        try:
            response = requests.post(
                f"{api_base_url}/broker/heartbeat/{task_id}",
                timeout=10
            )
            if response.status_code != 200:
                print(f"Heartbeat failed for task {task_id}: {response.status_code}")
        except Exception as e:
            print(f"Heartbeat error for task {task_id}: {e}")

        time.sleep(interval)


def run_workers(
    api_base_url: str,
    num_workers: int = 4,
    worker_id_prefix: Optional[str] = None,
    batch_size: int = 5,
    poll_interval: int = 5,
) -> List[multiprocessing.Process]:
    """Start multiple worker processes.

    Args:
        api_base_url: API base URL
        num_workers: Number of worker processes to spawn
        worker_id_prefix: Optional prefix for worker IDs (default: "worker")
        batch_size: Number of tasks to pull per request
        poll_interval: Seconds to wait between polls when no tasks

    Returns:
        List of started processes
    """
    processes: List[multiprocessing.Process] = []

    for i in range(num_workers):
        worker_id = f"{worker_id_prefix or 'worker'}-{uuid.uuid4().hex[:8]}"
        p = multiprocessing.Process(
            target=_worker_process,
            args=(api_base_url, worker_id, batch_size, poll_interval),
            name=f"TaskWorker-{worker_id}",
        )
        p.start()
        processes.append(p)
        print(f"Started worker {worker_id} (pid={p.pid})")

    return processes


def _worker_process(
    api_base_url: str,
    worker_id: str,
    batch_size: int,
    poll_interval: int,
):
    """Entry point for a single worker process."""
    # Register SIGTERM handler so child processes also shut down gracefully
    signal.signal(signal.SIGTERM, _sigterm_handler)

    worker = TaskWorker(
        api_base_url=api_base_url,
        worker_id=worker_id,
        batch_size=batch_size,
        poll_interval=poll_interval,
    )
    try:
        worker.start()
    except KeyboardInterrupt:
        worker.stop()
    except BaseException as exc:
        _logger.critical("Worker %s crashed with unhandled %s: %s", worker_id, type(exc).__name__, exc)
        raise


def _sigterm_handler(signum, frame):
    """Handle SIGTERM gracefully — raise KeyboardInterrupt so the
    main loop's except KeyboardInterrupt can perform orderly shutdown."""
    raise KeyboardInterrupt("Received SIGTERM")


def main():
    parser = argparse.ArgumentParser(description="OpenRag Task Worker")
    parser.add_argument(
        "--api-url",
        type=str,
        default=os.environ.get("WORKER_API_URL", "http://localhost:8001"),
        help="API server base URL (default: http://localhost:8001)",
    )
    parser.add_argument(
        "-n", "--num-workers",
        type=int,
        default=int(os.environ.get("WORKER_NUM", 4)),
        help="Number of worker processes (default: 4)",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default=os.environ.get("WORKER_PREFIX", "worker"),
        help="Worker ID prefix (default: worker)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5,
        help="Tasks to pull per request (default: 5)",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=5,
        help="Seconds between polls when idle (default: 5)",
    )
    args = parser.parse_args()

    # Handle graceful shutdown
    signal.signal(signal.SIGTERM, _sigterm_handler)
    signal.signal(signal.SIGINT, _sigterm_handler)

    processes = run_workers(
        api_base_url=args.api_url,
        num_workers=args.num_workers,
        worker_id_prefix=args.prefix,
        batch_size=args.batch_size,
        poll_interval=args.poll_interval,
    )

    print(f"All {len(processes)} workers started. Press Ctrl+C to stop.")

    try:
        # Monitor processes and restart any that crash
        while True:
            time.sleep(5)
            for i, p in enumerate(processes):
                if not p.is_alive():
                    exitcode = p.exitcode
                    if exitcode is not None:
                        if exitcode < 0:
                            sig = -exitcode
                            sig_name = signal.Signals(sig).name if sig < len(signal.Signals) else f"signal-{sig}"
                            _logger.error("Worker %s died with signal %s (exitcode=%d), restarting...", p.name, sig_name, exitcode)
                        else:
                            _logger.error("Worker %s died with exit code %d, restarting...", p.name, exitcode)
                    else:
                        _logger.error("Worker %s died (exitcode=None), restarting...", p.name)
                    new_p = multiprocessing.Process(
                        target=_worker_process,
                        args=(args.api_url, f"{args.prefix}-{uuid.uuid4().hex[:8]}", args.batch_size, args.poll_interval),
                        name=f"TaskWorker-restart-{i}",
                    )
                    new_p.start()
                    processes[i] = new_p
    except KeyboardInterrupt:
        print("Shutting down workers...")
        for p in processes:
            p.terminate()
        for p in processes:
            p.join(timeout=10)
            if p.is_alive():
                p.kill()
        print("All workers stopped.")


if __name__ == "__main__":
    main()
