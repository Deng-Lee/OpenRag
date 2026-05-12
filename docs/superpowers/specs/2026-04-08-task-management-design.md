---
name: 任务管理和并发控制系统设计
description: 业务空间隔离、可配置优先级策略、并发控制、任务重试机制
type: feature-design
date: 2026-04-08
---

# 任务管理和并发控制系统设计

## 1. 概述

### 1.1 目标

为 OpenRag 系统添加完善的任务管理和并发控制能力，支持：

1. **业务空间隔离** - 数据、权限、任务按业务空间完全隔离
2. **并发控制** - 全局、业务空间、队列三层并发限制
3. **可配置优先级策略** - 支持多种优先级策略，可动态切换
4. **任务管理** - 完整的任务生命周期管理和状态追踪
5. **自动重试** - 失败任务自动重试，指数退避策略
6. **永久保存** - 任务状态和结果永久保存，支持审计追踪

### 1.2 核心特性

- ✅ 业务空间（Workspace）多租户隔离
- ✅ 用户可加入多个业务空间，每个空间独立配额
- ✅ 三层并发控制（全局/空间/队列）
- ✅ 可插拔优先级策略系统
- ✅ 基于文件大小的默认优先级策略（可替换）
- ✅ 自动重试 + 指数退避（1min → 5min → 15min）
- ✅ 任务状态永久保存到数据库
- ✅ RESTful 任务管理 API
- ✅ Redis 缓存配额统计，减少数据库压力

## 2. 业务空间（Workspace）架构

### 2.1 核心概念

**业务空间** = 项目/团队级别的资源隔离单元

- 用户可以加入多个业务空间
- 每个空间有独立的文件、权限、任务配额
- 类似 GitHub 的 organization 模式

### 2.2 数据模型

#### Workspace 模型

```python
class Workspace(Base):
    __tablename__ = 'workspaces'
    
    id: int = Column(Integer, primary_key=True)
    name: str = Column(String(255), nullable=False)
    slug: str = Column(String(100), unique=True, nullable=False)  # URL 友好标识
    description: str = Column(Text)
    owner_id: int = Column(Integer, ForeignKey('users.id'))
    
    # 配额配置
    max_concurrent_tasks: int = Column(Integer, default=10)
    max_storage_bytes: int = Column(BigInteger, default=10*1024*1024*1024)  # 10GB
    priority_strategy: str = Column(String(50), default='file_size')
    
    created_at: datetime = Column(DateTime, default=datetime.utcnow)
    updated_at: datetime = Column(DateTime, onupdate=datetime.utcnow)
```

#### WorkspaceMember 模型

```python
class WorkspaceMember(Base):
    __tablename__ = 'workspace_members'
    
    id: int = Column(Integer, primary_key=True)
    workspace_id: int = Column(Integer, ForeignKey('workspaces.id'))
    user_id: int = Column(Integer, ForeignKey('users.id'))
    role: str = Column(String(20), default='member')  # admin, member, viewer
    joined_at: datetime = Column(DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        UniqueConstraint('workspace_id', 'user_id'),
    )
```

### 2.3 现有模型改造

所有现有模型添加 `workspace_id` 外键，实现数据隔离：

**需要改造的模型**：
- `File` - 文件属于某个业务空间
- `Team` - 团队属于某个业务空间
- `Permission` - 权限在空间内管理
- `Share` - 分享链接属于某个业务空间
- `Task`（新增）- 任务属于某个业务空间

**迁移策略**：
1. 添加 `workspace_id` 列（允许 NULL）
2. 创建默认业务空间，将现有数据迁移到默认空间
3. 将 `workspace_id` 设置为 NOT NULL

### 2.4 API 变化

所有 API 路径添加业务空间上下文：

**方式一：路径参数**（推荐）
```
POST /workspaces/{workspace_id}/files/upload
GET  /workspaces/{workspace_id}/files
GET  /workspaces/{workspace_id}/tasks
```

**方式二：Header 传递**
```
X-Workspace-ID: 123
```

**业务空间管理 API**：
```
POST   /workspaces                    # 创建业务空间
GET    /workspaces                    # 列出用户的所有空间
GET    /workspaces/{workspace_id}     # 获取空间详情
PUT    /workspaces/{workspace_id}     # 更新空间配置
DELETE /workspaces/{workspace_id}     # 删除空间

POST   /workspaces/{workspace_id}/members        # 添加成员
GET    /workspaces/{workspace_id}/members        # 列出成员
DELETE /workspaces/{workspace_id}/members/{user_id}  # 移除成员
```

## 3. 任务管理系统

### 3.1 Task 模型

```python
class Task(Base):
    __tablename__ = 'tasks'
    
    id: int = Column(Integer, primary_key=True)
    task_id: str = Column(String(255), unique=True, nullable=False)  # Celery task ID
    workspace_id: int = Column(Integer, ForeignKey('workspaces.id'), nullable=False)
    user_id: int = Column(Integer, ForeignKey('users.id'), nullable=False)
    file_id: int = Column(Integer, ForeignKey('files.id'), nullable=True)
    
    # 任务信息
    task_type: str = Column(String(50), nullable=False)  # process_document, reindex, etc.
    queue: str = Column(String(20), nullable=False)      # fast, normal, slow
    priority: int = Column(Integer, default=5)           # 0-10 优先级分数
    
    # 状态信息
    status: str = Column(String(20), default='pending')  # pending, started, success, failure, retry, cancelled
    progress: int = Column(Integer, default=0)           # 0-100
    retry_count: int = Column(Integer, default=0)
    max_retries: int = Column(Integer, default=3)
    
    # 时间信息
    created_at: datetime = Column(DateTime, default=datetime.utcnow)
    started_at: datetime = Column(DateTime, nullable=True)
    completed_at: datetime = Column(DateTime, nullable=True)
    
    # 结果信息
    result: dict = Column(JSON, nullable=True)           # 处理结果
    error: str = Column(Text, nullable=True)             # 错误信息
    traceback: str = Column(Text, nullable=True)         # 错误堆栈
    
    # 索引
    __table_args__ = (
        Index('idx_workspace_status', 'workspace_id', 'status'),
        Index('idx_user_created', 'user_id', 'created_at'),
    )
```

### 3.2 Celery 配置扩展

**celery_config.py 完整配置**：

```python
"""Celery configuration for OpenRag"""

# Broker and backend configuration
broker_url = 'redis://localhost:6379/0'
result_backend = 'redis://localhost:6379/0'

# Task settings
task_serializer = 'json'
result_serializer = 'json'
accept_content = ['json']
timezone = 'UTC'
enable_utc = True

# Task result settings
result_expires = None  # 永久保存（由数据库管理）

# Task execution settings
task_track_started = True
task_time_limit = 3600  # 1 hour max per task
task_soft_time_limit = 3000  # 50 minutes soft limit

# Worker 并发配置
worker_concurrency = 4  # 每个 worker 4 个进程
worker_prefetch_multiplier = 1  # 每次只预取 1 个任务，避免阻塞
worker_max_tasks_per_child = 100  # 每个子进程处理 100 个任务后重启

# 队列路由配置
task_routes = {
    'openrag.process_document_async': {
        'queue': 'default',  # 运行时动态路由到 fast/normal/slow
    }
}

# 重试配置
task_autoretry_for = (Exception,)
task_retry_kwargs = {'max_retries': 3}
task_retry_backoff = True  # 指数退避
task_retry_backoff_max = 900  # 最大 15 分钟

# 任务确认模式
task_acks_late = True  # 任务完成后才确认
task_reject_on_worker_lost = True
```

### 3.3 队列策略（可替换设计）

**三个优先级队列**：
- `fast` - 小文件（<1MB），高优先级
- `normal` - 中等文件（1-10MB），普通优先级
- `slow` - 大文件（>10MB），低优先级

**Worker 启动命令**：
```bash
# 启动 fast 队列 worker（2个进程）
celery -A openrag.tasks.celery_tasks worker -Q fast -c 2 -n fast@%h

# 启动 normal 队列 worker（4个进程）
celery -A openrag.tasks.celery_tasks worker -Q normal -c 4 -n normal@%h

# 启动 slow 队列 worker（2个进程）
celery -A openrag.tasks.celery_tasks worker -Q slow -c 2 -n slow@%h
```

### 3.4 任务管理 API

**tasks_api.py 端点设计**：

```python
# 查询任务
GET    /workspaces/{workspace_id}/tasks
       # 查询参数：status, user_id, skip, limit
       # 返回：任务列表 + 分页信息

GET    /workspaces/{workspace_id}/tasks/{task_id}
       # 返回：任务详情

GET    /workspaces/{workspace_id}/tasks/stats
       # 返回：任务统计（总数、运行中、成功、失败等）

# 任务操作
POST   /workspaces/{workspace_id}/tasks/{task_id}/cancel
       # 取消运行中的任务

POST   /workspaces/{workspace_id}/tasks/{task_id}/retry
       # 手动重试失败任务

# 配额管理
GET    /workspaces/{workspace_id}/quota
       # 返回：配额使用情况

PUT    /workspaces/{workspace_id}/quota
       # 更新配额配置（仅管理员）
```

**响应格式示例**：

任务详情：
```json
{
  "id": 123,
  "task_id": "abc-123-def-456",
  "workspace_id": 1,
  "user_id": 10,
  "file_id": 456,
  "task_type": "process_document",
  "queue": "normal",
  "priority": 5,
  "status": "started",
  "progress": 45,
  "retry_count": 0,
  "max_retries": 3,
  "created_at": "2026-04-08T10:00:00Z",
  "started_at": "2026-04-08T10:00:05Z",
  "completed_at": null,
  "result": null,
  "error": null
}
```

配额使用情况：
```json
{
  "workspace_id": 1,
  "max_concurrent_tasks": 10,
  "running_tasks": 5,
  "pending_tasks": 3,
  "total_tasks_today": 150,
  "storage_used_bytes": 5368709120,
  "storage_limit_bytes": 10737418240
}
```

## 4. 并发控制机制

### 4.1 三层并发限制

**层级结构**：
1. **全局限制** - 整个系统最大并发数（通过 worker 数量控制）
2. **业务空间限制** - 每个空间的 `max_concurrent_tasks`
3. **队列限制** - 每个队列独立的 worker 数量

### 4.2 配额检查流程

**任务提交时检查**：

```python
def check_workspace_quota(workspace_id: int) -> bool:
    """检查业务空间是否达到并发上限"""
    
    # 1. 从 Redis 获取当前运行任务数（快速）
    cache_key = f"workspace:{workspace_id}:running_tasks"
    running_tasks = redis_client.get(cache_key)
    
    if running_tasks is None:
        # 缓存未命中，从数据库查询
        running_tasks = db.query(Task).filter(
            Task.workspace_id == workspace_id,
            Task.status.in_(['pending', 'started', 'retry'])
        ).count()
        
        # 写入缓存（TTL 60秒）
        redis_client.setex(cache_key, 60, running_tasks)
    
    # 2. 获取空间配额
    workspace = db.query(Workspace).get(workspace_id)
    
    # 3. 检查是否超限
    if int(running_tasks) >= workspace.max_concurrent_tasks:
        raise QuotaExceededError(
            f"Workspace {workspace_id} reached max concurrent tasks "
            f"({workspace.max_concurrent_tasks})"
        )
    
    return True
```

### 4.3 配额计数管理

**增加计数**（任务开始时）：
```python
def increment_workspace_running_tasks(workspace_id: int):
    cache_key = f"workspace:{workspace_id}:running_tasks"
    redis_client.incr(cache_key)
    redis_client.expire(cache_key, 300)  # 5分钟过期
```

**减少计数**（任务完成/失败/取消时）：
```python
def decrement_workspace_running_tasks(workspace_id: int):
    cache_key = f"workspace:{workspace_id}:running_tasks"
    redis_client.decr(cache_key)
```

## 5. 优先级策略系统（可插拔设计）

### 5.1 设计原则

**可替换性**：优先级策略设计为可插拔模块，方便后续切换到其他策略（用户角色、文件类型、混合策略等）。

### 5.2 策略接口

**抽象基类**：

```python
from abc import ABC, abstractmethod
from typing import Tuple

class PriorityStrategy(ABC):
    """优先级策略基类"""
    
    @abstractmethod
    def calculate_priority(
        self, 
        file_size: int,
        file_type: str,
        user: User,
        workspace: Workspace
    ) -> Tuple[str, int]:
        """
        计算任务优先级
        
        Args:
            file_size: 文件大小（字节）
            file_type: 文件 MIME 类型
            user: 提交任务的用户
            workspace: 所属业务空间
        
        Returns:
            (queue_name, priority_score)
            - queue_name: 'fast', 'normal', 'slow'
            - priority_score: 0-10 的优先级分数
        """
        pass
    
    @abstractmethod
    def get_strategy_name(self) -> str:
        """返回策略名称"""
        pass
```

### 5.3 内置策略实现

**文件大小策略**（默认）：

```python
class FileSizeStrategy(PriorityStrategy):
    """基于文件大小的优先级策略"""
    
    def calculate_priority(
        self, 
        file_size: int,
        file_type: str,
        user: User,
        workspace: Workspace
    ) -> Tuple[str, int]:
        
        if file_size < 1_000_000:  # <1MB
            return ('fast', 8)
        elif file_size < 10_000_000:  # <10MB
            return ('normal', 5)
        else:  # >=10MB
            return ('slow', 2)
    
    def get_strategy_name(self) -> str:
        return 'file_size'
```

**预留策略接口**（后续实现）：

```python
class UserRoleStrategy(PriorityStrategy):
    """基于用户角色的优先级策略（预留）"""
    pass

class FileTypeStrategy(PriorityStrategy):
    """基于文件类型的优先级策略（预留）"""
    pass

class HybridStrategy(PriorityStrategy):
    """混合策略：综合文件大小、用户角色、文件类型（预留）"""
    pass
```

### 5.4 策略注册表

**策略管理器**：

```python
class PriorityStrategyRegistry:
    """策略注册表，支持动态切换"""
    
    _strategies: Dict[str, Type[PriorityStrategy]] = {
        'file_size': FileSizeStrategy,
        # 预留其他策略
        # 'user_role': UserRoleStrategy,
        # 'file_type': FileTypeStrategy,
        # 'hybrid': HybridStrategy,
    }
    
    @classmethod
    def register(cls, name: str, strategy_class: Type[PriorityStrategy]):
        """注册新策略"""
        cls._strategies[name] = strategy_class
    
    @classmethod
    def get_strategy(cls, strategy_name: str) -> PriorityStrategy:
        """获取策略实例"""
        if strategy_name not in cls._strategies:
            raise ValueError(f"Unknown priority strategy: {strategy_name}")
        return cls._strategies[strategy_name]()
    
    @classmethod
    def list_strategies(cls) -> List[str]:
        """列出所有可用策略"""
        return list(cls._strategies.keys())
```

### 5.5 使用方式

**在文件上传时计算优先级**：

```python
# 从 Workspace 配置读取策略类型
strategy = PriorityStrategyRegistry.get_strategy(
    workspace.priority_strategy  # 默认 'file_size'
)

# 计算优先级
queue, priority = strategy.calculate_priority(
    file_size=file.size,
    file_type=file.content_type,
    user=current_user,
    workspace=workspace
)

# 创建任务记录
task = Task(
    workspace_id=workspace.id,
    user_id=current_user.id,
    file_id=file.id,
    task_type='process_document',
    queue=queue,
    priority=priority,
    status='pending'
)
```

**切换策略**：

```python
# 方式1：更新 Workspace 配置
workspace.priority_strategy = 'user_role'  # 切换到用户角色策略
db.commit()

# 方式2：通过 API 更新
PUT /workspaces/{workspace_id}/quota
{
  "priority_strategy": "user_role"
}
```

## 6. 任务执行流程

### 6.1 完整流程图

```
1. 用户上传文件
   ↓
2. 创建 Task 记录（status=pending）
   ↓
3. 检查空间配额
   ↓ (未超限)
4. 计算优先级和队列（使用策略）
   ↓
5. 提交到 Celery 队列
   ↓
6. 更新 Task 状态（status=started）
   ↓
7. 增加空间运行任务计数（Redis）
   ↓
8. Worker 执行任务
   ├─ 成功 → status=success, 保存结果
   ├─ 失败 → status=retry, 自动重试（指数退避）
   └─ 重试失败 → status=failure, 保存错误信息
   ↓
9. 更新 Task 记录和完成时间
   ↓
10. 减少空间运行任务计数（Redis）
```

### 6.2 Celery 任务实现

**celery_tasks.py 完整实现**：

```python
from celery import Celery
from openrag.processors.document_processor import DocumentProcessor
from openrag.models.task import Task
from openrag.api.deps import get_db

app = Celery('openrag')
app.config_from_object('celery_config')

@app.task(
    bind=True, 
    name='openrag.process_document_async',
    autoretry_for=(Exception,),
    retry_kwargs={'max_retries': 3},
    retry_backoff=True,
    retry_backoff_max=900
)
def process_document_async(
    self,
    file_path: str,
    file_id: int,
    user_id: int,
    workspace_id: int
) -> dict:
    """异步处理文档"""
    
    db = next(get_db())
    task_record = None
    
    try:
        # 1. 获取任务记录
        task_record = db.query(Task).filter(
            Task.task_id == self.request.id
        ).first()
        
        if not task_record:
            raise ValueError(f"Task record not found: {self.request.id}")
        
        # 2. 更新状态为 started
        task_record.status = 'started'
        task_record.started_at = datetime.utcnow()
        db.commit()
        
        # 3. 增加空间运行任务计数
        increment_workspace_running_tasks(workspace_id)
        
        # 4. 执行实际处理
        processor = DocumentProcessor(db, parser_registry, chunk_engine, embedding_engine)
        result = processor.process_document(file_path, file_id, user_id)
        
        # 5. 更新状态为 success
        task_record.status = 'success'
        task_record.completed_at = datetime.utcnow()
        task_record.progress = 100
        task_record.result = result
        db.commit()
        
        return result
        
    except Exception as e:
        # 6. 更新重试计数
        if task_record:
            task_record.retry_count += 1
            
            if task_record.retry_count >= task_record.max_retries:
                # 超过最大重试次数，标记为失败
                task_record.status = 'failure'
                task_record.completed_at = datetime.utcnow()
                task_record.error = str(e)
                task_record.traceback = traceback.format_exc()
            else:
                # 标记为重试中
                task_record.status = 'retry'
            
            db.commit()
        
        raise
        
    finally:
        # 7. 减少空间运行任务计数
        decrement_workspace_running_tasks(workspace_id)
        db.close()
```

### 6.3 文件上传 API 集成

**files_api.py 修改**：

```python
@router.post("/upload", response_model=FileUploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    path: str = Form(default="/"),
    workspace_id: int = Form(...),  # 新增：业务空间 ID
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """上传文件并触发异步处理"""
    
    # 1. 验证用户是否属于该业务空间
    workspace = db.query(Workspace).get(workspace_id)
    if not workspace:
        raise HTTPException(404, "Workspace not found")
    
    member = db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace_id,
        WorkspaceMember.user_id == current_user.id
    ).first()
    if not member:
        raise HTTPException(403, "Not a member of this workspace")
    
    # 2. 检查空间配额
    try:
        check_workspace_quota(workspace_id)
    except QuotaExceededError as e:
        raise HTTPException(429, str(e))
    
    # 3. 保存文件（省略...）
    
    # 4. 计算优先级
    strategy = PriorityStrategyRegistry.get_strategy(workspace.priority_strategy)
    queue, priority = strategy.calculate_priority(
        file_size=file_size,
        file_type=file.content_type,
        user=current_user,
        workspace=workspace
    )
    
    # 5. 创建任务记录
    task_record = Task(
        task_id=str(uuid.uuid4()),  # 临时 ID，Celery 会覆盖
        workspace_id=workspace_id,
        user_id=current_user.id,
        file_id=file_record.id,
        task_type='process_document',
        queue=queue,
        priority=priority,
        status='pending'
    )
    db.add(task_record)
    db.commit()
    db.refresh(task_record)
    
    # 6. 提交到 Celery
    celery_task = process_document_async.apply_async(
        args=[str(storage_path), file_record.id, current_user.id, workspace_id],
        queue=queue,
        priority=priority
    )
    
    # 7. 更新任务记录的 Celery task ID
    task_record.task_id = celery_task.id
    db.commit()
    
    return FileUploadResponse(
        **file_record.__dict__,
        task_id=celery_task.id
    )
```

### 6.4 任务取消机制

**取消任务实现**：

```python
@router.post("/workspaces/{workspace_id}/tasks/{task_id}/cancel")
async def cancel_task(
    workspace_id: int,
    task_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """取消运行中的任务"""
    
    # 1. 获取任务记录
    task = db.query(Task).filter(
        Task.task_id == task_id,
        Task.workspace_id == workspace_id
    ).first()
    
    if not task:
        raise HTTPException(404, "Task not found")
    
    # 2. 检查权限（只有任务创建者或空间管理员可以取消）
    if task.user_id != current_user.id:
        member = db.query(WorkspaceMember).filter(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == current_user.id
        ).first()
        if not member or member.role != 'admin':
            raise HTTPException(403, "Permission denied")
    
    # 3. 检查任务状态
    if task.status not in ['pending', 'started', 'retry']:
        raise HTTPException(400, f"Cannot cancel task with status: {task.status}")
    
    # 4. 调用 Celery revoke
    from openrag.tasks.celery_tasks import app
    app.control.revoke(task_id, terminate=True, signal='SIGKILL')
    
    # 5. 更新数据库状态
    task.status = 'cancelled'
    task.completed_at = datetime.utcnow()
    db.commit()
    
    # 6. 减少配额计数
    decrement_workspace_running_tasks(workspace_id)
    
    return {"message": "Task cancelled successfully"}
```

### 6.5 手动重试机制

**重试失败任务**：

```python
@router.post("/workspaces/{workspace_id}/tasks/{task_id}/retry")
async def retry_task(
    workspace_id: int,
    task_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """手动重试失败的任务"""
    
    # 1. 获取任务记录
    task = db.query(Task).filter(
        Task.task_id == task_id,
        Task.workspace_id == workspace_id
    ).first()
    
    if not task:
        raise HTTPException(404, "Task not found")
    
    # 2. 检查任务状态
    if task.status != 'failure':
        raise HTTPException(400, f"Can only retry failed tasks, current status: {task.status}")
    
    # 3. 检查空间配额
    try:
        check_workspace_quota(workspace_id)
    except QuotaExceededError as e:
        raise HTTPException(429, str(e))
    
    # 4. 重置任务状态
    task.status = 'pending'
    task.retry_count = 0
    task.error = None
    task.traceback = None
    task.started_at = None
    task.completed_at = None
    db.commit()
    
    # 5. 重新提交到 Celery
    file = db.query(File).get(task.file_id)
    storage_path = get_storage_path(file.uri)
    
    celery_task = process_document_async.apply_async(
        args=[str(storage_path), task.file_id, task.user_id, workspace_id],
        queue=task.queue,
        priority=task.priority
    )
    
    # 6. 更新 Celery task ID
    task.task_id = celery_task.id
    db.commit()
    
    return {"message": "Task resubmitted successfully", "task_id": celery_task.id}
```

## 7. 数据库迁移策略

### 7.1 迁移步骤

**步骤 1：添加新表**
```sql
-- 创建 workspaces 表
CREATE TABLE workspaces (
    id INT PRIMARY KEY AUTO_INCREMENT,
    name VARCHAR(255) NOT NULL,
    slug VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    owner_id INT NOT NULL,
    max_concurrent_tasks INT DEFAULT 10,
    max_storage_bytes BIGINT DEFAULT 10737418240,
    priority_strategy VARCHAR(50) DEFAULT 'file_size',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (owner_id) REFERENCES users(id)
);

-- 创建 workspace_members 表
CREATE TABLE workspace_members (
    id INT PRIMARY KEY AUTO_INCREMENT,
    workspace_id INT NOT NULL,
    user_id INT NOT NULL,
    role VARCHAR(20) DEFAULT 'member',
    joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    UNIQUE KEY (workspace_id, user_id)
);

-- 创建 tasks 表
CREATE TABLE tasks (
    id INT PRIMARY KEY AUTO_INCREMENT,
    task_id VARCHAR(255) UNIQUE NOT NULL,
    workspace_id INT NOT NULL,
    user_id INT NOT NULL,
    file_id INT,
    task_type VARCHAR(50) NOT NULL,
    queue VARCHAR(20) NOT NULL,
    priority INT DEFAULT 5,
    status VARCHAR(20) DEFAULT 'pending',
    progress INT DEFAULT 0,
    retry_count INT DEFAULT 0,
    max_retries INT DEFAULT 3,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME,
    completed_at DATETIME,
    result JSON,
    error TEXT,
    traceback TEXT,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE SET NULL,
    INDEX idx_workspace_status (workspace_id, status),
    INDEX idx_user_created (user_id, created_at)
);
```

**步骤 2：为现有表添加 workspace_id**
```sql
-- 添加 workspace_id 列（允许 NULL）
ALTER TABLE files ADD COLUMN workspace_id INT;
ALTER TABLE teams ADD COLUMN workspace_id INT;
ALTER TABLE permissions ADD COLUMN workspace_id INT;
ALTER TABLE shares ADD COLUMN workspace_id INT;
```

**步骤 3：数据迁移**
```python
# 创建默认业务空间
default_workspace = Workspace(
    name="Default Workspace",
    slug="default",
    description="Default workspace for existing data",
    owner_id=1,  # 系统管理员
)
db.add(default_workspace)
db.commit()

# 将所有现有数据迁移到默认空间
db.execute("UPDATE files SET workspace_id = :wid", {"wid": default_workspace.id})
db.execute("UPDATE teams SET workspace_id = :wid", {"wid": default_workspace.id})
db.execute("UPDATE permissions SET workspace_id = :wid", {"wid": default_workspace.id})
db.execute("UPDATE shares SET workspace_id = :wid", {"wid": default_workspace.id})
db.commit()

# 将所有用户添加到默认空间
users = db.query(User).all()
for user in users:
    member = WorkspaceMember(
        workspace_id=default_workspace.id,
        user_id=user.id,
        role='member'
    )
    db.add(member)
db.commit()
```

**步骤 4：设置 NOT NULL 约束**
```sql
ALTER TABLE files MODIFY workspace_id INT NOT NULL;
ALTER TABLE teams MODIFY workspace_id INT NOT NULL;
ALTER TABLE permissions MODIFY workspace_id INT NOT NULL;
ALTER TABLE shares MODIFY workspace_id INT NOT NULL;

-- 添加外键约束
ALTER TABLE files ADD FOREIGN KEY (workspace_id) REFERENCES workspaces(id);
ALTER TABLE teams ADD FOREIGN KEY (workspace_id) REFERENCES workspaces(id);
ALTER TABLE permissions ADD FOREIGN KEY (workspace_id) REFERENCES workspaces(id);
ALTER TABLE shares ADD FOREIGN KEY (workspace_id) REFERENCES workspaces(id);
```

## 8. 文件结构

### 8.1 新增文件

```
src/openrag/
├── models/
│   ├── workspace.py              # Workspace 和 WorkspaceMember 模型
│   └── task.py                   # Task 模型
├── api/
│   ├── workspaces_api.py         # 业务空间管理 API
│   └── tasks_api.py              # 任务管理 API
├── services/
│   ├── priority_strategy.py      # 优先级策略系统
│   └── quota_manager.py          # 配额管理服务
└── tasks/
    └── celery_tasks.py           # 更新：添加配额检查和状态管理

tests/
├── test_workspace.py             # 业务空间测试
├── test_task_management.py       # 任务管理测试
├── test_priority_strategy.py     # 优先级策略测试
└── test_quota_control.py         # 配额控制测试

migrations/
└── versions/
    └── xxxx_add_workspace_and_task.py  # 数据库迁移脚本
```

### 8.2 修改文件

```
src/openrag/
├── models/
│   ├── file.py                   # 添加 workspace_id
│   ├── team.py                   # 添加 workspace_id
│   ├── permission.py             # 添加 workspace_id
│   └── share.py                  # 添加 workspace_id
├── api/
│   ├── files_api.py              # 集成配额检查和优先级计算
│   ├── main.py                   # 注册新的路由
│   └── deps.py                   # 添加 workspace 依赖注入
└── config.py                     # 添加 Redis 配置

celery_config.py                  # 扩展配置
```

## 9. 配置说明

### 9.1 环境变量

```bash
# Redis 配置（用于 Celery 和配额缓存）
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0

# Celery 配置
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0

# Worker 并发配置
CELERY_WORKER_CONCURRENCY=4
CELERY_WORKER_PREFETCH_MULTIPLIER=1
CELERY_WORKER_MAX_TASKS_PER_CHILD=100

# 全局配额（可选）
GLOBAL_MAX_CONCURRENT_TASKS=50
```

### 9.2 Worker 部署

**开发环境**（单 worker）：
```bash
celery -A openrag.tasks.celery_tasks worker -Q fast,normal,slow -c 4 -l info
```

**生产环境**（多 worker）：
```bash
# Fast 队列 worker
celery -A openrag.tasks.celery_tasks worker -Q fast -c 2 -n fast@%h &

# Normal 队列 worker
celery -A openrag.tasks.celery_tasks worker -Q normal -c 4 -n normal@%h &

# Slow 队列 worker
celery -A openrag.tasks.celery_tasks worker -Q slow -c 2 -n slow@%h &
```

**使用 Supervisor 管理**：
```ini
[program:celery-fast]
command=celery -A openrag.tasks.celery_tasks worker -Q fast -c 2 -n fast@%%h
directory=/path/to/openrag
autostart=true
autorestart=true

[program:celery-normal]
command=celery -A openrag.tasks.celery_tasks worker -Q normal -c 4 -n normal@%%h
directory=/path/to/openrag
autostart=true
autorestart=true

[program:celery-slow]
command=celery -A openrag.tasks.celery_tasks worker -Q slow -c 2 -n slow@%%h
directory=/path/to/openrag
autostart=true
autorestart=true
```

## 10. 测试策略

### 10.1 单元测试

**业务空间测试**：
- 创建/更新/删除业务空间
- 添加/移除成员
- 权限验证

**任务管理测试**：
- 任务创建和状态更新
- 任务取消和重试
- 配额检查逻辑

**优先级策略测试**：
- 文件大小策略计算
- 策略注册和切换
- 自定义策略扩展

### 10.2 集成测试

**端到端流程测试**：
1. 创建业务空间
2. 上传文件触发任务
3. 验证任务状态变化
4. 验证配额计数正确
5. 测试并发限制
6. 测试任务重试

**并发测试**：
- 模拟多用户同时上传
- 验证配额限制生效
- 验证队列隔离

### 10.3 性能测试

**配额查询性能**：
- Redis 缓存命中率
- 数据库查询优化

**任务吞吐量**：
- 不同队列的处理速度
- Worker 扩展性测试

## 11. 监控和运维

### 11.1 监控指标

**业务空间级别**：
- 当前运行任务数
- 待处理任务数
- 任务成功/失败率
- 存储使用量

**系统级别**：
- 总任务数
- 各队列任务分布
- Worker 健康状态
- Redis 连接状态

### 11.2 告警规则

- 业务空间达到配额上限
- 任务失败率超过阈值（如 10%）
- Worker 宕机
- Redis 连接失败
- 队列积压超过阈值

### 11.3 日志记录

**任务日志**：
```python
logger.info(f"Task {task_id} started for workspace {workspace_id}")
logger.info(f"Task {task_id} completed in {duration}s")
logger.error(f"Task {task_id} failed: {error}")
```

**配额日志**：
```python
logger.warning(f"Workspace {workspace_id} reached quota limit")
logger.info(f"Workspace {workspace_id} quota updated: {old} -> {new}")
```

## 12. 未来扩展

### 12.1 短期扩展（3个月内）

- 实现用户角色优先级策略
- 实现文件类型优先级策略
- 添加任务优先级动态调整
- 支持任务依赖关系

### 12.2 中期扩展（6个月内）

- 实现混合优先级策略
- 支持任务批量操作
- 添加任务调度策略（定时任务）
- 实现任务结果缓存

### 12.3 长期扩展（1年内）

- 支持分布式任务调度
- 实现智能负载均衡
- 添加任务成本分析
- 支持多区域部署

## 13. 总结

本设计实现了完整的任务管理和并发控制系统，核心特点：

1. **业务空间隔离** - 数据、权限、任务完全隔离，支持多租户
2. **可插拔设计** - 优先级策略可轻松替换，无需修改核心代码
3. **三层并发控制** - 全局、空间、队列三层限制，灵活可配
4. **自动重试** - 指数退避策略，提高任务成功率
5. **永久保存** - 任务状态永久保存，支持审计和分析
6. **易于扩展** - 预留接口，方便后续添加新功能

实现后将显著提升系统的稳定性、可管理性和用户体验。

