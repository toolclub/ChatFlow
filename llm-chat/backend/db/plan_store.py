"""
plan_store：执行计划的 DB 持久化层

职责：
  - create_plan:       planner_node 规划完成后写入 plan_steps 表
  - save_step_result:  reflector_node 步骤完成后保存结果并推进 current_step
  - get_plan:          供调试/未来恢复功能读取计划详情

设计原则：
  - 所有操作均 try/except，失败仅记录日志，不抛异常（不阻断主执行流程）
  - DB 是持久化副本，GraphState.plan 才是运行时权威数据
"""
import json
import logging
import time

from sqlalchemy import select, text, desc

from db.database import AsyncSessionLocal
from db.models import PlanStepModel

logger = logging.getLogger("db.plan_store")


async def create_plan(
    plan_id: str,
    conv_id: str,
    goal: str,
    steps: list[dict],
) -> None:
    """
    将 planner 规划结果写入 plan_steps 表。
    全部字段来自 GraphState，不依赖 DB 读取。
    """
    now = time.time()
    try:
        async with AsyncSessionLocal() as session:
            session.add(PlanStepModel(
                id=plan_id,
                conv_id=conv_id,
                goal=goal,
                steps=steps,
                current_step=0,
                total_steps=len(steps),
                created_at=now,
                updated_at=now,
            ))
            await session.commit()
        logger.info(
            "计划已写入DB | plan_id=%s | conv=%s | steps=%d",
            plan_id, conv_id, len(steps),
        )
    except Exception as exc:
        logger.error(
            "写入计划到DB失败（不影响主流程）| plan_id=%s | error=%s",
            plan_id, exc,
        )


async def save_step_result(
    plan_id: str,
    step_index: int,
    result: str,
    new_current_step: int,
) -> None:
    """
    原子更新步骤执行结果，不需要先读取整行。

    使用 PostgreSQL jsonb_set 对 steps[step_index] 的 status/result
    字段做原子写入，消灭读-改-写 round trip，并发安全。
    result 截断到 3000 字符防止 JSONB 过大。
    """
    truncated = result[:3000]
    # jsonb_set path 格式: {index,field}
    path_status = f"{{{step_index},status}}"
    path_result = f"{{{step_index},result}}"

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "UPDATE plan_steps"
                    "   SET steps       = jsonb_set(jsonb_set(steps, :p_status, CAST(:v_status AS jsonb)),"
                    "                               :p_result, CAST(:v_result AS jsonb)),"
                    "       current_step = :next_step,"
                    "       updated_at   = :now"
                    " WHERE id = :plan_id"
                ),
                {
                    "p_status": path_status,
                    "v_status": json.dumps("done"),
                    "p_result": path_result,
                    "v_result": json.dumps(truncated),
                    "next_step": new_current_step,
                    "now":       time.time(),
                    "plan_id":   plan_id,
                },
            )
            await session.commit()
        logger.info(
            "步骤结果已存DB | plan_id=%s | step=%d → next=%d | result_len=%d",
            plan_id, step_index, new_current_step, len(truncated),
        )
    except Exception as exc:
        logger.error(
            "保存步骤结果失败（不影响主流程）| plan_id=%s | step=%d | error=%s",
            plan_id, step_index, exc,
        )


async def finalize_all_steps(plan_id: str, plan: list[dict]) -> None:
    """
    将所有步骤标记为 done 并写入 DB（save_response 完成时调用）。

    确保刷新页面后 DB 里的状态全部是 done，不会残留 running。
    """
    try:
        finalized = []
        for step in plan:
            s = dict(step)
            if s.get("status") != "failed":
                s["status"] = "done"
            finalized.append(s)

        async with AsyncSessionLocal() as session:
            await session.execute(
                text(
                    "UPDATE plan_steps"
                    "   SET steps = CAST(:steps AS jsonb),"
                    "       updated_at = :now"
                    " WHERE id = :plan_id"
                ),
                {
                    "steps": json.dumps(finalized, ensure_ascii=False),
                    "now": time.time(),
                    "plan_id": plan_id,
                },
            )
            await session.commit()
        logger.info("finalize_all_steps | plan_id=%s | steps=%d", plan_id, len(finalized))
    except Exception as exc:
        logger.error("finalize_all_steps 失败 | plan_id=%s | error=%s", plan_id, exc)


async def get_latest_plan_for_conv(conv_id: str) -> dict | None:
    """
    获取对话最新的执行计划（按 created_at 降序取第一条）。
    供前端刷新后恢复认知面板使用。
    """
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(PlanStepModel)
                .where(PlanStepModel.conv_id == conv_id)
                .order_by(desc(PlanStepModel.created_at))
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if not row:
                return None
            return {
                "id":    row.id,
                "goal":  row.goal,
                "steps": list(row.steps or []),
            }
    except Exception as exc:
        logger.error("读取最新计划失败 | conv=%s | error=%s", conv_id, exc)
        return None


async def get_plan(plan_id: str) -> dict | None:
    """
    读取计划详情（供调试接口或未来断点续跑功能使用）。
    返回 None 表示 plan_id 不存在或 DB 异常。
    """
    try:
        async with AsyncSessionLocal() as session:
            row = await session.get(PlanStepModel, plan_id)
            if not row:
                return None
            return {
                "id":           row.id,
                "conv_id":      row.conv_id,
                "goal":         row.goal,
                "steps":        list(row.steps or []),
                "current_step": row.current_step,
                "total_steps":  row.total_steps,
            }
    except Exception as exc:
        logger.error("读取计划失败 | plan_id=%s | error=%s", plan_id, exc)
        return None
