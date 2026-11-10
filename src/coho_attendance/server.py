"""考勤查询 MCP server。

通过 stdio 暴露 4 个工具：
  * get_current_user  —— 当前登录账号信息
  * get_month_calendar —— 指定年月考勤原始数据
  * get_month_report   —— 指定年月考勤 Markdown 月报
  * get_day_detail     —— 某一天的考勤明细

凭据（账号、密码、租户码）全部从环境变量读取，任何工具都不接受凭据参数，
这样密钥不会出现在对话记录、工具入参或日志里。
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from .client import CalendarMonth, CohoClient, CohoError
from .config import ConfigError, Settings
from .summary import (
    all_month_totals,
    daily_rows,
    day_detail,
    month_metrics,
    render_month_report,
)

server = MCPServer(
    name="coho-attendance",
    version="0.1.0",
    instructions=(
        "查询 Coho WTS 考勤系统。账号、密码、租户码由服务端环境变量注入，"
        "调用工具时不需要也不能传入凭据。"
    ),
)

#: 进程内复用一个客户端，token 与 TLS 会话都能复用。
_client: CohoClient | None = None


def _get_client() -> CohoClient:
    global _client
    if _client is None:
        _client = CohoClient(Settings.from_env())
    return _client


def _handle_error(exc: Exception) -> str:
    """把异常转成给调用方看的文字；不泄露密码等敏感字段。"""
    if isinstance(exc, ConfigError):
        return f"配置错误：{exc}"
    if isinstance(exc, CohoError):
        return f"接口调用失败：{exc}"
    return f"{type(exc).__name__}: {exc}"


def _month_payload(calendar: CalendarMonth) -> dict[str, Any]:
    return {
        "period": calendar.label,
        "summary_status": calendar.summary_status,
        "metrics": [
            {"code": m.code, "name": m.name, "value": m.value} for m in month_metrics(calendar)
        ],
        "all_totals": [
            {"code": m.code, "name": m.name, "value": m.value}
            for m in all_month_totals(calendar)
        ],
        "days": daily_rows(calendar),
    }


@server.tool(
    description=(
        "获取当前登录的考勤账号信息（姓名、工号、租户、globalId）。"
        "用于确认凭据是否有效，或排查账号切换问题。"
    )
)
def get_current_user() -> str:
    try:
        user = _get_client().current_user()
    except Exception as exc:  # noqa: BLE001 - 统一转成可读文本返回
        return _handle_error(exc)

    keep = ("name", "globalId", "tenantCode", "tenantName", "identityType", "loginIp")
    return json.dumps({k: user.get(k) for k in keep if k in user}, ensure_ascii=False, indent=2)


@server.tool(
    description=(
        "获取指定年月的考勤原始日历数据，包含每日出勤、打卡明细、排班、月度合计。"
        "需要完整未加工的字段时用这个工具。"
    )
)
def get_month_calendar(year: int, month: int) -> str:
    try:
        calendar = _get_client().monthly_calendar(year, month)
    except Exception as exc:  # noqa: BLE001
        return _handle_error(exc)

    return json.dumps(calendar.raw, ensure_ascii=False, indent=2)


@server.tool(
    description=(
        "生成指定年月的考勤 Markdown 月报，含关键指标、异常明细和每日记录。"
        "想快速了解某月考勤情况时用这个工具。"
    )
)
def get_month_report(year: int, month: int) -> str:
    try:
        client = _get_client()
        calendar = client.monthly_calendar(year, month)
    except Exception as exc:  # noqa: BLE001
        return _handle_error(exc)

    try:
        username = Settings.from_env().username
    except ConfigError:
        username = None
    return render_month_report(calendar, username=username)


@server.tool(
    description=(
        "获取某一天的考勤明细，包含打卡时间、排班、当天所有非零统计项。"
        "date 格式为 YYYY-MM-DD，年月由它推导。"
    )
)
def get_day_detail(date: str) -> str:
    try:
        year_text, month_text, day_text = date.split("-")
        year, month = int(year_text), int(month_text)
    except (ValueError, AttributeError):
        return f"日期格式错误：{date!r}，应为 YYYY-MM-DD，例如 2026-10-07。"

    try:
        calendar = _get_client().monthly_calendar(year, month)
    except Exception as exc:  # noqa: BLE001
        return _handle_error(exc)

    detail = day_detail(calendar, date)
    if detail is None:
        return f"{date} 不在 {calendar.label} 的考勤数据里。"

    detail["metrics"] = [
        {"code": m.code, "name": m.name, "value": m.value} for m in detail["metrics"]
    ]
    return json.dumps(detail, ensure_ascii=False, indent=2)


@server.tool(
    description=(
        "获取指定年月的关键指标摘要（不含每日明细），"
        "适合批量对比多个月份。返回紧凑 JSON。"
    )
)
def get_month_summary(year: int, month: int) -> str:
    try:
        calendar = _get_client().monthly_calendar(year, month)
    except Exception as exc:  # noqa: BLE001
        return _handle_error(exc)

    payload = {
        "period": calendar.label,
        "summary_status": calendar.summary_status,
        "metrics": {m.code: {"name": m.name, "value": m.value} for m in month_metrics(calendar)},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main() -> None:
    """stdio 传输入口，供 MCP 客户端拉起。"""
    server.run("stdio")


if __name__ == "__main__":
    main()
