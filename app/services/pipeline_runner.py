"""异步流水线任务：按作用域防重入 + 后台线程执行 + 启动时冻结配置快照。

不同界面（近24小时 / 7×24 / 主体评估）可并行采集；同一作用域内仍互斥。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any, Optional

from app.config import MODULE_CODES, NEWS_WINDOW_HOURS_24, get_settings, news_window_label
from app.database.models import PipelineJob, ReportRun
from app.database.session import SessionLocal
from app.services.direct_site_config import load_direct_sites_config
from app.services.pipeline import RiskPipeline
from app.services.rss_news import RssNewsCollector
from app.timeutil import tokyo_isoformat, tokyo_now

logger = logging.getLogger(__name__)

# 全局登记表锁；各作用域可并行跑任务
_registry_lock = threading.Lock()
_running_by_scope: dict[str, str] = {}  # scope -> job_id
_scope_by_job: dict[str, str] = {}  # job_id -> scope
_job_snapshots: dict[str, dict[str, Any]] = {}


def job_scope(
    *,
    module_codes: Optional[list[str]] = None,
    entity_id: Optional[int] = None,
    window_hours: int = NEWS_WINDOW_HOURS_24,
) -> str:
    """采集作用域：近24小时 / 7×24 / 主体 彼此独立。"""
    hours = int(window_hours or NEWS_WINDOW_HOURS_24)
    if entity_id is not None:
        return f"entity:{int(entity_id)}"
    codes = sorted({str(c).upper() for c in (module_codes or []) if c})
    if codes == ["A"]:
        return "entity:all"
    if codes and set(codes) <= {"B", "C", "D"}:
        return f"news:{hours}"
    if codes:
        return f"mod:{hours}:{','.join(codes)}"
    return f"news:{hours}"


def get_running_job_id(scope: Optional[str] = None) -> Optional[str]:
    """有 scope 时返回该作用域任务；否则返回任意一个运行中任务（供「下次生效」提示）。"""
    with _registry_lock:
        if scope:
            return _running_by_scope.get(scope)
        if not _running_by_scope:
            return None
        return next(iter(_running_by_scope.values()))


def list_running_job_ids() -> list[str]:
    with _registry_lock:
        return list(_running_by_scope.values())


def recover_stale_jobs() -> int:
    """进程重启后，将库中遗留的 queued/running 标记为失败，避免前端一直等待幽灵任务。"""
    with _registry_lock:
        _running_by_scope.clear()
        _scope_by_job.clear()
        _job_snapshots.clear()
    db = SessionLocal()
    try:
        rows = (
            db.query(PipelineJob)
            .filter(PipelineJob.status.in_(("queued", "running")))
            .all()
        )
        count = 0
        for row in rows:
            row.status = "failed"
            row.error_message = "服务重启，任务中断"
            row.message = "任务已中断，请重新采集"
            row.finished_at = tokyo_now()
            count += 1
        if count:
            db.commit()
            logger.warning("已清理 %s 个遗留流水线任务", count)
        return count
    finally:
        db.close()


def get_current_job(
    *,
    window_hours: Optional[int] = None,
    entity_id: Optional[int] = None,
    module_codes: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """返回匹配作用域的运行中任务；无筛选时返回任意一个。"""
    if window_hours is not None or entity_id is not None or module_codes is not None:
        hours = int(window_hours if window_hours is not None else NEWS_WINDOW_HOURS_24)
        scope = job_scope(
            module_codes=module_codes,
            entity_id=entity_id,
            window_hours=hours,
        )
        jid = get_running_job_id(scope)
    else:
        jid = get_running_job_id()
    if not jid:
        return None
    return get_job_status(jid)


def _build_job_snapshot(db, entity_id: Optional[int] = None) -> dict[str, Any]:
    """任务启动快照。

    风险日报 / 主体评估不再读取用户上传的权威数据源；
    权威材料仅供深度研报使用。运行中改域名名单不影响本次采集。
    """
    from app.services.domain_rules import get_active_blacklist, get_active_whitelist

    return {
        "frozen_at": datetime.utcnow().isoformat() + "Z",
        "entity_id": entity_id,
        "source_ids": [],
        "source_names": [],
        "authority_chars": 0,
        "authority_text": "",
        "whitelist_by_module": {
            code: get_active_whitelist(db, code) for code in MODULE_CODES
        },
        "blacklist": get_active_blacklist(db),
    }


def _snapshot_for_storage(snapshot: dict[str, Any]) -> str:
    """落库时去掉大文本，避免 pipeline_jobs 膨胀；完整文本仅驻留内存。"""
    meta = {
        "frozen_at": snapshot.get("frozen_at"),
        "entity_id": snapshot.get("entity_id"),
        "source_ids": snapshot.get("source_ids") or [],
        "source_names": snapshot.get("source_names") or [],
        "authority_chars": snapshot.get("authority_chars") or 0,
        "window_hours": snapshot.get("window_hours"),
        "scope": snapshot.get("scope"),
        "whitelist_by_module": snapshot.get("whitelist_by_module") or {},
        "blacklist": snapshot.get("blacklist") or [],
    }
    return json.dumps(meta, ensure_ascii=False)


def start_pipeline_job(
    *,
    report_date: date,
    module_codes: Optional[list[str]] = None,
    entity_id: Optional[int] = None,
    window_hours: Optional[int] = None,
) -> dict[str, Any]:
    """启动异步任务；仅当同一作用域已有任务时返回冲突。"""
    codes = [c.upper() for c in (module_codes or list(MODULE_CODES.keys()))]
    invalid = [c for c in codes if c not in MODULE_CODES]
    if invalid:
        raise ValueError(f"未知模块: {', '.join(invalid)}")

    settings = get_settings()
    hours = int(
        window_hours
        if window_hours is not None
        else (getattr(settings, "news_window_hours", NEWS_WINDOW_HOURS_24) or NEWS_WINDOW_HOURS_24)
    )
    if hours < 1:
        hours = NEWS_WINDOW_HOURS_24

    scope = job_scope(module_codes=codes, entity_id=entity_id, window_hours=hours)

    with _registry_lock:
        existing = _running_by_scope.get(scope)
        if existing:
            return {
                "accepted": False,
                "job_id": existing,
                "status": "conflict",
                "scope": scope,
                "message": "该界面已有情报智能体任务在运行，请稍后再试或轮询当前状态",
            }
        job_id = uuid.uuid4().hex[:16]
        _running_by_scope[scope] = job_id
        _scope_by_job[job_id] = scope

    db = SessionLocal()
    try:
        snapshot = _build_job_snapshot(db, entity_id=entity_id)
        snapshot["window_hours"] = hours
        snapshot["scope"] = scope
        _job_snapshots[job_id] = snapshot
        row = PipelineJob(
            job_id=job_id,
            report_date=report_date,
            module_codes=json.dumps(codes, ensure_ascii=False),
            window_hours=hours,
            status="queued",
            message=(
                f"情报智能体任务已排队（主体#{entity_id}，近{news_window_label(hours)}）"
                if entity_id
                else (
                    f"情报智能体任务已排队（全部监控主体，近{news_window_label(hours)}）"
                    if codes == ["A"]
                    else f"情报智能体任务已排队（近{news_window_label(hours)}）"
                )
            ),
            snapshot_json=_snapshot_for_storage(snapshot),
        )
        db.add(row)
        db.commit()
    except Exception:
        with _registry_lock:
            if _running_by_scope.get(scope) == job_id:
                _running_by_scope.pop(scope, None)
            _scope_by_job.pop(job_id, None)
        _job_snapshots.pop(job_id, None)
        db.close()
        raise
    db.close()

    thread = threading.Thread(
        target=_execute_job,
        args=(job_id, report_date, codes, hours),
        name=f"pipeline-job-{job_id}",
        daemon=True,
    )
    thread.start()
    return {
        "accepted": True,
        "job_id": job_id,
        "status": "queued",
        "report_date": report_date.isoformat(),
        "module_codes": codes,
        "entity_id": entity_id,
        "window_hours": hours,
        "scope": scope,
        "message": f"情报智能体已启动（近{news_window_label(hours)}），正在后台处理",
        "snapshot": {
            "source_ids": snapshot.get("source_ids") or [],
            "authority_chars": snapshot.get("authority_chars") or 0,
            "frozen_at": snapshot.get("frozen_at"),
            "entity_id": entity_id,
            "window_hours": hours,
            "scope": scope,
        },
    }


def get_job_status(job_id: str) -> Optional[dict[str, Any]]:
    db = SessionLocal()
    try:
        row = db.query(PipelineJob).filter(PipelineJob.job_id == job_id).first()
        if not row:
            return None
        results = {}
        funnel = None
        snapshot_meta = None
        try:
            results = json.loads(row.results_json or "{}")
        except json.JSONDecodeError:
            results = {}
        try:
            funnel = json.loads(row.funnel_json) if row.funnel_json else None
        except json.JSONDecodeError:
            funnel = None
        try:
            snapshot_meta = json.loads(row.snapshot_json) if row.snapshot_json else None
        except json.JSONDecodeError:
            snapshot_meta = None
        return {
            "job_id": row.job_id,
            "report_date": row.report_date.isoformat(),
            "module_codes": json.loads(row.module_codes or "[]"),
            "window_hours": int(getattr(row, "window_hours", None) or NEWS_WINDOW_HOURS_24),
            "status": row.status,
            "results": results,
            "funnel": funnel,
            "message": row.message or "",
            "error_message": row.error_message,
            "started_at": tokyo_isoformat(row.started_at),
            "finished_at": tokyo_isoformat(row.finished_at),
            "running_job_id": get_running_job_id(),
            "scope": (
                (snapshot_meta or {}).get("scope")
                if isinstance(snapshot_meta, dict)
                else None
            )
            or _scope_by_job.get(job_id),
            "snapshot": snapshot_meta,
            "entity_id": (snapshot_meta or {}).get("entity_id") if isinstance(snapshot_meta, dict) else None,
        }
    finally:
        db.close()


def _run_one_module(
    job_id: str,
    report_date: date,
    code: str,
    authority_text: str,
    rss: RssNewsCollector,
    entity_id: Optional[int] = None,
    window_hours: int = NEWS_WINDOW_HOURS_24,
    direct_sites_config=None,
) -> tuple[str, int, Optional[dict], Optional[str]]:
    """每个模块独立 Session；权威文本与 RSS/直连配置来自任务快照。"""
    # Modified by DingJiaye: 2026-08-27 — 展示当前真实处理板块，避免页面长期只显示
    # 泛化的“检索与核验”文案而让用户误以为任务卡死。
    target_label = f"主体#{entity_id}" if entity_id else MODULE_CODES.get(code, code)
    _set_job_message(job_id, f"情报智能体正在检索与核验：{target_label}…")
    snapshot = _job_snapshots.get(job_id) or {}
    whitelist_map = snapshot.get("whitelist_by_module") or {}
    db = SessionLocal()
    try:
        pipeline = RiskPipeline(
            db,
            job_id=job_id,
            authority_text=authority_text,
            rss=rss,
            entity_id=entity_id,
            window_hours=window_hours,
            direct_sites_config=direct_sites_config,
            domain_whitelist=whitelist_map.get(code),
            domain_blacklist=snapshot.get("blacklist"),
        )
        count = pipeline.run_module(code, report_date)
        funnel = None
        run = (
            db.query(ReportRun)
            .filter(
                ReportRun.report_date == report_date,
                ReportRun.module_code == code,
                ReportRun.window_hours == window_hours,
            )
            .first()
        )
        if run and run.funnel_json:
            try:
                funnel = json.loads(run.funnel_json)
            except json.JSONDecodeError:
                funnel = None
        return code, count, funnel, None
    except Exception as exc:
        logger.exception("模块并行执行失败 %s", code)
        return code, -1, None, str(exc)
    finally:
        db.close()


def _set_job_message(job_id: str, message: str) -> None:
    db = SessionLocal()
    try:
        row = db.query(PipelineJob).filter(PipelineJob.job_id == job_id).first()
        if row:
            row.message = message
            db.commit()
    except Exception:
        db.rollback()
        logger.debug("更新采集进度失败", exc_info=True)
    finally:
        db.close()


def _run_module_a_all_entities(
    *,
    job_id: str,
    report_date: date,
    authority_text: str,
    rss: RssNewsCollector,
    window_hours: int,
    direct_sites_config,
) -> tuple[int, dict[str, Any], list[str]]:
    from app.services.entity_credit import list_active_entity_ids

    db = SessionLocal()
    try:
        entity_ids = list_active_entity_ids(db)
    finally:
        db.close()

    total = 0
    merged: dict[str, Any] = {"entities_planned": len(entity_ids), "saved_total": 0}
    errors: list[str] = []
    for index, entity_id in enumerate(entity_ids, 1):
        _set_job_message(job_id, f"采集进行中…（{index}/{len(entity_ids)} 个主体）")
        _code, count, funnel, err = _run_one_module(
            job_id,
            report_date,
            "A",
            authority_text,
            rss,
            entity_id,
            window_hours,
            direct_sites_config,
        )
        if isinstance(count, int) and count > 0:
            total += count
        if funnel:
            merged[f"entity_{entity_id}"] = {
                "saved": funnel.get("saved"),
                "google_supplement": funnel.get("google_supplement"),
                "source_fail": funnel.get("source_fail"),
            }
        if err:
            errors.append(f"A#{entity_id}: {err}")

    merged["saved_total"] = total
    db = SessionLocal()
    try:
        run = (
            db.query(ReportRun)
            .filter(
                ReportRun.report_date == report_date,
                ReportRun.module_code == "A",
                ReportRun.window_hours == window_hours,
            )
            .first()
        )
        if run:
            run.entry_count = total
            run.status = "failed" if errors and total <= 0 else "completed"
            run.notes = f"已采集 {len(entity_ids)} 个监控主体，入库 {total} 条"
            db.commit()
    except Exception:
        db.rollback()
        logger.exception("汇总主体采集结果失败")
    finally:
        db.close()
    return total, merged, errors


def _execute_modules(
    job_id: str,
    report_date: date,
    codes: list[str],
    authority_text: str,
    entity_id: Optional[int] = None,
    window_hours: int = NEWS_WINDOW_HOURS_24,
) -> tuple[dict[str, int], dict[str, Any], list[str]]:
    settings = get_settings()
    results: dict[str, int] = {}
    funnels: dict[str, Any] = {}
    errors: list[str] = []

    # 任务级冻结 RSS / 直连站点配置（热加载与运行中变更不影响本次）
    rss = RssNewsCollector(
        retry_attempts=int(getattr(settings, "network_retry_attempts", 3) or 3),
        retry_backoff=float(getattr(settings, "network_retry_backoff_seconds", 1.5) or 1.5),
        fetch_workers=int(getattr(settings, "pipeline_rss_fetch_workers", 6) or 6),
    )
    direct_path = getattr(settings, "direct_sites_config_path", None) or None
    direct_sites_config = load_direct_sites_config(direct_path)

    remaining = list(codes)
    if "A" in remaining and entity_id is None:
        remaining = [code for code in remaining if code != "A"]
        count, funnel, errs = _run_module_a_all_entities(
            job_id=job_id,
            report_date=report_date,
            authority_text=authority_text,
            rss=rss,
            window_hours=window_hours,
            direct_sites_config=direct_sites_config,
        )
        results["A"] = count
        if funnel:
            funnels["A"] = funnel
        errors.extend(errs)
    codes = remaining
    if not codes:
        return results, funnels, errors

    parallel = bool(getattr(settings, "pipeline_module_parallel", False)) and len(codes) > 1
    if parallel:
        workers = min(4, len(codes))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    _run_one_module,
                    job_id,
                    report_date,
                    code,
                    authority_text,
                    rss,
                    entity_id,
                    window_hours,
                    direct_sites_config,
                ): code
                for code in codes
            }
            for fut in as_completed(futures):
                code, count, funnel, err = fut.result()
                results[code] = count
                if funnel:
                    funnels[code] = funnel
                if err:
                    errors.append(f"{code}: {err}")
    else:
        for code in codes:
            code, count, funnel, err = _run_one_module(
                job_id,
                report_date,
                code,
                authority_text,
                rss,
                entity_id,
                window_hours,
                direct_sites_config,
            )
            results[code] = count
            if funnel:
                funnels[code] = funnel
            if err:
                errors.append(f"{code}: {err}")
    return results, funnels, errors


def _release_job_scope(job_id: str) -> None:
    with _registry_lock:
        scope = _scope_by_job.pop(job_id, None)
        if scope and _running_by_scope.get(scope) == job_id:
            _running_by_scope.pop(scope, None)
    _job_snapshots.pop(job_id, None)


def _execute_job(
    job_id: str,
    report_date: date,
    codes: list[str],
    window_hours: int = NEWS_WINDOW_HOURS_24,
) -> None:
    snapshot = _job_snapshots.get(job_id) or {}
    authority_text = snapshot.get("authority_text") or ""
    entity_id = snapshot.get("entity_id")
    hours = int(snapshot.get("window_hours") or window_hours or NEWS_WINDOW_HOURS_24)
    db = SessionLocal()
    try:
        row = db.query(PipelineJob).filter(PipelineJob.job_id == job_id).first()
        if row:
            row.status = "running"
            row.started_at = tokyo_now()
            n = len(snapshot.get("source_ids") or [])
            row.message = (
                f"采集进行中…（近{news_window_label(hours)}，已冻结 {n} 个数据源）"
            )
            db.commit()
    except Exception:
        db.rollback()
        _release_job_scope(job_id)
        raise
    finally:
        db.close()

    try:
        results, funnels, errors = _execute_modules(
            job_id,
            report_date,
            codes,
            authority_text,
            entity_id=entity_id,
            window_hours=hours,
        )

        # 新闻日报三板块跑完后补齐双投镜像，避免模块互清
        news_codes = [c for c in codes if c in ("B", "C", "D")]
        if news_codes and not (errors and all(results.get(c, -1) == -1 for c in news_codes)):
            try:
                sync_db = SessionLocal()
                try:
                    mirrored = RiskPipeline(
                        sync_db, window_hours=hours
                    ).sync_dual_route_mirrors(report_date)
                    if mirrored:
                        funnels["_dual_sync"] = {"mirrored": mirrored}
                        logger.info("双投同步完成：镜像 %s 条", mirrored)
                finally:
                    sync_db.close()
            except Exception:
                logger.exception("双投同步失败（不影响主采集结果）")

        db = SessionLocal()
        try:
            row = db.query(PipelineJob).filter(PipelineJob.job_id == job_id).first()
            if row:
                row.results_json = json.dumps(results, ensure_ascii=False)
                row.funnel_json = json.dumps(funnels, ensure_ascii=False)
                row.finished_at = tokyo_now()
                if errors and all(v == -1 for v in results.values()):
                    row.status = "failed"
                    row.error_message = "; ".join(errors)
                    row.message = "采集失败（已尽量保留上次结果）"
                else:
                    row.status = "completed"
                    row.message = f"近{news_window_label(hours)}资讯采集完成" + (
                        f"（部分失败: {'; '.join(errors)}）" if errors else ""
                    )
                db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.exception("异步流水线任务崩溃: %s", job_id)
        db = SessionLocal()
        try:
            row = db.query(PipelineJob).filter(PipelineJob.job_id == job_id).first()
            if row:
                row.status = "failed"
                row.error_message = str(exc)
                row.message = "任务异常结束"
                row.finished_at = tokyo_now()
                db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
    finally:
        _release_job_scope(job_id)


def get_last_news_refresh(
    *,
    window_hours: int = NEWS_WINDOW_HOURS_24,
    module_codes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """最近一次新闻采集完成时间（东京 ISO），供界面同步刷新文案。"""
    hours = int(window_hours or NEWS_WINDOW_HOURS_24)
    codes = [c.upper() for c in (module_codes or ["B", "C", "D"])]
    scope = job_scope(module_codes=codes, window_hours=hours)
    running = get_running_job_id(scope)
    db = SessionLocal()
    try:
        q = (
            db.query(PipelineJob)
            .filter(PipelineJob.status == "completed")
            .filter(PipelineJob.window_hours == hours)
            .filter(PipelineJob.finished_at.isnot(None))
            .order_by(PipelineJob.finished_at.desc())
        )
        finished_at = None
        job_id = None
        report_date = None
        for row in q.limit(30):
            try:
                job_codes = json.loads(row.module_codes or "[]")
            except json.JSONDecodeError:
                job_codes = []
            job_codes_u = {str(c).upper() for c in job_codes}
            if job_codes_u and job_codes_u <= set(codes):
                finished_at = row.finished_at
                job_id = row.job_id
                report_date = row.report_date.isoformat() if row.report_date else None
                break
        if finished_at is None:
            run = (
                db.query(ReportRun)
                .filter(ReportRun.module_code.in_(codes))
                .filter(ReportRun.window_hours == hours)
                .filter(ReportRun.finished_at.isnot(None))
                .filter(ReportRun.status.in_(("completed", "empty")))
                .order_by(ReportRun.finished_at.desc())
                .first()
            )
            if run:
                finished_at = run.finished_at
                report_date = run.report_date.isoformat() if run.report_date else None
        return {
            "scope": scope,
            "window_hours": hours,
            "finished_at": tokyo_isoformat(finished_at),
            "job_id": job_id,
            "report_date": report_date,
            "running": bool(running),
            "running_job_id": running,
        }
    finally:
        db.close()


def news_hour_slot_satisfied(
    *,
    window_hours: int = NEWS_WINDOW_HOURS_24,
    module_codes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """当前东京整点时段是否已有新闻采集（手动或自动），用于整点任务与手动合并。

    规则：同作用域任务进行中，或本小时内已有成功完成的新闻采集 → 视为已满足，整点可跳过。
    """
    hours = int(window_hours or NEWS_WINDOW_HOURS_24)
    codes = [c.upper() for c in (module_codes or ["B", "C", "D"])]
    scope = job_scope(module_codes=codes, window_hours=hours)
    running = get_running_job_id(scope)
    if running:
        return {
            "satisfied": True,
            "reason": "running",
            "scope": scope,
            "job_id": running,
            "finished_at": None,
        }

    now = tokyo_now()
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    db = SessionLocal()
    try:
        q = (
            db.query(PipelineJob)
            .filter(PipelineJob.status == "completed")
            .filter(PipelineJob.window_hours == hours)
            .filter(PipelineJob.finished_at.isnot(None))
            .filter(PipelineJob.finished_at >= hour_start)
            .order_by(PipelineJob.finished_at.desc())
        )
        for row in q.limit(30):
            try:
                job_codes = json.loads(row.module_codes or "[]")
            except json.JSONDecodeError:
                job_codes = []
            job_codes_u = {str(c).upper() for c in job_codes}
            if job_codes_u and job_codes_u <= set(codes):
                return {
                    "satisfied": True,
                    "reason": "completed_this_hour",
                    "scope": scope,
                    "job_id": row.job_id,
                    "finished_at": tokyo_isoformat(row.finished_at),
                }
        run = (
            db.query(ReportRun)
            .filter(ReportRun.module_code.in_(codes))
            .filter(ReportRun.window_hours == hours)
            .filter(ReportRun.finished_at.isnot(None))
            .filter(ReportRun.finished_at >= hour_start)
            .filter(ReportRun.status.in_(("completed", "empty")))
            .order_by(ReportRun.finished_at.desc())
            .first()
        )
        if run:
            return {
                "satisfied": True,
                "reason": "completed_this_hour",
                "scope": scope,
                "job_id": None,
                "finished_at": tokyo_isoformat(run.finished_at),
            }
        return {
            "satisfied": False,
            "reason": "none",
            "scope": scope,
            "job_id": None,
            "finished_at": None,
        }
    finally:
        db.close()


def run_modules_sync(
    *,
    report_date: date,
    module_codes: Optional[list[str]] = None,
    entity_id: Optional[int] = None,
    window_hours: Optional[int] = None,
) -> dict[str, Any]:
    """同步执行（调度器 / 调试用）。与同作用域异步任务互斥。"""
    codes = [c.upper() for c in (module_codes or list(MODULE_CODES.keys()))]
    settings = get_settings()
    hours = int(
        window_hours
        if window_hours is not None
        else (getattr(settings, "news_window_hours", NEWS_WINDOW_HOURS_24) or NEWS_WINDOW_HOURS_24)
    )
    if hours < 1:
        hours = NEWS_WINDOW_HOURS_24
    scope = job_scope(module_codes=codes, entity_id=entity_id, window_hours=hours)
    with _registry_lock:
        existing = _running_by_scope.get(scope)
        if existing:
            return {
                "ok": False,
                "results": {c: -1 for c in codes},
                "message": f"该界面已有任务在运行: {existing}",
            }
        job_id = f"sync-{uuid.uuid4().hex[:10]}"
        _running_by_scope[scope] = job_id
        _scope_by_job[job_id] = scope
    try:
        db = SessionLocal()
        try:
            snapshot = _build_job_snapshot(db, entity_id=entity_id)
            snapshot["window_hours"] = hours
            snapshot["scope"] = scope
        finally:
            db.close()
        _job_snapshots[job_id] = snapshot
        results, _funnels, errors = _execute_modules(
            job_id,
            report_date,
            codes,
            snapshot.get("authority_text") or "",
            entity_id=entity_id,
            window_hours=hours,
        )
        return {
            "ok": not (errors and all(v == -1 for v in results.values())),
            "results": results,
            "message": f"近{news_window_label(hours)}资讯采集完成"
            + (f"（部分失败: {'; '.join(errors)}）" if errors else ""),
            "errors": errors,
            "entity_id": entity_id,
            "window_hours": hours,
        }
    finally:
        _release_job_scope(job_id)
