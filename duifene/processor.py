"""签到业务核心：单个活动的判定与执行。"""

from __future__ import annotations

import time

from . import qrdecode
from .api import DuifeneApi
from .config import IniStore
from .errors import LogFn, RateLimited
from .location import LocationResolver
from .models import CheckInActivity, Course, Outcome
from .push import PushMonitor
from .settings import Settings


class ActivityProcessor:
    """判定单个活动的可签性并执行签到。

    跨轮次状态（已处理集合、人数提示缓存、WS 等待起点）内聚于本类，
    由 :meth:`reset` 在切换课程或重新开始监听时清空。
    """

    def __init__(
        self,
        settings: Settings,
        api: DuifeneApi,
        push: PushMonitor,
        resolver: LocationResolver,
        store: IniStore,
        log: LogFn,
    ) -> None:
        self._settings = settings
        self._api = api
        self._push = push
        self._resolver = resolver
        self._store = store
        self._log = log
        self._handled: set[str] = set()
        self._headcount_log: dict[str, tuple[int, int]] = {}
        self._wait_start: dict[str, float] = {}
        # 各门禁的提示去重标记：同活动同状态只提示一次，避免逐轮刷屏
        self._gate_log: dict[str, object] = {}
        # 已出过结果的活动：签到失败不进 ``_handled``，活动会每轮重试；此集合
        # 作为静音开关，令同活动的提交与结果文案只在首轮输出，避免逐轮刷屏。
        self._result_logged: set[str] = set()
        # 二维码签到：令牌滚动轮换，需按节拍重试并限制轮次
        self._qr_last: dict[str, float] = {}
        self._qr_rounds: dict[str, int] = {}
        # 取码/取答案的重试轮次：服务端迟迟不下发时按上限收口
        self._answer_rounds: dict[str, int] = {}

    def reset(self) -> None:
        """清空跨轮次状态。"""
        self._handled.clear()
        self._headcount_log.clear()
        self._wait_start.clear()
        self._gate_log.clear()
        self._result_logged.clear()
        self._qr_last.clear()
        self._qr_rounds.clear()
        self._answer_rounds.clear()

    def _log_attempt(self, activity_id: str, course: Course, message: str) -> None:
        """输出签到尝试文案；该活动已出过结果时静默。"""
        if activity_id in self._result_logged:
            return
        self._log_course(course, message)

    def _log_result(
        self, activity_id: str, course: Course, message: str, *, force: bool = False
    ) -> None:
        """输出签到结果文案，并把该活动标记为已出结果（后续静默）。

        ``force`` 为真时无视静音：用于「先失败、后重试成功」的场合，否则最关键
        的成功回执会被首轮失败留下的静音标记吞掉。
        """
        if activity_id in self._result_logged and not force:
            return
        self._result_logged.add(activity_id)
        self._log_course(course, message)

    def _log_course(self, course: Course, message: str) -> None:
        """带课程前缀的日志——多门课共用同一面板，前缀使记录可归属到课程。"""
        self._log(f"课程: {course.name} {message}")

    def process(self, activity: CheckInActivity, course: Course) -> Outcome:
        """处理一条活动，返回处理结果。"""
        if activity.id in self._handled:
            return Outcome.SKIPPED
        # 服务端已记录（含重启前所签）或已出勤：标记后不再关注
        if (
            activity.check_in_status == "1"
            or activity.status_id == "1"
            or activity.status_name == "出勤"
        ):
            self._mark(course, activity.id, "已签到，无需重复签到")
            return Outcome.SKIPPED
        if activity.can_apply != "1":
            return Outcome.SKIPPED
        if activity.remaining_seconds(self._settings.sign_end_offset_hours) < 0:
            self._mark(course, activity.id, "已超过签到结束时间")
            return Outcome.SKIPPED
        age = activity.age_seconds()
        if age is None:
            return Outcome.SKIPPED
        if age < 0 and -age > self._settings.future_tolerance_s:
            self._mark(course, activity.id, f"创建时间异常（{activity.create_date}）")
            return Outcome.SKIPPED
        if age > self._settings.stale_sign_minutes * 60:
            self._mark(
                course,
                activity.id,
                f"创建已超过 {self._settings.stale_sign_minutes} 分钟，视为旧活动",
            )
            return Outcome.SKIPPED
        if self._api.limiter.is_blocking():
            left = int(self._api.limiter.remaining())
            self._log_course(course, f"限流中，{left} 秒后恢复")
            return Outcome.DEFERRED
        gate = self._delay_gate(activity, age, course)
        if gate is not None:
            return gate
        gate = self._headcount_gate(activity, age, course)
        if gate is not None:
            return gate
        return self._execute(activity, course)

    def _delay_gate(
        self, activity: CheckInActivity, age: float, course: Course
    ) -> Outcome | None:
        """延迟门禁：检测到签到后等待 sign_delay_s 秒再签；未启用返回 None。"""
        delay = self._settings.sign_delay_s
        if delay <= 0 or age >= delay:
            return None
        left = int(delay - age)
        marker: object = left
        if self._gate_log.get(activity.id) != marker:  # 剩余秒数变化时提示
            self._gate_log[activity.id] = marker
            self._log_course(
                course, f"活动 {activity.id} 延迟等待中，还剩 {left} 秒签到"
            )
        return Outcome.DEFERRED

    def _headcount_gate(
        self, activity: CheckInActivity, age: float, course: Course
    ) -> Outcome | None:
        """人数门禁：需继续等待返回 :attr:`Outcome.DEFERRED`，可签返回 None。"""
        counts = self._api.fetch_headcount(activity.id)
        if counts is None:
            # 人数接口异常时若静默等待，四类签到会一起「毫无反应」，必须留痕
            if self._gate_log.get(activity.id) != "headcount_failed":
                self._gate_log[activity.id] = "headcount_failed"
                self._log_course(
                    course, f"活动 {activity.id} 人数获取失败，暂不签到，稍后重试"
                )
            return Outcome.DEFERRED
        if not counts[1]:
            return Outcome.DEFERRED
        signed, total = counts
        if self._settings.min_headcount > 0 and signed < self._settings.min_headcount:
            marker: object = signed
            if self._gate_log.get(activity.id) != marker:  # 仅人数变化时提示
                self._gate_log[activity.id] = marker
                self._log_course(
                    course,
                    f"活动 {activity.id} 已签 {signed}/{total}，"
                    f"未达 {self._settings.min_headcount} 人门槛，继续等待",
                )
            return Outcome.DEFERRED
        if total < self._settings.small_class_max:
            if age < self._settings.small_class_delay_s:
                wait = int(self._settings.small_class_delay_s - age)
                self._log_course(
                    course, f"活动 {activity.id} 小班 {total} 人，{wait} 秒后签到"
                )
                return Outcome.DEFERRED
            return None
        ratio = signed / total
        if ratio < self._settings.headcount_ratio:
            marker = (signed, total)
            if self._headcount_log.get(activity.id) != marker:  # 仅人数变化时提示
                self._headcount_log[activity.id] = marker
                self._log_course(
                    course,
                    f"活动 {activity.id} 已签 {signed}/{total}（{ratio:.0%}），"
                    f"未达 {self._settings.headcount_ratio:.0%} 门槛，继续等待",
                )
            return Outcome.DEFERRED
        return None

    def _execute(self, activity: CheckInActivity, course: Course) -> Outcome:
        """按签到类型执行；命中限流时降级为 :attr:`Outcome.DEFERRED`。"""
        try:
            if activity.type == "1":
                outcome = self._sign_code(activity, course)
            elif activity.type == "2":
                outcome = self._sign_qr(activity, course)
            elif activity.type == "3":
                outcome = self._sign_location(activity, course)
            elif activity.type == "4":
                outcome = self._sign_hanzi(activity, course)
            else:
                self._log_course(
                    course, f"活动 {activity.id} 类型未知（{activity.type}），跳过"
                )
                return Outcome.SKIPPED
        except RateLimited as exc:
            self._log_course(course, f"活动 {activity.id} {exc!s}")
            return Outcome.DEFERRED
        if outcome is Outcome.SIGNED:
            self._handled.add(activity.id)
            if str(activity.id) == self._push.subscribed_id:
                self._push.stop()
            self._wait_start.pop(activity.id, None)
            self._qr_last.pop(activity.id, None)
            self._qr_rounds.pop(activity.id, None)
            self._answer_rounds.pop(activity.id, None)
        return outcome

    def _sign_code(self, activity: CheckInActivity, course: Course) -> Outcome:
        """签到码签到：列表字段缺失时回退到考勤入口页取码。"""
        code = activity.code
        if not (code and len(code) == 4):
            code = self._api.fetch_code_from_entry() or ""
        if not (code and len(code) == 4):
            return self._wait_answer(activity, course, "签到码")
        self._log_attempt(activity.id, course, f"活动 {activity.id} 提交签到码 {code}")
        return self._report(activity.id, self._api.sign_by_code(code), course)

    def _wait_answer(
        self, activity: CheckInActivity, course: Course, what: str
    ) -> Outcome:
        """取不到签到码／汉字答案时的收口：先按轮次重试，超过上限再明确放弃。

        服务端可能迟迟不下发码，直接判失败会误伤；但无限重试只会每轮刷屏并拖到
        活动过期，因此用 ``answer_wait_rounds`` 卡住上限，结束时给出确定结论。
        """
        aid = activity.id
        rounds = self._answer_rounds.get(aid, 0) + 1
        self._answer_rounds[aid] = rounds
        limit = self._settings.answer_wait_rounds
        if rounds >= limit:
            self._log_result(
                aid,
                course,
                f"活动 {aid} 未取到有效{what}（已重试 {rounds} 轮），放弃",
            )
            return Outcome.SKIPPED
        marker = f"{what}_pending"
        if self._gate_log.get(aid) != marker:
            self._gate_log[aid] = marker
            self._log_course(
                course,
                f"活动 {aid} 暂未取到{what}，逐轮重试（上限 {limit} 轮）",
            )
        return Outcome.DEFERRED

    def _sign_qr(self, activity: CheckInActivity, course: Course) -> Outcome:
        """二维码签到：活动 ID 直签 → 实时令牌重试。

        服务端只认教师端滚动令牌（十几秒轮换一次），每次提交都得现取现解，因此
        成功与否取决于「这一轮拿到的令牌是否仍有效」；按 ``qr_retry_interval_s``
        节流，累计 ``qr_max_rounds`` 轮仍失败即判定活动已结束并停止重试。
        """
        aid = activity.id
        now = time.time()
        last = self._qr_last.get(aid)
        if last is not None and now - last < self._settings.qr_retry_interval_s:
            return Outcome.DEFERRED
        rounds = self._qr_rounds.get(aid, 0)
        if rounds >= self._settings.qr_max_rounds:
            self._log_result(
                aid,
                course,
                f"活动 {aid} 二维码签到失败：{self._settings.qr_max_rounds} 轮内"
                f"未取得有效令牌",
            )
            return Outcome.SKIPPED
        self._qr_rounds[aid] = rounds + 1
        self._qr_last[aid] = now

        self._api.prime_checkin_entry(course)
        message = self._api.sign_by_qr(aid)
        if not self._is_signed(message):
            # 直签被拒是预期行为（活动 ID 不再是合法 state），换实时令牌再试
            state = self._api.fetch_qr_state(course)
            if state and state != str(aid):
                message = self._api.sign_by_qr(state)
        if self._is_signed(message):
            self._log_result(aid, course, "签到成功", force=True)
            return Outcome.SIGNED
        # 失败原因分两类：本机缺解码依赖（装了就好的事）与服务端未给令牌（只能等）
        reason = message or "无响应"
        if not qrdecode.available():
            reason = (
                "本机缺少 Pillow/pyzbar，二维码无法解码；"
                "请执行 pip install -r requirements.txt"
            )
        if self._qr_rounds[aid] >= self._settings.qr_max_rounds:
            self._log_result(
                aid, course, f"活动 {aid} 二维码签到失败：{reason}"
            )
            return Outcome.SKIPPED
        if self._gate_log.get(aid) != "qr_retry":
            self._gate_log[aid] = "qr_retry"
            self._log_course(
                course,
                f"活动 {aid} 二维码未签上（{reason}），"
                f"每 {int(self._settings.qr_retry_interval_s)} 秒重试，"
                f"最多 {self._settings.qr_max_rounds} 轮",
            )
        return Outcome.DEFERRED

    def _sign_hanzi(self, activity: CheckInActivity, course: Course) -> Outcome:
        """汉字签到：取正确顺序后按序提交。

        答案依次取自列表字段、教师端页面、考勤入口页；都没取到时提示一次并继续
        等待（活动到期后由旧活动门禁收口），不再悄悄空转。
        """
        answer = activity.code
        if not (answer and len(answer) == 4):
            answer = self._api.fetch_hanzi_answer(course) or ""
        if not answer:
            return self._wait_answer(activity, course, "汉字答案")
        self._log_attempt(activity.id, course, f"活动 {activity.id} 提交汉字 {answer}")
        return self._report(activity.id, self._api.sign_by_code(answer), course)

    def _sign_location(self, activity: CheckInActivity, course: Course) -> Outcome:
        """定位签到：坐标取自 WS 推送 → 缓存 → 反推。

        过程不写日志（推送等待、缓存命中、反推采样等对用户无价值且刷屏），
        只记最终结果——失败统一为「定位签到失败，未取得教室坐标」，成功给出
        经纬度与距离。
        """
        aid = activity.id
        if self._push.subscribed_id != str(aid):
            if self._push.subscribe(aid):
                self._wait_start[aid] = time.time()
            else:
                # 订阅失败：跳过等待，直接进入回退链
                self._wait_start[aid] = time.time() - self._settings.ws_wait_timeout_s

        point = self._push.take_follow_point()
        if point is not None:
            lon, lat, distance = point
            if self._is_signed(self._api.sign_by_location(course, lon, lat)):
                self._store.save_center(course.class_id, lon, lat)
                self._log_signed_location(aid, course, lon, lat, distance)
                return Outcome.SIGNED

        waited = time.time() - self._wait_start.get(aid, 0)
        if waited < self._settings.ws_wait_timeout_s:
            return Outcome.DEFERRED

        cached = self._store.load_center(course.class_id)
        if cached is not None:
            if self._is_signed(self._api.sign_by_location(course, *cached)):
                self._log_signed_location(aid, course, *cached)
                return Outcome.SIGNED

        resolved = self._resolver.resolve(course)
        if resolved is None:
            self._log_result(aid, course, "定位签到失败，未取得教室坐标")
            return Outcome.SKIPPED
        lon, lat, residual = resolved
        message = self._api.sign_by_location(course, lon, lat)
        if self._is_signed(message):
            self._store.save_center(course.class_id, lon, lat)
            self._log_signed_location(aid, course, lon, lat, residual)
            return Outcome.SIGNED
        # 已解出坐标却被服务端拒绝：该文案含服务端原因（如距离），保留。
        self._log_result(aid, course, f"定位签到失败：{message or '无响应'}")
        return Outcome.SKIPPED

    def _log_signed_location(
        self,
        activity_id: str,
        course: Course,
        lon: float,
        lat: float,
        distance: float | None = None,
    ) -> None:
        """记录定位签到的成功结果。

        ``distance`` 跟随推送时为推送点距教室距离，反推解算时为拟合残差；
        缓存坐标无此值，则省略。
        """
        text = f"定位签到成功：经度 {lon:.6f}，纬度 {lat:.6f}"
        if distance is not None:
            text += f"，距离 {distance:.1f} 米"
        self._log_result(activity_id, course, text, force=True)

    @staticmethod
    def _is_signed(message: str | None) -> bool:
        """服务端文案是否表示签到成功。

        除「签到成功」外，「已签到 / 请勿重复签到」同样是有效回执：新旧活动
        并存或本轮之前已在别处签过时，服务端回的是这类文案。
        """
        if not message or "未成功" in message or "失败" in message:
            return False
        return any(
            marker in message
            for marker in ("签到成功", "已签到", "已经签到", "请勿重复签到")
        )

    def _report(self, activity_id: str, message: str | None, course: Course) -> Outcome:
        """输出签到结果并归一化：把五花八门的服务端文案统一成
        「签到成功 / 签到失败：原因」一行，借关键字着色（成功→绿、失败→红）。

        结果只记一次——失败不进 ``_handled`` 会每轮重试。
        """
        if self._is_signed(message):
            self._log_result(activity_id, course, "签到成功", force=True)
            return Outcome.SIGNED
        self._log_result(activity_id, course, f"签到失败：{message or '无响应'}")
        return Outcome.SKIPPED

    def _mark(self, course: Course, activity_id: str, reason: str) -> None:
        """记录活动为已处理并打印原因，避免每轮重复提示。"""
        self._handled.add(activity_id)
        self._log_course(course, f"活动 {activity_id} {reason}，跳过")
