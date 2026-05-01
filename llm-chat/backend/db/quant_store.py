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
) -> None:
    if rows is None: rows = []
    if provider_trace is None: provider_trace = []
    if risk_notes is None: risk_notes = []
    
    async with AsyncSessionLocal() as session:
        snapshot = QuantSnapshotModel(
            id=snapshot_id,
            client_id=client_id,
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


async def get_active_quant_session(client_id: str) -> Optional[QuantSnapshotModel]:
    """查询该客户端最近一个筛选任务（1小时内，任意状态）。"""
    one_hour_ago = time.time() - 3600
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(QuantSnapshotModel)
            .where(QuantSnapshotModel.client_id == client_id)
            .where(QuantSnapshotModel.created_at >= one_hour_ago)
            .order_by(desc(QuantSnapshotModel.created_at))
            .limit(1)
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
) -> None:
    """异步分析完成后回写 analysis / risk_notes（同 snapshot 行）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(QuantSnapshotModel).where(QuantSnapshotModel.id == snapshot_id)
        )
        snapshot = result.scalars().first()
        if snapshot is None:
            return
        snapshot.analysis = analysis or ""
        snapshot.risk_notes = list(risk_notes or [])
        await session.commit()
