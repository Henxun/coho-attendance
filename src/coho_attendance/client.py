"""Coho WTS 考勤接口客户端。

请求构造逻辑沿用原始抓包验证过的实现（TLS 指纹、cookie、三次握手、签名算法都不变），
差别只在于：凭据来自环境变量、token 动态获取并缓存、返回结构化数据而不是 print。
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from dataclasses import dataclass
from typing import Any

import tls_client

from .config import Settings

#: token 提前这么多秒视为过期，避免临界时刻拿过期 token 去打请求。
_TOKEN_EXPIRY_SKEW = 120
#: 服务端没给 expTime 时的兜底有效期（接口实测返回 7200）。
_DEFAULT_TOKEN_TTL = 7200

_CODE_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)


class CohoError(RuntimeError):
    """接口调用失败或返回了非预期结构。

    message 直接面向最终用户/MCP 调用方，因此带上接口名和原始错误信息。
    """

    def __init__(self, message: str, *, endpoint: str | None = None, payload: Any = None):
        super().__init__(message)
        self.endpoint = endpoint
        self.payload = payload


@dataclass(frozen=True)
class CalendarMonth:
    """一个自然月的考勤原始数据（已解包 data 字段）。"""

    year: int
    month: int
    raw: dict[str, Any]

    @property
    def label(self) -> str:
        return f"{self.year}-{self.month:02d}"

    # --- 各列表的便捷访问 ---------------------------------------------------

    @property
    def days(self) -> list[dict[str, Any]]:
        """每日出勤汇总（dateList）。"""
        return self.raw.get("dateList") or []

    @property
    def punches(self) -> list[dict[str, Any]]:
        """打卡明细（dkList）。"""
        return self.raw.get("dkList") or []

    @property
    def month_totals(self) -> list[dict[str, Any]]:
        """月度汇总项（summaryCountList）。"""
        return self.raw.get("summaryCountList") or []

    @property
    def day_details(self) -> list[dict[str, Any]]:
        """按日展开的明细统计项（detailCountList）。"""
        return self.raw.get("detailCountList") or []

    @property
    def shifts(self) -> list[dict[str, Any]]:
        """排班信息（shiftInfoList）。"""
        return self.raw.get("shiftInfoList") or []

    @property
    def summary_status(self) -> str | None:
        """月度考勤确认状态，例如 AttUnlock。"""
        return self.raw.get("summaryStatus")


def generate_code(length: int = 32) -> str:
    """生成请求体里的 encodeData 随机串。"""
    return "".join(random.choice(_CODE_CHARS) for _ in range(length))


def _credential(password: str) -> str:
    """按服务端要求对密码做 SHA1。

    保持与已验证跑通的原始实现完全一致：小写十六进制，不加盐。
    租户若配置了 secretKey / 非 sha1 算法，需另行调整，见 README。
    """
    return hashlib.sha1(password.encode()).hexdigest()


class CohoClient:
    """线程安全的考勤接口客户端，token 在实例内缓存复用。"""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._session = tls_client.Session(
            client_identifier="chrome_112",
            random_tls_extension_order=True,
        )
        self._cookies = {"sso_tenant_code": settings.tenant_code}
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._global_id: str | None = None

    # --- 基础设施 -----------------------------------------------------------

    def _headers(
        self,
        token: str | None = None,
        *,
        referer_path: str = "user/login",
    ) -> dict[str, str]:
        base = self._settings.base_url
        tenant = self._settings.tenant_code
        referer = f"{base}/{referer_path}"
        if referer_path == "user/login":
            referer = f"{referer}?tenantCode={tenant}&sso_tenant_code={tenant}"

        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "COHO-language": "zh-CN",
            "Connection": "keep-alive",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": base,
            "Referer": referer,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": _USER_AGENT,
            "sec-ch-ua": '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _parse(self, response: Any, endpoint: str) -> dict[str, Any]:
        """把响应解析成字典，并在业务失败时抛出可读异常。"""
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - 网关返回 HTML 时也走这里
            snippet = (getattr(response, "text", "") or "")[:300]
            raise CohoError(
                f"{endpoint} 返回的不是 JSON（HTTP {response.status_code}）。"
                f"常见原因是缺少租户 cookie 或租户码不对。响应片段：{snippet}",
                endpoint=endpoint,
            ) from exc

        if not isinstance(body, dict):
            raise CohoError(
                f"{endpoint} 返回结构异常：期望对象，实际 {type(body).__name__}。",
                endpoint=endpoint,
                payload=body,
            )

        # 业务层同时存在 code 和 ok 两种标记，任一明确失败即视为失败。
        ok_flag = body.get("ok")
        code = body.get("code")
        if ok_flag is False or (code not in (None, 200, 0)):
            msg = body.get("msg") or body.get("detailErrMsg") or "未知错误"
            raise CohoError(
                f"{endpoint} 调用失败：code={code} msg={msg}",
                endpoint=endpoint,
                payload=body,
            )

        return body

    def _data(self, response: Any, endpoint: str) -> dict[str, Any]:
        body = self._parse(response, endpoint)
        data = body.get("data")
        if not isinstance(data, dict):
            raise CohoError(
                f"{endpoint} 返回的 data 不是对象：{data!r}",
                endpoint=endpoint,
                payload=body,
            )
        return data

    # --- 认证 ---------------------------------------------------------------

    @property
    def token(self) -> str:
        """取缓存 token；已过期或不存在则重新登录。"""
        with self._lock:
            if self._token and time.time() < self._token_expires_at:
                return self._token
            return self._login_locked()

    def _login_locked(self) -> str:
        """调用方需已持有 self._lock。"""
        endpoint = "/auth/account/requestToken"
        url = f"{self._settings.gateway}{endpoint}"
        payload = {
            "sign": "",
            "timeStamp": "",
            "data": {
                "tenantCode": self._settings.tenant_code,
                "identifier": self._settings.username,
                "credential": _credential(self._settings.password),
            },
        }

        response = self._session.post(
            url,
            cookies=self._cookies,
            headers=self._headers(),
            json=payload,
        )
        data = self._data(response, endpoint)

        token = data.get("token")
        if not token:
            raise CohoError(
                "登录成功但没有拿到 token，可能账号或密码不正确。",
                endpoint=endpoint,
                payload=data,
            )

        try:
            ttl = int(data.get("expTime") or _DEFAULT_TOKEN_TTL)
        except (TypeError, ValueError):
            ttl = _DEFAULT_TOKEN_TTL

        self._token = token
        self._token_expires_at = time.time() + max(ttl - _TOKEN_EXPIRY_SKEW, 60)
        # token 变了，用户信息缓存也要跟着失效
        self._global_id = None
        return token

    def invalidate_token(self) -> None:
        """外部发现 token 失效时调用，下次访问会重新登录。"""
        with self._lock:
            self._token = None
            self._token_expires_at = 0.0
            self._global_id = None

    def current_user(self) -> dict[str, Any]:
        """当前登录用户信息，包含 globalId。"""
        endpoint = "/auth/user/getCurrentUser"
        response = self._session.get(
            f"{self._settings.gateway}{endpoint}",
            cookies=self._cookies,
            headers=self._headers(self.token),
        )
        return self._data(response, endpoint)

    @property
    def global_id(self) -> str:
        """当前用户 globalId，缓存复用；日历接口的 userId 用的就是它。"""
        with self._lock:
            if self._global_id:
                return self._global_id
        # 注意：current_user() 内部会再次获取 token，不能在持锁时调用以免递归
        user = self.current_user()
        global_id = user.get("globalId")
        if not global_id:
            raise CohoError(
                "getCurrentUser 没有返回 globalId，无法查询考勤。",
                endpoint="/auth/user/getCurrentUser",
                payload=user,
            )
        with self._lock:
            self._global_id = str(global_id)
        return self._global_id

    # --- 考勤 ---------------------------------------------------------------

    def monthly_calendar(self, year: int, month: int) -> CalendarMonth:
        """拉取指定年月（month 为 1-12）的月度考勤日历。"""
        if not 1 <= month <= 12:
            raise ValueError(f"month 必须在 1-12 之间，收到 {month}")
        if not 1900 <= year <= 2999:
            raise ValueError(f"year 看起来不合法：{year}")

        endpoint = "/wtspro-attendance/attSummaryComfirm/attCalendar"
        access_token = self.token
        user_id = self.global_id

        # 服务端按 yyyyMM 识别考勤周期，个位月份需补零。
        period = f"{year}{month:02d}"
        data = {
            "userId": user_id,
            "kqqsId": period,
            "kqqs": period,
        }

        timestamp = int(time.time() * 1000)
        key = hashlib.md5(f"{access_token}&{timestamp}".encode())
        sign = hashlib.md5(
            f"{hashlib.md5(key.digest())}&{timestamp}&{user_id}&{json.dumps(data)}".encode()
        ).hexdigest()

        payload = {
            "encodeData": generate_code(),
            "timeStamp": timestamp,
            "data": data,
            "key": key.hexdigest(),
            "sign": sign,
        }

        response = self._session.post(
            f"{self._settings.gateway}{endpoint}",
            cookies=self._cookies,
            headers=self._headers(access_token, referer_path="Workbench/WorkbenchCalendar"),
            json=payload,
        )
        raw = self._data(response, endpoint)
        return CalendarMonth(year=year, month=month, raw=raw)

    def close(self) -> None:
        """释放底层 TLS 会话。"""
        session = getattr(self, "_session", None)
        if session is not None and hasattr(session, "close"):
            try:
                session.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应影响主流程
                pass
