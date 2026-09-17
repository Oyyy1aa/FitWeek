"""Ensure the SQL adapter satisfies the complete public MemoryRepository port."""

import inspect

from app.domain.memory.repositories import MemoryRepository
from app.persistence.mysql.memory_repository import MySQLMemoryRepository


def test_mysql_memory_repository_implements_every_memory_port_method() -> None:
    protocol_methods = {
        name: member
        for name, member in vars(MemoryRepository).items()
        if inspect.iscoroutinefunction(member)
    }
    missing = [
        name for name in protocol_methods if not hasattr(MySQLMemoryRepository, name)
    ]
    assert missing == []
    for name in protocol_methods:
        implementation = getattr(MySQLMemoryRepository, name)
        assert inspect.iscoroutinefunction(implementation)
        assert "NotImplementedError" not in inspect.getsource(implementation)
