"""Persistence of workflow runs (needed to resume a run when the user replies)."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, app_session, utcnow
from app.workflows.models import RunStatus, WorkflowRun


class WorkflowRunRecord(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (Index("ix_workflow_runs_thread_status", "thread_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow: Mapped[str] = mapped_column(String(255), index=True)
    user_email: Mapped[str] = mapped_column(String(255), index=True)
    thread_id: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


async def save(run: WorkflowRun) -> None:
    async with app_session() as session:
        record = await session.get(WorkflowRunRecord, run.id) or WorkflowRunRecord(id=run.id)
        record.workflow = run.workflow
        record.user_email = run.user_email
        record.thread_id = run.thread_id
        record.status = run.status.value
        record.error = run.error
        record.data = run.model_dump(mode="json")
        session.add(record)
        await session.commit()


async def find_awaiting_reply(thread_id: str) -> WorkflowRun | None:
    async with app_session() as session:
        record = await session.scalar(
            select(WorkflowRunRecord)
            .where(WorkflowRunRecord.thread_id == thread_id, WorkflowRunRecord.status == RunStatus.AWAITING_REPLY)
            .order_by(WorkflowRunRecord.updated_at.desc())
        )
    return WorkflowRun.model_validate(record.data) if record else None


async def recent_runs(limit: int = 20) -> list[WorkflowRunRecord]:
    async with app_session() as session:
        return list(
            await session.scalars(select(WorkflowRunRecord).order_by(WorkflowRunRecord.updated_at.desc()).limit(limit))
        )
