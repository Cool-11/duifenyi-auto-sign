"""四类签到链路诊断脚本。

对分易改版频繁，签到失败时先跑这个脚本，把每一环的**原始响应**打出来，再判断是
「接口地址变了」「字段没了」还是「参数不对」，而不是靠猜。

用法：
  python diagnose_sign.py "<微信OAuth链接>"      # 用新链接登录后诊断
  python diagnose_sign.py --reuse               # 复用 diagnose_cookies.json
  python diagnose_sign.py --reuse --compare-location   # 额外用旧表单做定位对照

说明：
  * 只做诊断，不提交有效签到码；第 5 步用无效码 `0000` 探接口，不会真的签到。
  * 第 8 步用远场坐标（北京市中心）测距，正常应返回「不在教室范围，距离：x 米」，
    不会签到成功。
  * 登录成功后 Cookie 存到 diagnose_cookies.json，后续可 --reuse。
"""

import argparse
import json
import re
import sys

import requests

from duifene import qrdecode
from duifene.api import DuifeneApi
from duifene.models import Course
from duifene.rate_limit import RateLimiter
from duifene.settings import Settings

COOKIE_FILE = "diagnose_cookies.json"
# 远场探测点：北京市中心，离任何真实教室都足够远
FAR_LON, FAR_LAT = 116.4074, 39.9042


def load_session() -> requests.Session:
    """按真实客户端参数构造会话，并尽量复用已保存 Cookie。"""
    settings = Settings()
    session = requests.Session()
    session.headers["User-Agent"] = settings.user_agent
    session.trust_env = False
    try:
        with open(COOKIE_FILE, encoding="utf-8") as handle:
            for key, value in json.load(handle).items():
                session.cookies.set(key, value)
    except (FileNotFoundError, ValueError):
        pass
    return session


def save_session(session: requests.Session) -> None:
    """保存 Cookie，供下次 --reuse。"""
    with open(COOKIE_FILE, "w", encoding="utf-8") as handle:
        json.dump(session.cookies.get_dict(), handle)


def head(title: str) -> None:
    """打印小节标题。"""
    print(f"\n=== {title} ===")


def brief(text: str, limit: int = 600) -> str:
    """压缩响应文本，便于单行查看。"""
    return " ".join(str(text).split())[:limit]


def check_login(api: DuifeneApi, args) -> bool:
    """确认登录态；必要时用链接登录。"""
    head("[1] 登录态")
    state = api.check_login()
    print(f"    check_login() -> {state}  (True 已登录 / False 失效 / None 网络失败)")
    if state is True:
        return True
    if args.reuse:
        print("    已保存的 Cookie 不可用，请去掉 --reuse 并用新的微信链接登录")
        return False
    match = re.search(r"(?<=code=)\S{32}", args.link or "")
    if match is None:
        print("    链接里找不到 32 位 code")
        return False
    response = api.login_by_link(match.group(0))
    print(f"    登录请求 HTTP {response.status_code}")
    if api.check_login() is not True:
        print("    登录失败：code 可能已过期，重新复制链接再试")
        return False
    save_session(api._session)
    print(f"    登录成功，Cookie 已保存到 {COOKIE_FILE}")
    return True


def pick_course(api: DuifeneApi, index: int) -> Course | None:
    """拉课程并选中第 index 门。"""
    head("[2] 课程与用户")
    try:
        raw_courses = api.fetch_courses()
    except Exception as exc:  # noqa: BLE001 - 诊断脚本不中断
        print(f"    拉取课程异常：{exc}")
        return None
    if not raw_courses:
        print("    课程列表为空")
        return None
    courses = [
        Course(
            course_id=str(row["CourseID"]),
            class_id=str(row["TClassID"]),
            name=str(row["CourseName"]),
        )
        for row in raw_courses
    ]
    for position, course in enumerate(courses, start=1):
        mark = "*" if position == index else " "
        print(f"   {mark}[{position}] {course.name} course={course.course_id} "
              f"class={course.class_id}")
    user_id = api.fetch_user_id()
    print(f"    学生 ID(hidUID) -> {user_id}")
    if index < 1 or index > len(courses):
        print(f"    课程序号 {index} 越界")
        return None
    return courses[index - 1]


def dump_activities(api: DuifeneApi, course: Course, user_id: str) -> list:
    """打印当日活动原始字段，并核对关键字段是否还在。"""
    head("[3] 当日活动原始行（判断字段是否还在）")
    settings = api._settings
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": settings.host + "/_CheckIn/PC/StudentNoCheckCount.aspx",
    }
    try:
        response = api._session.post(
            settings.host + "/_CheckIn/MBCount.ashx",
            data={
                "action": "getstudentinlogbyday",
                "classid": course.class_id,
                "studentid": user_id,
            },
            headers=headers,
            timeout=settings.request_timeout_s,
        )
    except requests.exceptions.RequestException as exc:
        print(f"    请求异常：{exc}")
        return []
    print(f"    HTTP {response.status_code}  body={brief(response.text, 400)}")
    try:
        payload = response.json()
    except ValueError:
        print("    非 JSON 响应，接口可能已变更")
        return []
    rows = payload.get("rows") or []
    print(f"    msg={payload.get('msg')!r} rows={len(rows)}")
    if not rows:
        print("    今日暂无活动：请先让教师在平台发起一次签到再跑此脚本")
        return rows
    print(f"    行字段：{sorted(rows[0].keys())}")
    wanted = ("CheckInType", "CheckInCode", "CanApply", "CheckInStatus",
              "StatusName", "CreaterDate", "ApplyLimitDate")
    for row in rows[:5]:
        print("    ---")
        for key in wanted:
            present = key in row
            print(f"      {key:16s} {'✓' if present else '✗ 缺失'}  {row.get(key)!r}")
    return rows


def dump_headcount(api: DuifeneApi, rows: list) -> None:
    """打印人数接口原始响应与解析结果。"""
    head("[4] 人数接口（门禁依赖它，失败会暂不签到）")
    if not rows:
        print("    无活动，跳过")
        return
    ciid = str(rows[0].get("ID"))
    settings = api._settings
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": settings.host + "/_CheckIn/PC/StudentNoCheckCount.aspx",
    }
    try:
        response = api._session.post(
            settings.host + "/_CheckIn/MBCount.ashx",
            data={"action": "getcheckintotalbyciid", "ciid": ciid, "t": "cking"},
            headers=headers,
            timeout=settings.request_timeout_s,
        )
        print(f"    HTTP {response.status_code}  body={brief(response.text, 400)}")
    except requests.exceptions.RequestException as exc:
        print(f"    请求异常：{exc}")
    print(f"    fetch_headcount({ciid}) -> {api.fetch_headcount(ciid)}")


def probe_code(api: DuifeneApi, rows: list) -> None:
    """用无效签到码探接口：只验证端点与回执形态，不会真的签到。"""
    head("[5] 签到码接口探针（无效码 0000）")
    if not rows:
        print("    无活动，跳过")
        return
    try:
        message = api.sign_by_code("0000")
        print(f"    sign_by_code('0000') -> {message!r}")
        print("    说明：无效码返回「签到码错误」属正常；返回「禁止访问」说明登录态或"
              "接口鉴权变了；返回「参数错误」说明参数名变了")
    except Exception as exc:  # noqa: BLE001
        print(f"    提交异常：{type(exc).__name__}: {exc}")


def probe_qr(api: DuifeneApi, course: Course, rows: list) -> None:
    """打印二维码直签回执，并尝试取实时令牌。"""
    head("[6] 二维码链路")
    print(f"    Pillow/pyzbar 可用：{qrdecode.available()}")
    if not rows:
        print("    无活动，跳过")
        return
    activity_id = str(rows[0].get("ID"))
    api.prime_checkin_entry(course)
    try:
        text = api.sign_by_qr(activity_id)
        print(f"    直签 state={activity_id} -> {text!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"    直签异常：{type(exc).__name__}: {exc}")
    token = api.fetch_qr_state(course)
    if token is None:
        print("    fetch_qr_state() -> None（响应无图 / 未识别出 state / 缺依赖）")
    else:
        print(f"    fetch_qr_state() -> {token[:8]}…（长度 {len(token)}）")


def probe_hanzi(api: DuifeneApi, course: Course) -> None:
    """检查汉字答案与签到码的三个来源。"""
    head("[7] 汉字 / 取码来源")
    settings = api._settings
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": settings.host + "/_CheckIn/PC/TeachCheckInList.aspx",
    }
    try:
        response = api._session.post(
            settings.host + "/_CheckIn/PC/TeachCheckIn.aspx",
            data={"courseid": course.course_id, "moduleid": "7", "classtype": "1"},
            headers=headers,
            timeout=settings.request_timeout_s,
        )
        print(f"    教师端页 HTTP {response.status_code} len={len(response.text)}")
        for element_id in ("HFChecktype", "HFCheckCodeKey"):
            found = re.findall(rf'id="{element_id}"[^>]*', response.text)
            print(f"      {element_id}: {found[:1]}")
    except requests.exceptions.RequestException as exc:
        print(f"    教师端页异常：{exc}")
    print(f"    入口页取码 fetch_code_from_entry() -> {api.fetch_code_from_entry()!r}")
    print(f"    汉字答案 fetch_hanzi_answer()     -> {api.fetch_hanzi_answer(course)!r}")


def probe_location(api: DuifeneApi, course: Course, compare: bool) -> None:
    """官方表单远场测距；可选再用旧表单做对照。"""
    head("[8] 定位链路（远场探测，不会签到成功）")
    try:
        response = api._sign_post(
            "/_CheckIn/CheckInRoomHandler.ashx",
            api._location_payload(course, FAR_LON, FAR_LAT),
            timeout=15,
        )
        match = re.search(r"距离[:：]\s*([0-9]+(?:\.[0-9]+)?)\s*米", response.text)
        print(f"    官方表单 HTTP {response.status_code} -> {brief(response.text, 300)}")
        print(f"    距离正则匹配：{match.group(1) if match else '未匹配到'}")
    except Exception as exc:  # noqa: BLE001
        print(f"    官方表单异常：{type(exc).__name__}: {exc}")
        return
    if not compare:
        return
    legacy = (
        f"action=signin&sid={api.cached_user_id}"
        f"&longitude={FAR_LON}&latitude={FAR_LAT}"
    )
    print("    --- 旧表单（缺 cid/tcid）对照 ---")
    try:
        response = api._sign_post(
            "/_CheckIn/CheckInRoomHandler.ashx", legacy, timeout=15
        )
        print(f"    HTTP {response.status_code} -> {brief(response.text, 300)}")
        print("    若旧表单返回「参数错误」而官方表单返回距离，说明 cid/tcid 是必需的")
    except Exception as exc:  # noqa: BLE001
        print(f"    旧表单异常：{type(exc).__name__}: {exc}")


def main() -> None:
    """按顺序执行八步诊断，任何一步失败都不影响后续。"""
    parser = argparse.ArgumentParser(description="对分易四类签到链路诊断")
    parser.add_argument("link", nargs="?", default="", help="微信 OAuth 链接")
    parser.add_argument("--reuse", action="store_true", help="复用已保存的 Cookie")
    parser.add_argument("--course", type=int, default=1, help="课程序号，从 1 开始")
    parser.add_argument(
        "--compare-location",
        action="store_true",
        help="额外用旧表单（缺 cid/tcid）做定位对照",
    )
    args = parser.parse_args()

    settings = Settings()
    session = load_session()
    api = DuifeneApi(session, settings, RateLimiter(settings))

    if not check_login(api, args):
        sys.exit(2)
    course = pick_course(api, args.course)
    if course is None:
        sys.exit(3)
    user_id = api.cached_user_id or api.fetch_user_id() or ""
    rows = dump_activities(api, course, user_id)
    dump_headcount(api, rows)
    probe_code(api, rows)
    probe_qr(api, course, rows)
    probe_hanzi(api, course)
    probe_location(api, course, args.compare_location)
    print("\n诊断结束：把上面每一节的原始响应与「✓/✗」结果对照 README 的排查章节。")


if __name__ == "__main__":
    main()
