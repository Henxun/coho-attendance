"""把原始考勤 JSON 整理成报告与统计结构。

原始接口把同一个指标拆在三个列表里：
  * summaryCountList —— 整月合计，date 为 null
  * detailCountList  —— 按日展开，date 为具体日期
  * dateList         —— 每日出勤/缺勤/旷工工时 + 日期类型

这里负责把它们对齐成「一天一行」的视图，并挑出最常用的月度指标。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .client import CalendarMonth

#: 月报里最常看的指标，顺序即展示顺序。键是接口的 code。
KEY_METRICS: tuple[tuple[str, str], ...] = (
    ("quota", "应出勤时长(天)"),
    ("cqgs", "出勤工时(h)"),
    ("ljgs", "累计工时(h)"),
    ("kggs", "旷工"),
    ("cd", "迟到"),
    ("zt", "早退"),
    ("qk", "缺卡"),
    ("bk", "补卡"),
    ("qj", "请假"),
    ("jb", "加班"),
    ("chaiq", "差勤"),
    ("ot1", "一般加班"),
    ("ot2", "双休加班"),
    ("ot3", "节假加班"),
    ("re1", "夜班津贴工时(h)"),
)

#: 请假类 code 前缀，用于把 detailCountList 里的请假项归并。
_LEAVE_PREFIX = "leave_"
_BUSINESS_PREFIX = "business_"

#: 明细里这些 type 的项才值得在日报里单独列出（值为 0 的会过滤掉）。
_INTERESTING_TYPES = {"cd", "zt", "overtime", "leave", "business"}


@dataclass(frozen=True)
class Metric:
    code: str
    name: str
    value: float


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _index_by_code(items: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 code 建索引；同 code 多条时保留第一条（月度汇总里 code 唯一）。"""
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        code = item.get("code")
        if code and code not in result:
            result[code] = item
    return result


def month_metrics(calendar: CalendarMonth) -> list[Metric]:
    """取月报关键指标；接口没返回的项跳过，不编造 0。"""
    index = _index_by_code(calendar.month_totals)
    metrics: list[Metric] = []
    for code, label in KEY_METRICS:
        item = index.get(code)
        if item is None:
            continue
        metrics.append(Metric(code=code, name=item.get("name") or label, value=_to_float(item.get("value"))))
    return metrics


def all_month_totals(calendar: CalendarMonth) -> list[Metric]:
    """月度汇总的全部条目，按接口顺序。"""
    return [
        Metric(
            code=str(item.get("code")),
            name=str(item.get("name") or ""),
            value=_to_float(item.get("value")),
        )
        for item in calendar.month_totals
        if item.get("code")
    ]


def daily_rows(calendar: CalendarMonth) -> list[dict[str, Any]]:
    """把 dateList、detailCountList、dkList、shiftInfoList 对齐成一天一行。

    返回字段：
      date / day_type / attendance_hours / absence_hours / absence_days
      shift / punches / anomalies
    """
    details_by_date: dict[str, list[dict[str, Any]]] = {}
    for item in calendar.day_details:
        date = item.get("date")
        if date:
            details_by_date.setdefault(date, []).append(item)

    punches_by_date: dict[str, list[str]] = {}
    for punch in calendar.punches:
        date = punch.get("date")
        if not date:
            continue
        time_text = punch.get("time") or ""
        kind = punch.get("type") or ""
        label = f"{time_text} {kind}".strip()
        if label:
            punches_by_date.setdefault(date, []).append(label)

    shifts_by_date = {item.get("date"): item for item in calendar.shifts if item.get("date")}

    rows: list[dict[str, Any]] = []
    for day in calendar.days:
        date = day.get("date")
        if not date:
            continue

        anomalies: list[str] = []
        for item in details_by_date.get(date, []):
            value = _to_float(item.get("value"))
            if value == 0:
                continue
            kind = item.get("type")
            # 只保留真正异常的项：迟到/早退/加班/请假/公出
            if kind in _INTERESTING_TYPES or str(item.get("code", "")).startswith(
                (_LEAVE_PREFIX, _BUSINESS_PREFIX)
            ):
                anomalies.append(f"{item.get('name')}: {value:g}")

        shift = shifts_by_date.get(date)
        shift_text = ""
        if shift:
            shift_text = " ".join(
                part
                for part in (shift.get("code"), shift.get("type"), shift.get("period"))
                if part
            )

        rows.append(
            {
                "date": date,
                "day_type": day.get("type") or "工作日",
                "attendance_hours": _to_float(day.get("cqqs")),
                "absence_hours": _to_float(day.get("qqgs")),
                "absence_days": _to_float(day.get("kggs")),
                "shift": shift_text,
                "punches": punches_by_date.get(date, []),
                "anomalies": anomalies,
            }
        )

    rows.sort(key=lambda row: row["date"])
    return rows


def day_detail(calendar: CalendarMonth, date: str) -> dict[str, Any] | None:
    """单日完整明细；日期不存在返回 None。"""
    for row in daily_rows(calendar):
        if row["date"] == date:
            metrics = [
                Metric(
                    code=str(item.get("code")),
                    name=str(item.get("name") or ""),
                    value=_to_float(item.get("value")),
                )
                for item in calendar.day_details
                if item.get("date") == date
            ]
            return {**row, "metrics": [m for m in metrics if m.value != 0]}
    return None


def render_month_report(calendar: CalendarMonth, *, username: str | None = None) -> str:
    """把一个月渲染成 Markdown 报告。"""
    lines: list[str] = [f"# 考勤月报 {calendar.label}"]
    if username:
        lines.append(f"\n账号：{username}")
    lines.append(f"统计状态：{calendar.summary_status or '未知'}\n")

    metrics = month_metrics(calendar)
    if metrics:
        lines.append("## 关键指标\n")
        lines.append("| 指标 | 数值 |")
        lines.append("| --- | --- |")
        for metric in metrics:
            lines.append(f"| {metric.name} | {metric.value:g} |")
        lines.append("")

    rows = daily_rows(calendar)
    worked = [row for row in rows if row["attendance_hours"] > 0]
    abnormal = [row for row in rows if row["anomalies"] or row["absence_hours"] > 0]

    lines.append("## 出勤概况\n")
    lines.append(f"- 统计天数：{len(rows)}")
    lines.append(f"- 有出勤记录：{len(worked)} 天")
    lines.append(f"- 打卡次数：{sum(len(row['punches']) for row in rows)} 次")
    lines.append(f"- 异常天数：{len(abnormal)} 天")
    lines.append("")

    if abnormal:
        lines.append("## 异常明细\n")
        lines.append("| 日期 | 类型 | 缺勤工时 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for row in abnormal:
            notes = "；".join(row["anomalies"]) or "-"
            lines.append(
                f"| {row['date']} | {row['day_type']} | {row['absence_hours']:g} | {notes} |"
            )
        lines.append("")

    lines.append("## 每日记录\n")
    lines.append("| 日期 | 类型 | 出勤(h) | 打卡 |")
    lines.append("| --- | --- | --- | --- |")
    for row in rows:
        punches = " ".join(row["punches"]) or "-"
        lines.append(
            f"| {row['date']} | {row['day_type']} | {row['attendance_hours']:g} | {punches} |"
        )

    return "\n".join(lines)
