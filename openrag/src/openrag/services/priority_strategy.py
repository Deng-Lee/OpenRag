"""Priority strategy system for task queue routing"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Dict, List, Tuple, Type

if TYPE_CHECKING:
    from openrag.models.user import User
    from openrag.models.workspace import Workspace


class PriorityStrategy(ABC):
    """Abstract base class for priority strategies

    Priority strategies determine which queue a task should be routed to
    and its priority score within that queue.
    """

    @abstractmethod
    def calculate_priority(
        self,
        file_size: int,
        file_type: str,
        user: "User",
        workspace: "Workspace"
    ) -> Tuple[str, int]:
        """Calculate task priority

        Args:
            file_size: File size in bytes
            file_type: File MIME type
            user: User who submitted the task
            workspace: Workspace the task belongs to

        Returns:
            Tuple of (queue_name, priority_score)
            - queue_name: 'fast', 'normal', 'slow'
            - priority_score: 0-10 priority score (higher is more important)
        """
        pass

    @abstractmethod
    def get_strategy_name(self) -> str:
        """Return strategy name identifier"""
        pass


class FileSizeStrategy(PriorityStrategy):
    """File size based priority strategy

    Routes tasks based on file size:
    - < 1MB: fast queue, high priority (8)
    - 1MB - 10MB: normal queue, medium priority (5)
    - > 10MB: slow queue, low priority (2)
    """

    # Size thresholds in bytes
    FAST_THRESHOLD = 1_000_000  # 1MB
    NORMAL_THRESHOLD = 10_000_000  # 10MB

    def calculate_priority(
        self,
        file_size: int,
        file_type: str,
        user: "User",
        workspace: "Workspace"
    ) -> Tuple[str, int]:
        """Calculate priority based on file size"""
        if file_size < self.FAST_THRESHOLD:
            return ('fast', 8)
        elif file_size < self.NORMAL_THRESHOLD:
            return ('normal', 5)
        else:
            return ('slow', 2)

    def get_strategy_name(self) -> str:
        return 'file_size'


class UserRoleStrategy(PriorityStrategy):
    """User role based priority strategy (reserved for future use)

    Routes tasks based on user role:
    - admin: fast queue, highest priority (10)
    - member: normal queue, medium priority (5)
    - viewer: slow queue, low priority (3)
    """

    ROLE_PRIORITY = {
        'admin': ('fast', 10),
        'member': ('normal', 5),
        'viewer': ('slow', 3),
    }

    def calculate_priority(
        self,
        file_size: int,
        file_type: str,
        user: "User",
        workspace: "Workspace"
    ) -> Tuple[str, int]:
        """Calculate priority based on user role"""
        # TODO: Get user role in workspace
        # For now, default to member
        role = 'member'
        return self.ROLE_PRIORITY.get(role, ('normal', 5))

    def get_strategy_name(self) -> str:
        return 'user_role'


class FileTypeStrategy(PriorityStrategy):
    """File type based priority strategy (reserved for future use)

    Routes tasks based on file type:
    - text/markdown: fast queue, high priority (9)
    - text/plain: fast queue, high priority (8)
    - application/pdf: normal queue, medium priority (5)
    - image/*: slow queue, low priority (3)
    - application/vnd.*: slow queue, lowest priority (2)
    """

    TYPE_PRIORITY = {
        'text/markdown': ('fast', 9),
        'text/plain': ('fast', 8),
        'text/html': ('fast', 7),
        'application/pdf': ('normal', 5),
        'application/msword': ('normal', 5),
        'image/png': ('slow', 3),
        'image/jpeg': ('slow', 3),
        'image/*': ('slow', 3),
    }

    def calculate_priority(
        self,
        file_size: int,
        file_type: str,
        user: "User",
        workspace: "Workspace"
    ) -> Tuple[str, int]:
        """Calculate priority based on file type"""
        # Try exact match first
        if file_type in self.TYPE_PRIORITY:
            return self.TYPE_PRIORITY[file_type]

        # Try wildcard match (e.g., image/jpeg -> image/*)
        main_type = file_type.split('/')[0] if '/' in file_type else ''
        wildcard_type = f"{main_type}/*"
        if wildcard_type in self.TYPE_PRIORITY:
            return self.TYPE_PRIORITY[wildcard_type]

        # Default to normal
        return ('normal', 5)

    def get_strategy_name(self) -> str:
        return 'file_type'


class HybridStrategy(PriorityStrategy):
    """Hybrid strategy combining file size and user role (reserved for future use)

    Combines multiple factors:
    - File size weight: 60%
    - User role weight: 40%
    """

    def __init__(self):
        self.file_size_strategy = FileSizeStrategy()
        # TODO: Add user role strategy when implemented

    def calculate_priority(
        self,
        file_size: int,
        file_type: str,
        user: "User",
        workspace: "Workspace"
    ) -> Tuple[str, int]:
        """Calculate priority using hybrid approach"""
        # Get file size priority
        queue, score = self.file_size_strategy.calculate_priority(
            file_size, file_type, user, workspace
        )

        # TODO: Adjust based on user role
        # For now, just use file size

        return (queue, score)

    def get_strategy_name(self) -> str:
        return 'hybrid'


class PriorityStrategyRegistry:
    """Registry for priority strategies

    Supports dynamic registration and retrieval of strategies.
    """

    _strategies: Dict[str, Type[PriorityStrategy]] = {
        'file_size': FileSizeStrategy,
        'user_role': UserRoleStrategy,
        'file_type': FileTypeStrategy,
        'hybrid': HybridStrategy,
    }

    @classmethod
    def register(cls, name: str, strategy_class: Type[PriorityStrategy]) -> None:
        """Register a new strategy

        Args:
            name: Strategy name
            strategy_class: Strategy class
        """
        cls._strategies[name] = strategy_class

    @classmethod
    def get_strategy(cls, strategy_name: str) -> PriorityStrategy:
        """Get strategy instance by name

        Args:
            strategy_name: Strategy name

        Returns:
            Strategy instance

        Raises:
            ValueError: If strategy not found
        """
        if strategy_name not in cls._strategies:
            raise ValueError(
                f"Unknown priority strategy: {strategy_name}. "
                f"Available: {cls.list_strategies()}"
            )
        return cls._strategies[strategy_name]()

    @classmethod
    def list_strategies(cls) -> List[str]:
        """List all available strategy names"""
        return list(cls._strategies.keys())

    @classmethod
    def has_strategy(cls, strategy_name: str) -> bool:
        """Check if strategy exists"""
        return strategy_name in cls._strategies


# Convenience function for calculating priority
def calculate_task_priority(
    file_size: int,
    file_type: str,
    user: "User",
    workspace: "Workspace",
    strategy_name: str = 'file_size'
) -> Tuple[str, int]:
    """Calculate task priority using specified strategy

    Args:
        file_size: File size in bytes
        file_type: File MIME type
        user: User who submitted the task
        workspace: Workspace the task belongs to
        strategy_name: Name of priority strategy to use

    Returns:
        Tuple of (queue_name, priority_score)
    """
    strategy = PriorityStrategyRegistry.get_strategy(strategy_name)
    return strategy.calculate_priority(file_size, file_type, user, workspace)
