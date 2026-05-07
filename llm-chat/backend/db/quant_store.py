import time
from typing import Optional
from sqlalchemy import select, desc

from db.database import AsyncSessionLocal
from db.models import QuantSnapshotModel

async def save_quant_snapshot(
    snapshot_id: str,
    client_id: str,
    criteria: dict,
    rows: list = None,
    provider_trace: list = None,
    analysis: str = "",
    risk_notes: list = None,
    status: str = "DONE",
    user_id: str = "",
) -> None:
    if rows is None: rows = []
    if provider_trace is None: provider_trace = []
    if risk_notes is None: risk_notes = []
    
    async with AsyncSessionLocal() as session:
        snapshot = QuantSnapshotModel(
            id=snapshot_id,
            client_id=client_id,
            user_id=user_id,
            criteria=criteria,
            rows=rows,
            provider_trace=provider_trace,
            analysis=analysis,
            risk_notes=risk_notes,
            status=status,
            created_at=time.time(),
        )
        session.add(snapshot)
        await session.commit()


async def update_quant_snapshot(
    snapshot_id: str,
    rows: list,
    provider_trace: list,
    status: str = "DONE",
    warnings: list[str] = None,
) -> None:
    """异步计算完成后，回写结果数据和状态。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(QuantSnapshotModel).where(QuantSnapshotModel.id == snapshot_id)
        )
        snapshot = result.scalars().first()
        if snapshot:
            snapshot.rows = rows
            snapshot.provider_trace = provider_trace
            snapshot.status = status
            # 如果有警告，可以并入 criteria 或记录日志，目前暂不扩展字段
            await session.commit()


async def get_active_quant_session(
    client_id: str,
    market: Optional[str] = None,
    user_id: str = "",
) -> Optional[QuantSnapshotModel]:
    """查询该客户端最近一个筛选任务（1小时内，任意状态）。"""
    one_hour_ago = time.time() - 3600
    async with AsyncSessionLocal() as session:
        stmt = select(QuantSnapshotModel).where(QuantSnapshotModel.created_at >= one_hour_ago)
        
        if user_id:
            stmt = stmt.where(QuantSnapshotModel.user_id == user_id)
        else:
            stmt = stmt.where(
                QuantSnapshotModel.client_id == client_id,
                QuantSnapshotModel.user_id == ""
            )
        
        if market:
            # PostgreSQL JSONB lookup: criteria ->> 'market'
            stmt = stmt.where(QuantSnapshotModel.criteria['market'].astext == market)
            
        result = await session.execute(
            stmt.order_by(desc(QuantSnapshotModel.created_at)).limit(1)
        )
        return result.scalars().first()

async def get_quant_snapshot(snapshot_id: str) -> Optional[QuantSnapshotModel]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(QuantSnapshotModel).where(QuantSnapshotModel.id == snapshot_id)
        )
        return result.scalars().first()

async def attach_snapshot_to_conversation(snapshot_id: str, conv_id: str) -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(QuantSnapshotModel).where(QuantSnapshotModel.id == snapshot_id)
        )
        snapshot = result.scalars().first()
        if snapshot:
            snapshot.conversation_id = conv_id
            await session.commit()


async def update_quant_analysis(
    snapshot_id: str,
    analysis: str,
    risk_notes: list[str],
    thinking_segments: list[dict] | None = None,
) -> None:
    """异步分析完成后回写 analysis / risk_notes / thinking_segments（同 snapshot 行）。

    thinking_segments 遵循 spec §模型思考流程的结构化协议：
      [{"node":"quant_analyze", "step_index": null, "phase":"reasoning"|"content", "content":"..."}]
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(QuantSnapshotModel).where(QuantSnapshotModel.id == snapshot_id)
        )
        snapshot = result.scalars().first()
        if snapshot is None:
            return
        snapshot.analysis = analysis or ""
        snapshot.risk_notes = list(risk_notes or [])
        if thinking_segments is not None:
            snapshot.thinking_segments = list(thinking_segments)
        await session.commit()

async def cleanup_stale_quant_sessions(timeout_minutes: int | None = None) -> int:
    """系统启动时或定时调用：清理所有卡在 COMPUTING 状态的旧任务。"""
    from sqlalchemy import update
    import time
    
    async with AsyncSessionLocal() as session:
        stmt = update(QuantSnapshotModel).where(QuantSnapshotModel.status == "COMPUTING")
        
        if timeout_minutes is not None:
            cutoff_ts = time.time() - (timeout_minutes * 60)
            stmt = stmt.where(QuantSnapshotModel.created_at < cutoff_ts)
            reason = f"筛选任务超时（超过 {timeout_minutes} 分钟未完成）"
        else:
            reason = "服务重启，筛选任务已中断"
            
        stmt = stmt.values(status="FAILED", analysis=reason)
        result = await session.execute(stmt)
        count = result.rowcount
        await session.commit()
        return count
