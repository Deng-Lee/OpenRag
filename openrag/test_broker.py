"""Test script for TaskBroker"""

import sys
import time
import uuid
import threading
from datetime import datetime, timedelta

# Add src to path
sys.path.insert(0, 'e:/project/OpenRag/openrag/src')

from openrag.config import get_config
from openrag.database import SessionLocal, init_db, get_engine
from openrag.broker import TaskBroker
from openrag.models.task import Task, TaskStatus
from openrag.models.workspace import Workspace
from openrag.models.user import User

# Initialize database
config = get_config()
init_db()

# Configure SessionLocal with engine
SessionLocal.configure(bind=get_engine())


def setup_test_data():
    """Create test workspace and user"""
    db = SessionLocal()
    try:
        # Check if test user exists
        user = db.query(User).filter(User.email == "test@example.com").first()
        if not user:
            user = User(
                email="test@example.com",
                username="testuser",
                password_hash="testpass",
                full_name="Test User",
                is_active=True,
                is_admin=False
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f"✓ Created test user: {user.id}")
        else:
            print(f"✓ Test user exists: {user.id}")

        # Check if test workspace exists
        ws = db.query(Workspace).filter(Workspace.slug == "test-ws").first()
        if not ws:
            ws = Workspace(
                name="Test Workspace",
                slug="test-ws",
                owner_id=user.id,
                max_concurrent_tasks=3,  # 并发上限3
                max_storage_bytes=10737418240
            )
            db.add(ws)
            db.commit()
            db.refresh(ws)
            print(f"✓ Created test workspace: {ws.id} (max_concurrent={ws.max_concurrent_tasks})")
        else:
            print(f"✓ Test workspace exists: {ws.id} (max_concurrent={ws.max_concurrent_tasks})")

        return user.id, ws.id
    finally:
        db.close()


def create_test_tasks(user_id: int, workspace_id: int, count: int = 10):
    """Create test tasks"""
    db = SessionLocal()
    try:
        # Clear existing test tasks
        db.query(Task).filter(Task.workspace_id == workspace_id).delete()
        db.commit()

        tasks = []
        for i in range(count):
            task = Task(
                task_id=f"test-task-{uuid.uuid4().hex[:8]}",
                workspace_id=workspace_id,
                user_id=user_id,
                file_id=None,  # No file reference for testing
                task_type="process_document",
                queue="normal",
                priority=5,
                status=TaskStatus.PENDING,
                progress=0,
                retry_count=0,
                max_retries=3
            )
            db.add(task)
            tasks.append(task)

        db.commit()
        print(f"✓ Created {count} test tasks")
        return [t.id for t in tasks]
    finally:
        db.close()


def test_broker_basic():
    """Test basic broker functionality"""
    print("\n=== Test 1: Basic Task Assignment ===")

    db = SessionLocal()
    try:
        broker = TaskBroker(db)

        # Worker 1 pulls tasks
        tasks1 = broker.get_tasks("worker-1", limit=5)
        print(f"✓ Worker-1 got {len(tasks1)} tasks")
        for t in tasks1:
            print(f"  - Task {t.id}: status={t.status}, worker={t.worker_id}")

        # Worker 2 pulls tasks (should get remaining)
        tasks2 = broker.get_tasks("worker-2", limit=5)
        print(f"✓ Worker-2 got {len(tasks2)} tasks")
        for t in tasks2:
            print(f"  - Task {t.id}: status={t.status}, worker={t.worker_id}")

        return len(tasks1) + len(tasks2)
    finally:
        db.close()


def test_concurrent_quota():
    """Test concurrent quota control"""
    print("\n=== Test 2: Concurrent Quota Control ===")

    db = SessionLocal()
    try:
        # Get workspace max concurrent
        ws = db.query(Workspace).filter(Workspace.slug == "test-ws").first()
        max_concurrent = ws.max_concurrent_tasks
        print(f"Workspace max_concurrent: {max_concurrent}")

        # Check assigned tasks
        assigned_count = db.query(Task).filter(
            Task.workspace_id == ws.id,
            Task.status.in_([TaskStatus.ASSIGNED.value, TaskStatus.STARTED.value])
        ).count()
        print(f"Currently assigned tasks: {assigned_count}")

        # Try to get more tasks
        broker = TaskBroker(db)
        tasks = broker.get_tasks("worker-3", limit=5)
        print(f"✓ Worker-3 got {len(tasks)} tasks (should respect quota)")

        return len(tasks)
    finally:
        db.close()


def test_heartbeat():
    """Test heartbeat update"""
    print("\n=== Test 3: Heartbeat Update ===")

    db = SessionLocal()
    try:
        # Get an assigned task
        task = db.query(Task).filter(
            Task.status == TaskStatus.ASSIGNED.value
        ).first()

        if not task:
            print("✗ No assigned task found")
            return False

        old_heartbeat = task.heartbeat_at
        print(f"Task {task.id} old heartbeat: {old_heartbeat}")

        # Update heartbeat
        broker = TaskBroker(db)
        broker.update_heartbeat(task.id)

        # Refresh and check
        db.refresh(task)
        new_heartbeat = task.heartbeat_at
        print(f"Task {task.id} new heartbeat: {new_heartbeat}")

        if new_heartbeat and (not old_heartbeat or new_heartbeat > old_heartbeat):
            print("✓ Heartbeat updated successfully")
            return True
        else:
            print("✗ Heartbeat not updated")
            return False
    finally:
        db.close()


def test_timeout_recovery():
    """Test timeout task recovery"""
    print("\n=== Test 4: Timeout Recovery ===")

    db = SessionLocal()
    try:
        # Manually set a task as timeout (11 minutes ago)
        task = db.query(Task).filter(
            Task.status == TaskStatus.ASSIGNED.value
        ).first()

        if not task:
            print("✗ No assigned task found")
            return 0

        # Set heartbeat to 11 minutes ago
        task.heartbeat_at = datetime.now() - timedelta(minutes=11)
        db.commit()
        print(f"✓ Set task {task.id} heartbeat to 11 minutes ago")

        # Run recovery
        broker = TaskBroker(db)
        recovered = broker.recover_timeout_tasks()
        print(f"✓ Recovered {recovered} timeout tasks")

        # Check task status
        db.refresh(task)
        print(f"Task {task.id} status after recovery: {task.status}")

        return recovered
    finally:
        db.close()


def test_dynamic_weights():
    """Test dynamic weight calculation"""
    print("\n=== Test 5: Dynamic Weights ===")

    db = SessionLocal()
    try:
        broker = TaskBroker(db)

        # Get initial weights
        weights = broker.get_workspace_weights()
        print(f"Current weights: {weights}")

        # Pull tasks multiple times to see weight decay
        for i in range(3):
            tasks = broker.get_tasks(f"worker-test-{i}", limit=2)
            weights = broker.get_workspace_weights()
            print(f"After pull {i+1}: weights={weights}")

        return True
    finally:
        db.close()


def test_concurrent_workers():
    """Test concurrent worker access"""
    print("\n=== Test 6: Concurrent Workers ===")

    results = []
    lock = threading.Lock()

    def worker_pull(worker_id: str):
        # Each thread needs its own session
        from openrag.database import SessionLocal as NewSessionLocal
        db = NewSessionLocal()
        try:
            broker = TaskBroker(db)
            tasks = broker.get_tasks(worker_id, limit=3)
            with lock:
                results.append((worker_id, len(tasks), [t.id for t in tasks]))
        finally:
            db.close()

    # Reset all tasks to pending
    db = SessionLocal()
    try:
        db.query(Task).update({
            "status": TaskStatus.PENDING.value,
            "worker_id": None,
            "assigned_at": None,
            "heartbeat_at": None
        })
        db.commit()
        print("✓ Reset all tasks to pending")
    finally:
        db.close()

    # Create 5 concurrent workers
    threads = []
    for i in range(5):
        t = threading.Thread(target=worker_pull, args=(f"concurrent-worker-{i}",))
        threads.append(t)

    # Start all threads simultaneously
    for t in threads:
        t.start()

    # Wait for completion
    for t in threads:
        t.join()

    # Check results
    total_assigned = sum(r[1] for r in results)
    all_task_ids = []
    for r in results:
        all_task_ids.extend(r[2])
        print(f"  {r[0]}: got {r[1]} tasks")

    # Check for duplicates
    duplicates = len(all_task_ids) - len(set(all_task_ids))
    if duplicates == 0:
        print(f"✓ No duplicate assignments! Total assigned: {total_assigned}")
    else:
        print(f"✗ Found {duplicates} duplicate assignments!")

    return duplicates == 0


def main():
    print("=" * 60)
    print("TaskBroker Test Suite")
    print("=" * 60)

    # Setup
    print("\n--- Setup ---")
    user_id, ws_id = setup_test_data()
    task_ids = create_test_tasks(user_id, ws_id, count=10)

    # Run tests
    try:
        test_broker_basic()
        test_concurrent_quota()
        test_heartbeat()
        test_timeout_recovery()
        test_dynamic_weights()
        test_concurrent_workers()

        print("\n" + "=" * 60)
        print("All tests completed!")
        print("=" * 60)

    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
