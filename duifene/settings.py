"""运行参数。所有可调常量集中于此，避免魔数散落在业务逻辑中。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

# 可由用户在界面配置并持久化到 ini 的字段子集（其余为内部常量）。
PERSISTABLE_FIELDS: tuple[str, ...] = (
    "poll_min_s",
    "poll_max_s",
    "error_backoff_max_s",
    "monitor_start_hour",
    "monitor_end_hour",
    "small_class_max",
    "small_class_delay_s",
    "headcount_ratio",
    "headcount_deadline_s",
    "sign_delay_s",
    "stale_sign_minutes",
    "cooldown_seconds",
    "proxy_url",
)

# 派生字段：随其它字段联动计算，不接受用户直接设置，但同样持久化。
DERIVED_FIELDS: tuple[str, ...] = ("verify_tls",)

# 各可持久化字段的取值闭区间 (下限, 上限)；未列出的字段不做范围校验。
SETTING_LIMITS: dict[str, tuple[float, float]] = {
    "poll_min_s": (0.5, 60.0),
    "poll_max_s": (0.5, 300.0),
    "error_backoff_max_s": (5.0, 3600.0),
    "monitor_start_hour": (0, 24),
    "monitor_end_hour": (0, 24),
    "small_class_max": (1, 1000),
    "small_class_delay_s": (0.0, 600.0),
    "headcount_ratio": (0.0, 1.0),
    "headcount_deadline_s": (0.0, 1800.0),
    "sign_delay_s": (0.0, 600.0),
    "stale_sign_minutes": (1, 1440),
    "cooldown_seconds": (0.0, 3600.0),
}


def _coerce(current: Any, raw: Any) -> tuple[bool, Any]:
    """按字段当前值的类型把外部输入转为合适类型，返回 `(是否成功, 值)`。"""
    if isinstance(current, bool):
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True, True
        if text in ("0", "false", "no", "off"):
            return True, False
        return False, None
    if isinstance(current, int):
        try:
            return True, int(float(str(raw).strip()))
        except (TypeError, ValueError):
            return False, None
    if isinstance(current, float):
        try:
            return True, float(str(raw).strip())
        except (TypeError, ValueError):
            return False, None
    return True, str(raw)


def _normalize_proxy(raw: str) -> str:
    """把代理地址规整为 requests 可用的 URL；空串表示直连。"""
    text = raw.strip()
    if not text:
        return ""
    if "://" not in text:
        # 缺 scheme：按抓包工具常见约定补 http://
        text = "http://" + text
    return text


def _check_consistency(values: Mapping[str, Any]) -> str:
    """跨字段一致性校验；返回错误文案，通过时返回空串。"""
    if values["poll_min_s"] > values["poll_max_s"]:
        return "轮询最小间隔不能大于最大间隔"
    if values["monitor_start_hour"] >= values["monitor_end_hour"]:
        return "监听时间窗的起始小时须小于结束小时"
    proxy = _normalize_proxy(str(values.get("proxy_url", "")).strip())
    if proxy and not urlparse(proxy).hostname:
        return "代理地址格式有误，请填写形如 http://127.0.0.1:8080"
    return ""


def _normalize(values: dict[str, Any]) -> None:
    """联动归一化（就地修改）。

    证书校验不在前端暴露，取值完全由代理决定：配置代理即视为抓包调试场景，
    其证书通常无法通过系统信任链校验，故关闭校验；无代理时恢复为安全默认。
    """
    values["proxy_url"] = _normalize_proxy(str(values.get("proxy_url", "")).strip())
    values["verify_tls"] = not values["proxy_url"]


@dataclass
class Settings:
    """对分易签到工具的运行时配置。

    非 frozen：界面修改后可即时对已注入该实例的各模块生效（标量读写在
    CPython 下天然原子，足以满足单写多读场景）。
    """

    # App 接口域名：签到数据、四类签到提交、教室坐标全部走它
    app_host: str = "https://dfapp.duifene.com"
    user_agent: str = "okhttp/4.12.0"
    ini_filename: str = "duifenyi.ini"
    proxy_url: str = ""
    verify_tls: bool = True
    request_timeout_s: float = 12.0

    # 轮询：区间内随机，避免固定节律被风控识别
    poll_min_s: float = 3.0
    poll_max_s: float = 5.0
    # 连续异常时的退避上限（秒）
    error_backoff_max_s: float = 60.0

    # 活动筛选：创建超过该分钟数的活动不再处理
    stale_sign_minutes: int = 30
    # 检测到签到后延迟多少秒再提交（0=立即），用于规避「秒签」风控
    sign_delay_s: float = 0.0
    # 兜底：活动创建后超过该秒数即无视人数门槛直接签，保证不因人数接口异常而错过
    headcount_deadline_s: float = 45.0

    # 人数门禁：已签人数占总人数比例达到该值才签（0 表示不启用）
    headcount_ratio: float = 0.2
    # 小班直签：总人数低于该值且活动创建满 small_class_delay_s 秒即签
    small_class_max: int = 5
    small_class_delay_s: float = 5.0

    # 监听时间窗（左闭右开）；0–24 表示全天轮询
    monitor_start_hour: int = 0
    monitor_end_hour: int = 24

    # 命中限流后的静默秒数
    cooldown_seconds: float = 15.0
    rate_limit_markers: tuple[str, ...] = ("过于频繁", "请求频繁", "频繁")

    # 二维码/定位的重试节拍（内部常量，不暴露到界面）
    qr_retry_interval_s: float = 5.0
    location_retry_interval_s: float = 5.0
    # 取不到签到码/汉字时，同一活动最多提示一次，之后静默重试
    manual_code_ttl_s: float = 300.0

    # 定位签到的随机偏移半径（米）：落在教室范围内即可，避免每次都同一坐标
    location_offset_min_m: float = 3.0
    location_offset_max_m: float = 18.0

    @property
    def proxies(self) -> dict[str, str] | None:
        """requests 代理映射；未配置时返回 None。"""
        if not self.proxy_url:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

    def persistable_values(self) -> dict[str, Any]:
        """导出待持久化的字段值（:data:`PERSISTABLE_FIELDS` + 派生字段）。"""
        names = PERSISTABLE_FIELDS + DERIVED_FIELDS
        return {name: getattr(self, name) for name in names}

    def editable_values(self) -> dict[str, Any]:
        """导出用户可直接编辑的字段值（不含派生字段），供前端渲染表单。"""
        return {name: getattr(self, name) for name in PERSISTABLE_FIELDS}

    def apply(self, patch: Mapping[str, Any]) -> str:
        """校验并应用一批配置更新（原地修改）。

        仅接受 :data:`PERSISTABLE_FIELDS` 中的字段；逐字段做类型转换与范围
        校验，再整体做跨字段一致性校验与派生字段归一化，全部通过后才写入，
        保证原子性。
        """
        staged: dict[str, Any] = self.persistable_values()
        for name, raw in patch.items():
            if name in DERIVED_FIELDS:
                return f"字段 {name} 由系统派生，不可直接设置"
            if name not in PERSISTABLE_FIELDS:
                return f"不支持修改字段：{name}"
            ok, value = _coerce(getattr(self, name), raw)
            if not ok:
                return f"字段 {name} 取值非法：{raw!r}"
            limits = SETTING_LIMITS.get(name)
            if limits is not None:
                low, high = limits
                if not (low <= value <= high):
                    return f"字段 {name} 须在 {low} ~ {high} 之间"
            staged[name] = value
        error = _check_consistency(staged)
        if error:
            return error
        _normalize(staged)
        for name, value in staged.items():
            setattr(self, name, value)
        return ""
