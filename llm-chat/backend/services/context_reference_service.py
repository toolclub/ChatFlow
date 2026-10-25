"""Load explicit user-selected context, checking ownership before model exposure."""
import json
from sqlalchemy import select
from db.database import AsyncSessionLocal
from db.models import PredictionSnapshotModel
from db.quant_store import get_quant_snapshot


def owns_snapshot(snapshot, user_id: str, client_id: str) -> bool:
    owner = getattr(snapshot, 'user_id', '') or ''
    if owner:
        return bool(user_id) and owner == user_id
    return bool(client_id) and getattr(snapshot, 'client_id', '') == client_id


async def load_context_reference(ref: dict, *, user_id: str = '', client_id: str = '') -> str:
    kind = ref.get('type')
    snapshot_id = ref.get('id')
    if kind == 'quant_snapshot':
        snap = await get_quant_snapshot(snapshot_id)
        if not snap or not owns_snapshot(snap, user_id, client_id):
            raise ValueError('选股快照不存在或无权读取，请重新选择')
        data = {'id':snap.id,'criteria':snap.criteria,'analysis':snap.analysis,'risk_notes':snap.risk_notes,'rows':snap.rows[:5]}
        return '【量化选股快照】\n' + json.dumps(data, ensure_ascii=False, default=str)
    if kind == 'prediction_snapshot':
        model = PredictionSnapshotModel
        # News bodies can exceed 10 MB. The analysis uses saved conclusions,
        # prices, risks and model parameters, not a duplicate of every webpage.
        names = ('id','user_id','client_id','symbol','market','status','target_date','predicted_price','predicted_range','confidence','analysis','risk_notes','technical_indicators','model_params','event_score')
        async with AsyncSessionLocal() as session:
            row = (await session.execute(select(*(getattr(model,n) for n in names)).where(model.id == snapshot_id))).first()
        if not row or not owns_snapshot(row, user_id, client_id):
            raise ValueError('预测快照不存在或无权读取，请重新选择')
        data = {key:value for key,value in row._mapping.items() if key not in ('user_id','client_id')}
        return '【股票预测快照】\n' + json.dumps(data, ensure_ascii=False, default=str)
    raise ValueError('不支持的上下文引用类型')
