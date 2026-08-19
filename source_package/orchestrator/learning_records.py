"""学员学习记录：完成一轮知识点训练后落盘（JSONL 追加写），供学习记录页与画像汇总查询。

存储设计（0818 需求 5/6）：
- 文件位于运行时持久卷（容器 /app/runtime/learning_records/learning_records.jsonl），
  追加写 + 进程内锁；竞赛规模（单机、低并发）下无需引入数据库。
- 游客（未登录）不落盘账号字段（account_user_id=null），仅登录学员可跨会话查询；
- 画像汇总按 profile_id 聚合误区命中频次，供后续讲义/追问针对性引导迭代。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def tier_from_status(status: Any) -> int:
    """mastery_status → 蛛网档位（0-3）。未知档位按保守 1 处理。"""

    text = str(status or "")
    if "advanced" in text:
        return 3
    if "applied" in text:
        return 2
    if "mastered" in text or "correct" in text:
        return 3
    if "needs_training" in text or "pending_training" in text:
        return 0
    return 1


def build_learning_record(
    *,
    account: Mapping[str, Any] | None,
    profile_id: str,
    knowledge_point: str,
    started_at: str,
    finished_at: str,
    mastery_plan: list[dict[str, Any]] | None,
    follow_up_turns: list[dict[str, Any]] | None,
    unresolved_misconceptions: list[str] | None,
    sql_failure_count: int,
    query_count: int,
    follow_up_rounds: int,
    achievement: str,
    outcome: str,
    training_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """组装一条学习记录（时间字段含日期/开始/结束/时长，直接可展示）。"""

    start = _parse_iso(started_at)
    finish = _parse_iso(finished_at) or datetime.now(timezone.utc)
    duration_seconds = max(
        0,
        int((finish - start).total_seconds()) if start else 0,
    )
    # 本地时区展示字段（服务器为东八区；若跨时区部署再引入 tz 配置）
    local_finish = finish.astimezone()

    misconception_counts: dict[str, int] = {}
    wrong_answers = 0
    for turn in follow_up_turns or []:
        assessment = str(turn.get("assessment", ""))
        if assessment and assessment != "mastered":
            wrong_answers += 1
        misconception = str(turn.get("diagnosed_misconception", "") or "")
        if misconception and misconception != "UNKNOWN":
            misconception_counts[misconception] = (
                misconception_counts.get(misconception, 0) + 1
            )
    for misconception in unresolved_misconceptions or []:
        misconception_counts[misconception] = (
            misconception_counts.get(misconception, 0) + 1
        )

    mastery = {
        str(item.get("knowledge_point", "")): tier_from_status(
            item.get("mastery_status")
        )
        for item in (mastery_plan or [])
        if isinstance(item, Mapping) and item.get("knowledge_point")
    }

    record: dict[str, Any] = {
        "record_id": f"rec-{finish.strftime('%Y%m%d%H%M%S')}-{knowledge_point[:12]}",
        "account_user_id": str(account.get("user_id", "")) if account else None,
        "account_username": str(account.get("username", "")) if account else None,
        "profile_id": profile_id,
        "knowledge_point": knowledge_point,
        "started_at": started_at,
        "finished_at": finished_at or _now_iso(),
        "date": local_finish.strftime("%Y-%m-%d"),
        "start_time": start.astimezone().strftime("%H:%M") if start else "",
        "end_time": local_finish.strftime("%H:%M"),
        "duration_seconds": duration_seconds,
        "mastery": mastery,
        "misconception_hits": dict(
            sorted(
                misconception_counts.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        ),
        "wrong_answer_rounds": wrong_answers,
        "sql_failure_count": int(sql_failure_count),
        "query_count": int(query_count),
        "follow_up_rounds": int(follow_up_rounds),
        "achievement": str(achievement),
        "outcome": str(outcome),
    }
    if training_report:
        record["difficulty"] = {
            "initial": training_report.get("initial_difficulty"),
            "final": training_report.get("final_difficulty"),
        }
        record["pretest_score"] = training_report.get("pretest_score")
    return record


class LearningRecordStore:
    """JSONL 追加写学习记录；读取按账号过滤，汇总按画像聚合。"""

    def __init__(self, records_dir: Path) -> None:
        self._dir = Path(records_dir)
        self._path = self._dir / "learning_records.jsonl"
        self._lock = threading.Lock()

    def append(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def list_records(
        self,
        *,
        user_id: str | None = None,
        profile_id: str | None = None,
    ) -> list[dict[str, Any]]:
        records = self._read_all()
        if user_id is not None:
            records = [
                item
                for item in records
                if item.get("account_user_id") == user_id
            ]
        if profile_id is not None:
            records = [
                item for item in records if item.get("profile_id") == profile_id
            ]
        records.sort(key=lambda item: str(item.get("finished_at", "")))
        return records

    def summarize_by_profile(self) -> list[dict[str, Any]]:
        """画像维度汇总：轮次、平均时长、误区命中频次、平均最终档位。

        误区频次是"系统后续在讲义/提问中针对性引导"的数据基础（0818 需求 6）。
        """

        records = self._read_all()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in records:
            grouped.setdefault(str(item.get("profile_id", "")), []).append(item)
        summary: list[dict[str, Any]] = []
        for profile, items in sorted(grouped.items()):
            misconception_counts: dict[str, int] = {}
            tier_sum: dict[str, list[int]] = {}
            total_duration = 0
            wrong_rounds = 0
            sql_failures = 0
            for item in items:
                for misconception, count in (
                    item.get("misconception_hits") or {}
                ).items():
                    misconception_counts[misconception] = (
                        misconception_counts.get(misconception, 0) + int(count)
                    )
                for point, tier in (item.get("mastery") or {}).items():
                    tier_sum.setdefault(point, []).append(int(tier))
                total_duration += int(item.get("duration_seconds", 0))
                wrong_rounds += int(item.get("wrong_answer_rounds", 0))
                sql_failures += int(item.get("sql_failure_count", 0))
            summary.append({
                "profile_id": profile,
                "round_count": len(items),
                "learner_count": len({
                    item.get("account_user_id")
                    for item in items
                    if item.get("account_user_id")
                }),
                "avg_duration_seconds": (
                    round(total_duration / len(items)) if items else 0
                ),
                "misconception_frequency": dict(
                    sorted(
                        misconception_counts.items(),
                        key=lambda entry: entry[1],
                        reverse=True,
                    )
                ),
                "avg_mastery": {
                    point: round(sum(tiers) / len(tiers), 2)
                    for point, tiers in sorted(tier_sum.items())
                },
                "wrong_answer_rounds": wrong_rounds,
                "sql_failure_total": sql_failures,
            })
        return summary

    def _read_all(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self._lock:
            with self._path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return records
