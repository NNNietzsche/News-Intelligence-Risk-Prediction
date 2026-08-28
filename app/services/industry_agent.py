"""行业分析智能体的后台编排器。

将网页请求与搜索、证据整理、LLM 生成解耦：浏览器获得 job_id 后立刻恢复交互，
耗时工作改由独立线程和独立 SQLite 会话完成。
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime

from app.database.industry_db import industry_session
from app.services.industry_analysis import IndustryAnalysisService

logger = logging.getLogger(__name__)


@dataclass
class IndustryAgentJob:
    job_id: str
    sector_key: str
    report_id: int
    status: str = "queued"
    stage: str = "已加入智能体队列"
    progress: int = 5
    result_report_id: int | None = None
    error: str | None = None
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None

    def payload(self) -> dict:
        return asdict(self)


class IndustryAgentRunner:
    """单进程后台任务器；SQLite 结果仍持久化在行业库中。"""

    def __init__(self) -> None:
        self._jobs: dict[str, IndustryAgentJob] = {}
        self._active_by_report: dict[tuple[str, int], str] = {}
        self._lock = threading.Lock()

    def submit(self, *, sector_key: str, report_id: int) -> IndustryAgentJob:
        key = (sector_key, report_id)
        with self._lock:
            active_id = self._active_by_report.get(key)
            if active_id and active_id in self._jobs:
                active = self._jobs[active_id]
                if active.status in {"queued", "running"}:
                    return active
            job = IndustryAgentJob(
                job_id=uuid.uuid4().hex,
                sector_key=sector_key,
                report_id=report_id,
                created_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            )
            self._jobs[job.job_id] = job
            self._active_by_report[key] = job.job_id
        thread = threading.Thread(
            target=self._run,
            args=(job.job_id,),
            daemon=True,
            name=f"industry-agent-{report_id}",
        )
        thread.start()
        return job

    def get(self, job_id: str) -> IndustryAgentJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _update(self, job_id: str, **values: object) -> IndustryAgentJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for name, value in values.items():
                setattr(job, name, value)
            return job

    def _run(self, job_id: str) -> None:
        job = self._update(
            job_id,
            status="running",
            stage="正在整理已选来源并生成结构化报告",
            progress=25,
            started_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
        )
        if job is None:
            return
        try:
            # Modified by DingJiaye: 2026-08-27 — 后台线程使用独立行业 Session，
            # 不复用 HTTP 请求线程的数据库连接，避免页面卡住或 SQLite 跨线程错误。
            with industry_session(job.sector_key) as db:
                report = IndustryAnalysisService(db).generate_report(job.report_id)
            self._update(
                job_id,
                status="completed",
                stage="报告已生成",
                progress=100,
                result_report_id=report.id,
                finished_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            )
        except Exception as exc:
            logger.exception("行业分析智能体任务失败: job_id=%s", job_id)
            self._update(
                job_id,
                status="failed",
                stage="生成失败",
                progress=100,
                error=str(exc),
                finished_at=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            )
        finally:
            with self._lock:
                current = self._jobs.get(job_id)
                if current:
                    self._active_by_report.pop((current.sector_key, current.report_id), None)


industry_agent_runner = IndustryAgentRunner()
