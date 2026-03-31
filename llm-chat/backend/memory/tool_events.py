"""
工具调用事件持久化：存储和查询每次对话的工具调用历史
供前端刷新后复现"此会话经历了什么"
"""
import logging
import time

from sqlalchemy import select

from db.database import AsyncSessionLocal
from db.models import ToolEventModel

logger = logging.getLogger("memory.tool_events")


async def save_tool_event(conv_id: str, tool_name: str, tool_input: dict) -> None:
    """保存一条工具调用事件"""
    try:
        async with AsyncSessionLocal() as session:
            session.add(ToolEventModel(
                conv_id=conv_id,
                tool_name=tool_name,
                tool_input=tool_input or {},
                created_at=time.time(),
            ))
            await session.commit()
    except Exception as e:
        logger.error("保存工具事件失败 conv=%s tool=%s: %s", conv_id, tool_name, e)


async def get_tool_events(conv_id: str) -> list[dict]:
    """获取对话的全部工具调用事件，按时间升序"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ToolEventModel)
            .where(ToolEventModel.conv_id == conv_id)
            .order_by(ToolEventModel.created_at.asc())
        )
        rows = result.scalars().all()
        return [
            {
                "id": r.id,
                "tool_name": r.tool_name,
                "tool_input": r.tool_input or {},
                "created_at": r.created_at,
            }
            for r in rows
        ]
