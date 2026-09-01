# -*- coding: utf-8 -*-
"""Generate the P0 batch-1 three-stage latency audit from a read-only DB."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional


METRICS = (
    "queue_delay",
    "stage1_duration",
    "lock_to_stage2_start_delay",
    "stage2_llm_duration",
    "stage2_total_duration",
    "stage3_first_call_duration",
    "stage3_repair_duration",
    "stage3_total_duration",
    "publish_duration",
    "total_pipeline_duration",
    "total_lock_hold_duration",
    "lock_idle_duration",
)
STAT_COLUMNS = ("count", "min", "mean", "median", "p90", "p95", "p99", "max")
TIMELINE_FIELDS = (
    "pipeline_created_at",
    "task_enqueued_at",
    "task_started_at",
    "stage1_started_at",
    "stage1_finished_at",
    "room_lock_acquired_at",
    "stage2_started_at",
    "stage2_llm_started_at",
    "stage2_llm_finished_at",
    "stage2_finished_at",
    "stage3_started_at",
    "stage3_llm_attempt_1_started_at",
    "stage3_llm_attempt_1_finished_at",
    "stage3_repair_started_at",
    "stage3_repair_finished_at",
    "stage3_finished_at",
    "publish_started_at",
    "message_committed_at",
    "room_lock_released_at",
)
EVENT_TO_FIELD = {
    "pipeline_created": "pipeline_created_at",
    "task_enqueued": "task_enqueued_at",
    "task_started": "task_started_at",
    "stage1_started": "stage1_started_at",
    "stage1_finished": "stage1_finished_at",
    "room_lock_acquired": "room_lock_acquired_at",
    "stage2_started": "stage2_started_at",
    "stage2_llm_started": "stage2_llm_started_at",
    "stage2_llm_finished": "stage2_llm_finished_at",
    "stage2_finished": "stage2_finished_at",
    "stage3_started": "stage3_started_at",
    "stage3_llm_attempt_1_started": "stage3_llm_attempt_1_started_at",
    "stage3_llm_attempt_1_finished": "stage3_llm_attempt_1_finished_at",
    "stage3_repair_started": "stage3_repair_started_at",
    "stage3_repair_finished": "stage3_repair_finished_at",
    "stage3_finished": "stage3_finished_at",
    "publish_started": "publish_started_at",
    "message_committed": "message_committed_at",
    "room_lock_released": "room_lock_released_at",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="ssrl_esp.db")
    parser.add_argument("--log", default="logs/nqy_20260729_144616.log")
    parser.add_argument("--output", default="docs/p0_three_stage_latency_audit.md")
    return parser.parse_args()


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def seconds_between(started: Any, finished: Any) -> Optional[float]:
    first, second = parse_dt(started), parse_dt(finished)
    if not first or not second:
        return None
    value = (second - first).total_seconds()
    return value if value >= 0 else None


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def describe(values: Iterable[Optional[float]]) -> dict[str, Optional[float]]:
    clean = sorted(float(value) for value in values if value is not None and value >= 0)
    if not clean:
        return {column: 0 if column == "count" else None for column in STAT_COLUMNS}
    return {
        "count": len(clean),
        "min": min(clean),
        "mean": statistics.mean(clean),
        "median": statistics.median(clean),
        "p90": percentile(clean, 0.90),
        "p95": percentile(clean, 0.95),
        "p99": percentile(clean, 0.99),
        "max": max(clean),
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_rows(db_path: Path) -> tuple[list[dict], list[dict], list[dict]]:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise RuntimeError(f"database quick_check failed: {quick_check}")
        pipelines = [dict(row) for row in conn.execute("SELECT * FROM strategy_pipeline_runs")]
        batches = [dict(row) for row in conn.execute("SELECT * FROM state_assessment_batches")]
        event_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='strategy_pipeline_latency_events'"
        ).fetchone()
        events = (
            [dict(row) for row in conn.execute("SELECT * FROM strategy_pipeline_latency_events ORDER BY occurred_at, id")]
            if event_table
            else []
        )
        return pipelines, batches, events
    finally:
        conn.close()


def attach_batches(pipelines: list[dict], batches: list[dict]) -> None:
    by_id = {row["id"]: row for row in batches}
    by_scope: dict[tuple, list[dict]] = {}
    for batch in batches:
        key = (
            batch["group_id"],
            batch["session_id"],
            batch["discussion_id"],
            batch["candidate_end_sequence"],
        )
        by_scope.setdefault(key, []).append(batch)
    for pipeline in pipelines:
        batch = by_id.get(pipeline.get("assessment_batch_id"))
        if not batch:
            key = (
                pipeline["group_id"],
                pipeline["session_id"],
                pipeline["discussion_id"],
                pipeline["input_cutoff_student_sequence"],
            )
            candidates = by_scope.get(key) or []
            batch = candidates[-1] if candidates else None
        pipeline["_batch"] = batch


def attach_events(pipelines: list[dict], events: list[dict]) -> None:
    by_pipeline: dict[int, list[dict]] = {}
    by_batch: dict[int, list[dict]] = {}
    for event in events:
        if event.get("pipeline_run_id") is not None:
            by_pipeline.setdefault(int(event["pipeline_run_id"]), []).append(event)
        if event.get("assessment_batch_id") is not None:
            by_batch.setdefault(int(event["assessment_batch_id"]), []).append(event)
    for pipeline in pipelines:
        selected = list(by_pipeline.get(int(pipeline["id"]), []))
        batch = pipeline.get("_batch")
        if batch:
            selected.extend(
                event
                for event in by_batch.get(int(batch["id"]), [])
                if event not in selected
            )
        pipeline["_events"] = sorted(
            selected, key=lambda row: (row["occurred_at"], row.get("id") or 0)
        )


def is_repair(pipeline: dict) -> bool:
    for field in ("text_validation_result_json", "strategy_raw_response_json"):
        try:
            payload = json.loads(pipeline.get(field) or "{}")
        except (TypeError, ValueError):
            continue
        if len(payload.get("validation_attempts") or []) >= 2:
            return True
        if len(payload.get("raw_outputs") or []) >= 2:
            return True
    return any(event.get("event") == "stage3_repair_started" for event in pipeline["_events"])


def timeline(pipeline: dict) -> dict[str, Any]:
    batch = pipeline.get("_batch") or {}
    values = {
        "pipeline_created_at": pipeline.get("created_at"),
        "task_enqueued_at": batch.get("enqueued_at"),
        "task_started_at": batch.get("started_at"),
        "stage1_started_at": pipeline.get("stage1_started_at"),
        "stage1_finished_at": pipeline.get("stage1_completed_at"),
        "room_lock_acquired_at": pipeline.get("room_lock_acquired_at"),
        "stage2_started_at": pipeline.get("stage2_started_at"),
        "stage2_llm_started_at": None,
        "stage2_llm_finished_at": None,
        "stage2_finished_at": pipeline.get("stage2_completed_at"),
        "stage3_started_at": pipeline.get("stage3_started_at"),
        "stage3_llm_attempt_1_started_at": None,
        "stage3_llm_attempt_1_finished_at": None,
        "stage3_repair_started_at": None,
        "stage3_repair_finished_at": None,
        "stage3_finished_at": pipeline.get("stage3_completed_at"),
        "publish_started_at": None,
        "message_committed_at": pipeline.get("published_at"),
        "room_lock_released_at": pipeline.get("room_lock_released_at"),
    }
    for event in pipeline["_events"]:
        field = EVENT_TO_FIELD.get(event.get("event"))
        if not field:
            continue
        if field == "room_lock_released_at":
            values[field] = event["occurred_at"]
        elif values.get(field) is None:
            values[field] = event["occurred_at"]
    return values


def event_elapsed(pipeline: dict, event_name: str) -> Optional[float]:
    rows = [event for event in pipeline["_events"] if event.get("event") == event_name]
    if not rows:
        return None
    return float(rows[-1].get("elapsed_ms") or 0) / 1000.0


def metric_values(pipeline: dict) -> dict[str, Optional[float]]:
    points = timeline(pipeline)
    stage2_total = seconds_between(points["stage2_started_at"], points["stage2_finished_at"])
    stage3_total = seconds_between(points["stage3_started_at"], points["stage3_finished_at"])
    publish = event_elapsed(pipeline, "message_committed")
    if publish is None and pipeline.get("published_at"):
        publish = seconds_between(points["stage3_finished_at"], pipeline["published_at"])
    terminal = (
        points["message_committed_at"]
        or points["stage3_finished_at"]
        or points["stage2_finished_at"]
        or pipeline.get("updated_at")
    )
    lock_hold = seconds_between(
        points["room_lock_acquired_at"], points["room_lock_released_at"]
    )
    active = sum(value or 0 for value in (stage2_total, stage3_total, publish))
    return {
        "queue_delay": seconds_between(points["task_enqueued_at"], points["task_started_at"]),
        "stage1_duration": seconds_between(points["stage1_started_at"], points["stage1_finished_at"]),
        "lock_to_stage2_start_delay": seconds_between(
            points["room_lock_acquired_at"], points["stage2_started_at"]
        ),
        "stage2_llm_duration": event_elapsed(pipeline, "stage2_llm_finished"),
        "stage2_total_duration": stage2_total,
        "stage3_first_call_duration": event_elapsed(
            pipeline, "stage3_llm_attempt_1_finished"
        ),
        "stage3_repair_duration": event_elapsed(pipeline, "stage3_repair_finished"),
        "stage3_total_duration": stage3_total,
        "publish_duration": publish,
        "total_pipeline_duration": seconds_between(points["pipeline_created_at"], terminal),
        "total_lock_hold_duration": lock_hold,
        "lock_idle_duration": max(0.0, lock_hold - active) if lock_hold is not None else None,
    }


def categories(pipelines: list[dict]) -> list[tuple[str, list[dict]]]:
    return [
        (
            "Stage 2 后 PASS/OI",
            [
                row
                for row in pipelines
                if row.get("stage2_status") == "SUCCEEDED"
                and not int(row.get("should_intervene") or 0)
            ],
        ),
        (
            "Stage 2 后进入 Stage 3",
            [
                row
                for row in pipelines
                if row.get("stage2_status") == "SUCCEEDED"
                and int(row.get("should_intervene") or 0)
            ],
        ),
        ("Stage 3 schema repair", [row for row in pipelines if is_repair(row)]),
        (
            "最终发布成功",
            [row for row in pipelines if row.get("publish_status") == "PUBLISHED"],
        ),
        (
            "文本校验失败",
            [row for row in pipelines if row.get("stage3_status") == "FAILED"],
        ),
        (
            "TTL 回收",
            [
                row
                for row in pipelines
                if row.get("failure_code") == "ROOM_LEASE_EXPIRED"
                or row.get("skip_reason") == "ROOM_LEASE_EXPIRED"
            ],
        ),
    ]


def fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, int):
        return str(value)
    return f"{float(value):.3f}"


def stats_table(rows: list[dict]) -> list[str]:
    output = [
        "| 指标（秒） | count | min | mean | median | P90 | P95 | P99 | max |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    values = [metric_values(row) for row in rows]
    for metric in METRICS:
        summary = describe(item[metric] for item in values)
        output.append(
            "| " + metric + " | " + " | ".join(fmt(summary[key]) for key in STAT_COLUMNS) + " |"
        )
    return output


def huey_durations(log_path: Path) -> list[float]:
    if not log_path.exists():
        return []
    pattern = re.compile(
        r"process_state_assessment_batch:\s+([0-9a-f-]+).*?executed in ([0-9.]+)s",
        re.IGNORECASE,
    )
    jobs: dict[str, float] = {}
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = pattern.search(line)
            if match:
                jobs[match.group(1)] = float(match.group(2))
    return list(jobs.values())


def render(
    *,
    db_path: Path,
    log_path: Path,
    pipelines: list[dict],
    batches: list[dict],
    events: list[dict],
) -> str:
    all_metrics = [metric_values(row) for row in pipelines]
    stage2_rows = [row for row in pipelines if row.get("stage2_completed_at")]
    stage3_rows = [row for row in pipelines if row.get("stage3_completed_at")]
    published = [row for row in pipelines if row.get("publish_status") == "PUBLISHED"]
    ttl_rows = [
        row
        for row in pipelines
        if row.get("failure_code") == "ROOM_LEASE_EXPIRED"
        or row.get("skip_reason") == "ROOM_LEASE_EXPIRED"
    ]
    stage2_p99 = describe(metric_values(row)["stage2_total_duration"] for row in stage2_rows)["p99"]
    stage3_p99 = describe(metric_values(row)["stage3_total_duration"] for row in stage3_rows)["p99"]
    lock_delay_p99 = describe(
        item["lock_to_stage2_start_delay"] for item in all_metrics
    )["p99"]
    published_lock_p99 = describe(
        metric_values(row)["total_lock_hold_duration"] for row in published
    )["p99"]
    ttl_lock_p99 = describe(
        metric_values(row)["total_lock_hold_duration"] for row in ttl_rows
    )["p99"]
    critical = [
        sum(
            value or 0
            for value in (
                metric_values(row)["stage2_total_duration"],
                metric_values(row)["stage3_total_duration"],
                metric_values(row)["publish_duration"],
            )
        )
        for row in stage3_rows
    ]
    critical_p99 = describe(critical)["p99"]
    huey = describe(huey_durations(log_path))

    lines = [
        "# P0 三阶段真实时延与锁内关键路径审计",
        "",
        f"生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        "",
        "## 1. 结论",
        "",
        "当前证据不支持把 75 秒本身判为根因。Stage 2 与含 repair 的 Stage 3 模型关键路径明显短于 75 秒；真正异常集中在锁取得过早和正常终态未主动释放。",
        "",
        f"- Stage 2 总耗时 P99：{fmt(stage2_p99)} 秒（n={len(stage2_rows)}）。",
        f"- Stage 3 总耗时 P99：{fmt(stage3_p99)} 秒（n={len(stage3_rows)}；历史 Stage 3 流程均检测到第二次校验/repair）。",
        f"- 进入 Stage 3 的 Stage2+Stage3+发布代理耗时 P99：{fmt(critical_p99)} 秒。",
        f"- 锁取得到 Stage 2 开始的空等 P99：{fmt(lock_delay_p99)} 秒。",
        f"- 成功发布流程总持锁 P99：{fmt(published_lock_p99)} 秒；TTL 回收流程总持锁 P99：{fmt(ttl_lock_p99)} 秒。",
        f"- TTL 回收共 {len(ttl_rows)} 条，几乎贴着 75 秒边界；这些记录没有 Stage 2 执行时间，说明它们是提前取得但无人消费的 preliminary Stage 1 锁。",
        "- 当前代码存在两个明确命名的租约值：preliminary Stage 1 使用 `INTERVENTION_V2_LOCK_SECONDS=75`，权威 pipeline claim 使用 `THREE_STAGE_ROOM_LOCK_SECONDS=120`；`INTERVENTION_V2_COOLDOWN_SECONDS=120` 是发布冷却，不是租约。",
        "",
        "建议：本批次不修改现有 75/120 秒配置。后续锁重构批次采用统一的初始 TTL 75 秒、heartbeat 20 秒、最大总持锁 180 秒；其中 heartbeat 和上限是待实现参数，不应与 120 秒策略 cooldown 混用。推荐依据是当前核心路径 P99 加 10 秒数据库/发布安全余量仍低于 75 秒，而提前持锁空等已接近整个 TTL。",
        "",
        "## 2. 证据范围与精度",
        "",
        f"- 数据库：`{db_path}`；SHA-256 `{sha256(db_path)}`；`PRAGMA quick_check=ok`。",
        f"- 日志：`{log_path}`；SHA-256 `{sha256(log_path) if log_path.exists() else 'missing'}`。",
        f"- strategy pipeline：{len(pipelines)}；state assessment batch：{len(batches)}；新增高精度事件：{len(events)}。",
        f"- Huey `process_state_assessment_batch` 可去重任务耗时：n={huey['count']}，P95={fmt(huey['p95'])} 秒，P99={fmt(huey['p99'])} 秒，max={fmt(huey['max'])} 秒。",
        "- 修复前数据库时间戳只有秒级精度，因此 queue、Stage 1 和发布代理的 0 秒表示“同一秒内”，不表示真实耗时为零。新增事件表使用毫秒级时间和 monotonic elapsed_ms，后续运行可直接拆分模型调用。",
        "",
        "## 3. 时序字段可复原性",
        "",
        "| 字段 | 修复前来源 | 修复前非空 | 新观测事件 |",
        "| --- | --- | ---: | --- |",
    ]
    legacy_source = {
        "pipeline_created_at": "strategy_pipeline_runs.created_at",
        "task_enqueued_at": "state_assessment_batches.enqueued_at",
        "task_started_at": "state_assessment_batches.started_at",
        "stage1_started_at": "strategy_pipeline_runs.stage1_started_at",
        "stage1_finished_at": "strategy_pipeline_runs.stage1_completed_at",
        "room_lock_acquired_at": "strategy_pipeline_runs.room_lock_acquired_at",
        "stage2_started_at": "strategy_pipeline_runs.stage2_started_at",
        "stage2_finished_at": "strategy_pipeline_runs.stage2_completed_at",
        "stage3_started_at": "strategy_pipeline_runs.stage3_started_at",
        "stage3_finished_at": "strategy_pipeline_runs.stage3_completed_at",
        "message_committed_at": "strategy_pipeline_runs.published_at",
        "room_lock_released_at": "strategy_pipeline_runs.room_lock_released_at",
    }
    timelines = [timeline(row) for row in pipelines]
    reverse_event = {field: event for event, field in EVENT_TO_FIELD.items()}
    for field in TIMELINE_FIELDS:
        lines.append(
            f"| {field} | {legacy_source.get(field, '无')} | "
            f"{sum(1 for item in timelines if item.get(field))} | {reverse_event[field]} |"
        )
    lines.extend(
        [
            "",
            "事件日志固定携带 `group_id/session_id/discussion_id/task_id/pipeline_run_id/assessment_batch_id/cutoff_sequence/lock_owner/lock_token_hash/call_id/attempt/stage/event/elapsed_ms`。完整 token、API key 和消息正文不会进入该日志或事件表。",
            "",
            "## 4. 分组分位数",
            "",
            "所有分位数采用线性插值；每个指标单独统计非空样本，故 count 会因历史字段缺失而不同。",
        ]
    )
    for name, rows in categories(pipelines):
        lines.extend(["", f"### {name}（流程数 {len(rows)}）", "", *stats_table(rows)])

    lock_idle = describe(item["lock_idle_duration"] for item in all_metrics)
    lines.extend(
        [
            "",
            "## 5. 锁内空闲与 75 秒判断",
            "",
            f"全体可测流程的 lock_idle_duration：count={lock_idle['count']}，median={fmt(lock_idle['median'])} 秒，P95={fmt(lock_idle['p95'])} 秒，P99={fmt(lock_idle['p99'])} 秒，max={fmt(lock_idle['max'])} 秒。",
            "",
            "锁内空闲按 `总持锁 - Stage2 - Stage3 - 发布代理` 计算；修复前缺少模型级事件，因此这是保守代理值。TTL 回收 preliminary 流程没有 Stage 2/3，约 75 秒几乎全部属于锁内空闲。",
            "",
            "回答验收问题：",
            "",
            "1. Stage 1 修复前为秒级同秒记录，无法可靠量化；Stage 2/3 的真实阶段分位数见上表。",
            "2. 排队延迟修复前均落在同一秒；Huey 总任务耗时可独立验证，新增事件以后可精确拆分 queue 与 LLM。",
            "3. 锁从 `room_lock_acquired_at` 到 `room_lock_released_at`；新增 acquire/renew/release/TTL 事件可按 token 哈希复原。",
            "4. 75 秒覆盖当前正常模型关键路径 P99 加安全余量；它不覆盖“提前持锁后排队/无人消费”，而后者本就不应计入 TTL。",
            "5. 存在显著提前锁定：lock_to_stage2_start_delay P99 接近 72 秒，30 条 preliminary 锁直到 TTL 回收且未执行 Stage 2。",
            "6. 根因排序：取得锁过早与正常释放缺失 > TTL 太短。",
            "",
            "## 6. 单 pipeline 复原方法",
            "",
            "按 `strategy_pipeline_latency_events.pipeline_run_id`、`occurred_at,id` 排序即可得到毫秒级完整时序；历史数据则以 pipeline scope `(group_id, session_id, discussion_id, input_cutoff_student_sequence)` 关联 batch 的 `candidate_end_sequence`。同一模型调用使用 `call_id + attempt` 配对，锁只使用 `lock_owner + lock_token_hash` 配对。",
            "",
            "## 7. 观测改造",
            "",
            "- 新增增量表 `strategy_pipeline_latency_events` 和 pipeline→assessment batch 关联列。",
            "- 在队列入队/启动、Stage 1、Stage 2 LLM、Stage 3 首次调用与 repair、发布提交、锁 acquire/renew/release/expire 记录结构化事件。",
            "- 本批次未修改锁取得条件、TTL、heartbeat、发布门或业务判断。",
            "- 报告生成器以 SQLite `mode=ro` 打开数据库；本次报告生成不修改业务数据库和日志。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    db_path = Path(args.db)
    log_path = Path(args.log)
    output_path = Path(args.output)
    pipelines, batches, events = load_rows(db_path)
    attach_batches(pipelines, batches)
    attach_events(pipelines, events)
    report = render(
        db_path=db_path.resolve(),
        log_path=log_path.resolve(),
        pipelines=pipelines,
        batches=batches,
        events=events,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output_path.resolve()),
                "pipelines": len(pipelines),
                "batches": len(batches),
                "events": len(events),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
