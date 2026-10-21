from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession


def contains_pattern(term: str) -> str:
    """LIKE pattern for a substring search with user-supplied wildcards neutralised (use escape="\\")."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def count_rows(session: AsyncSession, stmt: Select) -> int:
    return await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
