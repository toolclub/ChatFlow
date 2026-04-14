"""
工具调用持久化层 — 每次工具调用独立记录
"""
import logging
import time

from sqlalchemy import select, update as sa_update

from db.database import AsyncSessionLocal
from db.models import ToolExecutionModel

logger = logging.getLogger("db.tool_store")


async def create_tool_execution(
    conv_id: str,
    message_id: str,
    tool_name: str,
    tool_input: dict,
    sequence_number: int = 0,
    step_index: int | None = None,
) -> int:
    """创建工具调用记录（status=RUNNING），返回自增 ID。"""
    from db.state_machine import ToolExecutionStatus

    async with AsyncSessionLocal() as session:
        row = ToolExecutionModel(
            conv_id=conv_id,
            message_id=message_id,
            tool_name=tool_name,
            tool_input=tool_input,
            status=ToolExecutionStatus.RUNNING.value,
            sequence_number=sequence_number,
            step_index=step_index,
            created_at=time.time(),
        )
        session.add(row)
        await session.flush()
        tid = row.id
        await session.commit()
    return tid


async def complete_tool_execution(
    tool_exec_id: int,
    output: str = "",
    search_items: list | None = None,
    status: str = "done",
    duration: float = 0,
) -> None:
    """
    将工具调用结果持久化到 DB。

    状态校验由调用方的 ToolExecutionSM 实例负责，此函数只做持久化。
    """
    values: dict = {"status": status, "tool_output": output, "duration": duration}
    if search_items is not None:
        values["search_items"] = search_items
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_update(ToolExecutionModel)
            .where(ToolExecutionModel.id == tool_exec_id)
            .values(**values)
        )
        await session.commit()


async def update_tool_search_items(tool_exec_id: int, search_items: list) -> None:
    """更新搜索结果（实时追加）。"""
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_update(ToolExecutionModel)
            .where(ToolExecutionModel.id == tool_exec_id)
            .values(search_items=search_items)
        )
        await session.commit()


async def get_tool_executions_for_conv(conv_id: str) -> list[dict]:
    """获取对话的全部工具调用记录（含 display_mode 协议字段）。"""
    from tools.skill import SkillRegistry
    registry = SkillRegistry.instance()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ToolExecutionModel)
            .where(ToolExecutionModel.conv_id == conv_id)
            .order_by(ToolExecutionModel.id.asc())
        )
        rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "message_id": r.message_id,
            "tool_name": r.tool_name,
            "tool_input": r.tool_input,
            "tool_output": r.tool_output,
            "search_items": r.search_items or [],
            "status": r.status,
            "step_index": r.step_index,
            "sequence_number": r.sequence_number,
            "duration": r.duration,
            "created_at": r.created_at,
            "display_mode": registry.get_display_mode(r.tool_name),
        }
        for r in rows
    ]


async def get_tool_executions_for_message(message_id: str) -> list[dict]:
    """获取指定消息的工具调用记录（含 display_mode 协议字段）。"""
    from tools.skill import SkillRegistry
    registry = SkillRegistry.instance()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ToolExecutionModel)
            .where(ToolExecutionModel.message_id == message_id)
            .order_by(ToolExecutionModel.sequence_number.asc(), ToolExecutionModel.id.asc())
        )
        rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "tool_name": r.tool_name,
            "tool_input": r.tool_input,
            "tool_output": r.tool_output,
            "search_items": r.search_items or [],
            "status": r.status,
            "duration": r.duration,
            "display_mode": registry.get_display_mode(r.tool_name),
        }
        for r in rows
    ]
