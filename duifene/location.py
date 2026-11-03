"""教室坐标反推：距离预言机 + 三边测量（业务核心）。"""

from __future__ import annotations

import math
import time

import requests

from .api import DuifeneApi
from .errors import RateLimited
from .models import Course
from .settings import Settings
from .trilateration import haversine, solve_trilateration


class LocationResolver:
    """以学生权限反推教室坐标。

    以中性城市为圆心、在其周围大半径环上多方位测距，再用三边测量解出教室
    坐标。使用中性城市而非学校真实位置，避免从探测点分布反推出使用者所在
    区域；采样中心只决定探测环圆心，不影响解算结果。

    本类不产生日志：反推过程（探测点、残差、耗时）对用户无价值，只把最终
    坐标与拟合残差返回给调用方，由调用方决定如何记录结果。
    """

    def __init__(self, settings: Settings, api: DuifeneApi) -> None:
        self._settings = settings
        self._api = api
        # 采样进度：被限流打断时保留已采到的点，冷却后从断点继续，避免每轮都从头再采
        self._class_id = ""
        self._samples: list[tuple[float, float, float]] = []
        self._cursor = 0
        self._batch_start = True

    def resolve(self, course: Course) -> tuple[float, float, float] | None:
        """远场多方位测距后反推教室坐标，返回 `(经度, 纬度, 拟合残差米)`。

        每次探测都是一次真实签到请求，服务端按「最后一次签到请求」计时，因此：
        按 ``probe_interval_s`` 限速、静默期内完全不探测、被限流打断时保留进度，
        下一轮从断点续采——否则一次「过于频繁」就等于整场定位失败。
        """
        if self._class_id != course.class_id:
            self._reset(course.class_id)
        self._batch_start = True  # 本轮首次探测不再等间隔
        center_lat, center_lon = self._settings.probe_center
        total = self._settings.probe_azimuths
        while self._cursor < total:
            if self._api.limiter.is_blocking():
                return None  # 冷却中：保留样本，等静默结束接着采
            if not self._batch_start:
                time.sleep(self._settings.probe_interval_s)
            self._batch_start = False
            index = self._cursor
            angle = 2 * math.pi * index / total + 0.1
            dlat = (
                self._settings.probe_radius_m
                * math.sin(angle)
                / self._settings.meters_per_deg_lat
            )
            dlon = self._settings.probe_radius_m * math.cos(angle) / (
                self._settings.meters_per_deg_lon * math.cos(math.radians(center_lat))
            )
            lat, lon = center_lat + dlat, center_lon + dlon
            try:
                distance = self._api.probe_distance(course, lon, lat)
            except RateLimited:
                return None
            except requests.exceptions.RequestException:
                self._cursor += 1
                continue
            if distance is not None:
                self._samples.append((lat, lon, distance))
            self._cursor += 1
        samples = self._samples
        self._reset(course.class_id)
        if len(samples) < self._settings.probe_min_samples:
            return None
        try:
            lat, lon = solve_trilateration(samples)
        except Exception:
            return None
        residual = max(
            abs(haversine(la, lo, lat, lon) - dist) for la, lo, dist in samples
        )
        if residual > self._settings.probe_max_residual_m:  # 残差过大说明数据异常
            return None
        return lon, lat, residual

    def _reset(self, class_id: str) -> None:
        """切换教学班或完成一次解算后清空采样进度。"""
        self._class_id = class_id
        self._samples = []
        self._cursor = 0
        self._batch_start = True
