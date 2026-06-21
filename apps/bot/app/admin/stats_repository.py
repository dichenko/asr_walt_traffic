from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message, User


@dataclass(frozen=True)
class DailyStatsPoint:
    date: str
    new_users: int
    active_users: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "newUsers": self.new_users,
            "activeUsers": self.active_users,
        }


@dataclass(frozen=True)
class AdminStats:
    total_users: int
    total_messages: int
    daily: list[DailyStatsPoint]

    def as_dict(self) -> dict[str, Any]:
        return {
            "totalUsers": self.total_users,
            "totalMessages": self.total_messages,
            "daily": [point.as_dict() for point in self.daily],
        }


class AdminStatsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def fetch(self, *, today: date | None = None) -> AdminStats:
        current_day = today or datetime.now(UTC).date()
        start_day = current_day - timedelta(days=29)
        start_at = datetime.combine(start_day, datetime.min.time(), tzinfo=UTC)

        total_users = await self._count_users()
        total_messages = await self._count_messages()
        new_users_by_day = await self._new_users_by_day(start_at)
        active_users_by_day = await self._active_users_by_day(start_at)

        daily = []
        for day_offset in range(30):
            day = start_day + timedelta(days=day_offset)
            day_key = day.isoformat()
            daily.append(
                DailyStatsPoint(
                    date=day_key,
                    new_users=new_users_by_day.get(day_key, 0),
                    active_users=active_users_by_day.get(day_key, 0),
                )
            )

        return AdminStats(
            total_users=total_users,
            total_messages=total_messages,
            daily=daily,
        )

    async def _count_users(self) -> int:
        result = await self.session.execute(select(func.count(User.id)))
        return int(result.scalar_one())

    async def _count_messages(self) -> int:
        result = await self.session.execute(select(func.count(Message.id)))
        return int(result.scalar_one())

    async def _new_users_by_day(self, start_at: datetime) -> dict[str, int]:
        first_messages = (
            select(
                Message.user_id.label("user_id"),
                func.min(Message.created_at).label("first_message_at"),
            )
            .where(Message.direction == "in")
            .group_by(Message.user_id)
            .subquery()
        )
        day_expr = func.date(first_messages.c.first_message_at)
        stmt = (
            select(day_expr, func.count(first_messages.c.user_id))
            .where(first_messages.c.first_message_at >= start_at)
            .group_by(day_expr)
        )
        result = await self.session.execute(stmt)
        return {_date_key(day): int(count) for day, count in result.all()}

    async def _active_users_by_day(self, start_at: datetime) -> dict[str, int]:
        day_expr = func.date(Message.created_at)
        stmt = (
            select(day_expr, func.count(distinct(Message.user_id)))
            .where(Message.direction == "in", Message.created_at >= start_at)
            .group_by(day_expr)
        )
        result = await self.session.execute(stmt)
        return {_date_key(day): int(count) for day, count in result.all()}


def _date_key(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]
