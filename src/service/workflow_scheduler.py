"""
周期性工作流调度器 (WorkflowScheduler)

按固定时间间隔自动触发工作流执行，支持并行实例。
调度配置存放在 GuidanceFile.constraints 中：
  - schedule_interval_seconds: int   — 周期间隔（秒）
  - schedule_max_parallel: int       — 最大并行实例数（默认 5）
  - schedule_max_history: int        — 保留历史记录数（默认 100）
  - schedule_auto_restart: bool      — 服务重启后自动恢复调度（默认 False）

执行历史持久化到 data/schedule_history.json。
"""
import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.app.models import ScheduleExecutionRecord

logger = logging.getLogger(__name__)

HISTORY_STORE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "schedule_history.json"
)


# ======================================================================
# 内部调度状态
# ======================================================================

@dataclass
class _ScheduleState:
    """单个应用的调度运行状态"""
    app_id: str
    schedule_workflow_handle: str
    interval_seconds: int
    max_parallel: int
    max_history: int
    scheduler_task: Optional[asyncio.Task] = field(default=None, repr=False)
    active_runs: Dict[str, asyncio.Task] = field(default_factory=dict, repr=False)
    total_runs: int = 0
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


# ======================================================================
# WorkflowScheduler
# ======================================================================

class WorkflowScheduler:
    """
    周期性工作流调度器

    管理所有应用的周期调度：
    - start_schedule()  启动某应用的周期调度
    - stop_schedule()   停止某应用的周期调度
    - get_schedule_status()  查询调度状态
    - get_history()     查询执行历史
    """

    def __init__(self):
        self._schedules: Dict[str, _ScheduleState] = {}
        self._history_path = os.path.abspath(HISTORY_STORE_PATH)
        # app_id → List[ScheduleExecutionRecord]
        self._history: Dict[str, List[ScheduleExecutionRecord]] = {}
        os.makedirs(os.path.dirname(self._history_path), exist_ok=True)
        self._load_history()
        logger.info("[Scheduler] WorkflowScheduler 初始化完成")

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def start_schedule(
        self,
        app_id: str,
        interval_seconds: int,
        max_parallel: int = 5,
        max_history: int = 100,
    ) -> bool:
        """
        启动应用的周期调度。

        Args:
            app_id:            应用 ID
            interval_seconds:  周期间隔（秒）
            max_parallel:      最大并行实例数
            max_history:       保留历史记录数

        Returns:
            True 表示成功启动
        """
        if app_id in self._schedules:
            logger.warning(f"[Scheduler] 应用 {app_id} 已在调度中")
            return False

        if interval_seconds < 1:
            logger.error(f"[Scheduler] interval_seconds 必须 >= 1，收到 {interval_seconds}")
            return False

        state = _ScheduleState(
            app_id=app_id,
            schedule_workflow_handle=f"sched_wf_{app_id}_{uuid.uuid4().hex[:6]}",
            interval_seconds=interval_seconds,
            max_parallel=max_parallel,
            max_history=max_history,
        )
        # 说明：我们把 schedule_workflow_handle 直接作为可视化工作流 id，
        # 前端会把它当作一个可选项显示。实际每次调度触发的子运行仍然
        # 是独立的执行任务（run_id）。调度器会把必要的子运行进度合并
        # 到此调度会话的 snapshot 中供前端展示，而不必将每个子 run 单独
        # 注册为 viz workflow（以避免列表泛滥）。
        # 在 VizBus 中注册该调度会话的可视化入口（用于列表与汇总展示）
        self._register_schedule_viz_record(state)
        state.scheduler_task = asyncio.create_task(
            self._schedule_loop(app_id, state),
            name=f"scheduler_{app_id}",
        )
        self._schedules[app_id] = state

        logger.info(
            f"[Scheduler] 周期调度已启动: app_id={app_id}, "
            f"interval={interval_seconds}s, max_parallel={max_parallel}, "
            f"session={state.schedule_workflow_handle}"
        )
        return True

    async def stop_schedule(self, app_id: str) -> bool:
        """
        停止应用的周期调度。

        活跃的工作流实例继续运行直到完成（不强制取消）。

        Args:
            app_id: 应用 ID

        Returns:
            True 表示成功停止
        """
        state = self._schedules.pop(app_id, None)
        if not state:
            logger.warning(f"[Scheduler] 应用 {app_id} 未在调度中")
            return False

        # 取消调度循环
        if state.scheduler_task and not state.scheduler_task.done():
            state.scheduler_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(state.scheduler_task), timeout=2.0
                )
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        active_count = len(state.active_runs)
        self._publish_schedule_viz_state(
            state,
            node_name="schedule_stopped",
            extra={
                "schedule_stopped_at": datetime.utcnow().isoformat(),
                "schedule_status": "stopped",
            },
            finish_status="cancelled",
        )
        logger.info(
            f"[Scheduler] 周期调度已停止: app_id={app_id}, "
            f"total_runs={state.total_runs}, "
            f"active_runs={active_count}（将继续运行至完成）"
        )
        return True

    def is_scheduled(self, app_id: str) -> bool:
        """检查应用是否处于周期调度状态"""
        return app_id in self._schedules

    def get_schedule_status(self, app_id: str) -> Optional[dict]:
        """
        获取调度状态。

        Returns:
            状态字典，未调度返回 None
        """
        state = self._schedules.get(app_id)
        if not state:
            return None

        # 清理已完成的 runs
        self._cleanup_done_runs(app_id, state)

        return {
            "app_id": app_id,
            "schedule_workflow_handle": state.schedule_workflow_handle,
            "interval_seconds": state.interval_seconds,
            "max_parallel": state.max_parallel,
            "active_runs": len(state.active_runs),
            "total_runs": state.total_runs,
            "started_at": state.started_at,
            "active_run_ids": list(state.active_runs.keys()),
        }

    def get_history(self, app_id: str, limit: int = 50) -> List[dict]:
        """
        获取执行历史。

        Args:
            app_id: 应用 ID
            limit:  最多返回条数

        Returns:
            执行记录列表（最新的在前）
        """
        records = self._history.get(app_id, [])
        # 按 started_at 倒序
        sorted_records = sorted(records, key=lambda r: r.started_at, reverse=True)
        return [r.to_dict() for r in sorted_records[:limit]]

    # ------------------------------------------------------------------
    # 调度循环
    # ------------------------------------------------------------------

    async def _schedule_loop(self, app_id: str, state: _ScheduleState):
        """周期调度主循环"""
        logger.info(
            f"[Scheduler] 调度循环启动: app_id={app_id}, "
            f"interval={state.interval_seconds}s"
        )
        try:
            while True:
                await asyncio.sleep(state.interval_seconds)

                # 清理已完成的 runs
                self._cleanup_done_runs(app_id, state)

                # 检查并行上限
                if len(state.active_runs) >= state.max_parallel:
                    logger.warning(
                        f"[Scheduler] 达到并行上限，跳过本轮: "
                        f"app_id={app_id}, active={len(state.active_runs)}, "
                        f"max={state.max_parallel}"
                    )
                    continue

                # 创建新 run
                run_id = f"run_{uuid.uuid4().hex[:8]}"
                workflow_handle = f"wf_{app_id}_sched_{run_id}"
                state.total_runs += 1

                record = ScheduleExecutionRecord(
                    run_id=run_id,
                    app_id=app_id,
                    workflow_handle=workflow_handle,
                    schedule_workflow_handle=state.schedule_workflow_handle,
                )
                self._append_record(app_id, record, state.max_history)

                task = asyncio.create_task(
                    self._execute_single_run(app_id, run_id, workflow_handle, state),
                    name=f"sched_run_{app_id}_{run_id}",
                )
                state.active_runs[run_id] = task
                # 发布一次调度级别的状态: 标记新 run 已启动
                # 注意：此处发布的是 schedule 汇总快照（含 active_runs 列表），
                # 并不包含子运行的逐节点 execution_plan，除非后续调用
                # _publish_schedule_viz_state 时把 latest_run_state 一并合并。
                self._publish_schedule_viz_state(
                    state,
                    node_name="run_started",
                    extra={
                        "schedule_status": "running",
                        "last_run_id": run_id,
                        "last_workflow_handle": workflow_handle,
                    },
                )

                logger.info(
                    f"[Scheduler] 触发第 {state.total_runs} 次执行: "
                    f"app_id={app_id}, run_id={run_id}, "
                    f"active={len(state.active_runs)}"
                )

        except asyncio.CancelledError:
            logger.info(f"[Scheduler] 调度循环已取消: app_id={app_id}")
        except Exception as e:
            self._publish_schedule_viz_state(
                state,
                node_name="schedule_failed",
                extra={"schedule_status": "failed", "schedule_error": str(e)},
                finish_status="failed",
                error=str(e),
            )
            logger.error(f"[Scheduler] 调度循环异常退出: app_id={app_id}, error={e}")

    async def _execute_single_run(
        self,
        app_id: str,
        run_id: str,
        workflow_handle: str,
        state: _ScheduleState,
    ):
        """执行单次工作流"""
        try:
            from src.app.app_logic_engine import get_app_logic_engine

            engine = get_app_logic_engine()

            # 子运行的状态回调：当子运行内部有状态更新时，调度器会通过此回调
            # 接收子运行的部分或完整 state（run_state），并将其合并到调度会话
            # 的 payload 中发布。前端订阅调度会话即可看到当前子运行的
            # execution_plan / progress 等实时信息，无需为每次子 run 单独打开
            # 一个 viz workflow。
            def _on_state_update(run_state: Dict[str, Any], node_name: str) -> None:
                self._publish_schedule_viz_state(
                    state,
                    node_name=node_name,
                    extra={
                        "schedule_status": "running",
                        "last_run_id": run_id,
                        "last_workflow_handle": workflow_handle,
                        "current_run_id": run_id,
                        "current_workflow_handle": workflow_handle,
                        "current_run_node": node_name,
                    },
                    latest_run_state=run_state,
                )

            # 调用引擎执行单次工作流，并把上面定义的 state 回调传入，
            # 以便在子运行内部发生节点级别更新时，能够把该更新回传到
            # 调度会话的可视化快照中。
            result = await engine.run_single_workflow(
                app_id,
                workflow_handle,
                state_callback=_on_state_update,
            )

            # 更新记录为成功
            result_summary = str(result)[:500] if result else ""
            self._update_record(
                app_id, run_id,
                status="completed",
                result_summary=result_summary,
            )
            state.active_runs.pop(run_id, None)
            self._publish_schedule_viz_state(
                state,
                node_name="run_completed",
                extra={
                    "schedule_status": "running",
                    "last_run_id": run_id,
                    "last_workflow_handle": workflow_handle,
                    "last_run_result_preview": result_summary,
                },
                latest_run_state=result if isinstance(result, dict) else None,
            )
            logger.info(f"[Scheduler] 执行完成: app_id={app_id}, run_id={run_id}")
            return result

        except asyncio.CancelledError:
            self._update_record(app_id, run_id, status="cancelled")
            state.active_runs.pop(run_id, None)
            self._publish_schedule_viz_state(
                state,
                node_name="run_cancelled",
                extra={
                    "schedule_status": "running",
                    "last_run_id": run_id,
                    "last_workflow_handle": workflow_handle,
                },
            )
            logger.info(f"[Scheduler] 执行被取消: app_id={app_id}, run_id={run_id}")
            raise

        except Exception as e:
            self._update_record(
                app_id, run_id,
                status="failed",
                error=str(e),
            )
            state.active_runs.pop(run_id, None)
            self._publish_schedule_viz_state(
                state,
                node_name="run_failed",
                extra={
                    "schedule_status": "running",
                    "last_run_id": run_id,
                    "last_workflow_handle": workflow_handle,
                    "last_run_error": str(e),
                },
            )
            logger.error(
                f"[Scheduler] 执行失败: app_id={app_id}, run_id={run_id}, "
                f"error={e}"
            )

    # ------------------------------------------------------------------
    # 活跃 runs 清理
    # ------------------------------------------------------------------

    def _cleanup_done_runs(self, app_id: str, state: _ScheduleState):
        """清理已完成的 active_runs 并更新记录"""
        done_run_ids = [
            rid for rid, t in state.active_runs.items() if t.done()
        ]
        for rid in done_run_ids:
            task = state.active_runs.pop(rid)
            # 如果记录仍为 running，根据 task 结果更新
            record = self._find_record(app_id, rid)
            if record and record.status == "running":
                exc = task.exception() if not task.cancelled() else None
                if task.cancelled():
                    record.status = "cancelled"
                elif exc:
                    record.status = "failed"
                    record.error = str(exc)
                else:
                    record.status = "completed"
                    result = task.result()
                    record.result_summary = str(result)[:500] if result else ""
                record.finished_at = datetime.utcnow().isoformat()
                self._save_history()

    def _register_schedule_viz_record(self, state: _ScheduleState):
        """在可视化总线中注册一个周期调度主记录。"""
        try:
            from src.service.viz_bus import get_viz_bus

            bus = get_viz_bus()
            title = f"Schedule {state.app_id}"
            bus.register(title=title, workflow_id=state.schedule_workflow_handle)
            self._publish_schedule_viz_state(
                state,
                node_name="schedule_started",
                extra={"schedule_status": "running"},
            )
        except Exception as e:
            logger.warning(f"[Scheduler] 注册可视化调度记录失败: {e}")

    def _publish_schedule_viz_state(
        self,
        state: _ScheduleState,
        node_name: str,
        extra: Optional[Dict[str, Any]] = None,
        latest_run_state: Optional[Dict[str, Any]] = None,
        finish_status: Optional[str] = None,
        error: Optional[str] = None,
    ):
        """将当前调度会话聚合状态推送到可视化总线。"""
        try:
            from src.service.viz_bus import get_viz_bus

            bus = get_viz_bus()
            current_state = self._schedules.get(state.app_id, state)
            payload = {
                "view_type": "schedule",
                "app_id": state.app_id,
                "schedule_workflow_handle": state.schedule_workflow_handle,
                "schedule_interval_seconds": state.interval_seconds,
                "schedule_max_parallel": state.max_parallel,
                "schedule_started_at": state.started_at,
                "schedule_total_runs": state.total_runs,
                "schedule_active_runs": list(current_state.active_runs.keys()),
                "schedule_active_count": len(current_state.active_runs),
            }
            # 如果调用方传入了 latest_run_state（子运行的 state），则把它合并到
            # 调度会话的 payload 中。这样前端在只订阅 schedule 会话的情况下
            # 也能看到当前子运行的 execution_plan / progress / timeline 等字段。
            if latest_run_state and isinstance(latest_run_state, dict):
                payload.update(latest_run_state)
            if extra:
                payload.update(extra)
            # 强制把调度会话标识写回去，避免子运行状态覆盖后丢失调度语义。
            payload["view_type"] = "schedule"
            payload["schedule_workflow_handle"] = state.schedule_workflow_handle
            payload["app_id"] = state.app_id
            payload["schedule_total_runs"] = state.total_runs
            payload["schedule_active_runs"] = list(current_state.active_runs.keys())
            payload["schedule_active_count"] = len(current_state.active_runs)
            payload["orchestration_mode"] = "schedule"
            bus.update_state(state.schedule_workflow_handle, payload, node_name=node_name)
            if finish_status:
                bus.finish(
                    state.schedule_workflow_handle,
                    status=finish_status,
                    final_state=payload,
                    error=error,
                )
        except Exception as e:
            logger.warning(f"[Scheduler] 更新可视化调度记录失败: {e}")

    # ------------------------------------------------------------------
    # 执行历史管理
    # ------------------------------------------------------------------

    def _append_record(
        self, app_id: str, record: ScheduleExecutionRecord, max_history: int
    ):
        """追加记录，超过上限时淘汰最旧的"""
        if app_id not in self._history:
            self._history[app_id] = []
        self._history[app_id].append(record)
        # 淘汰旧记录
        if len(self._history[app_id]) > max_history:
            self._history[app_id] = self._history[app_id][-max_history:]
        self._save_history()

    def _update_record(
        self,
        app_id: str,
        run_id: str,
        status: str = "",
        result_summary: str = "",
        error: str = "",
    ):
        """更新执行记录"""
        record = self._find_record(app_id, run_id)
        if not record:
            return
        if status:
            record.status = status
        if result_summary:
            record.result_summary = result_summary
        if error:
            record.error = error
        record.finished_at = datetime.utcnow().isoformat()
        self._save_history()

    def _find_record(
        self, app_id: str, run_id: str
    ) -> Optional[ScheduleExecutionRecord]:
        """按 run_id 查找记录"""
        for record in self._history.get(app_id, []):
            if record.run_id == run_id:
                return record
        return None

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _save_history(self):
        """将执行历史写入磁盘"""
        try:
            data = {
                app_id: [r.to_dict() for r in records]
                for app_id, records in self._history.items()
            }
            with open(self._history_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[Scheduler] 保存执行历史失败: {e}")

    def _load_history(self):
        """从磁盘加载执行历史"""
        if not os.path.exists(self._history_path):
            return
        try:
            with open(self._history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for app_id, records_dicts in data.items():
                self._history[app_id] = [
                    ScheduleExecutionRecord(**rd) for rd in records_dicts
                ]
            logger.info(
                f"[Scheduler] 从磁盘加载执行历史: "
                f"{sum(len(v) for v in self._history.values())} 条记录"
            )
        except Exception as e:
            logger.warning(f"[Scheduler] 加载执行历史失败: {e}")


# ======================================================================
# 单例访问
# ======================================================================

_scheduler_instance: Optional[WorkflowScheduler] = None


def get_workflow_scheduler() -> WorkflowScheduler:
    """获取全局 WorkflowScheduler 单例"""
    global _scheduler_instance
    if _scheduler_instance is None:
        _scheduler_instance = WorkflowScheduler()
    return _scheduler_instance
