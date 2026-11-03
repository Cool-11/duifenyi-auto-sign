"""定位/二维码签到链路诊断脚本。

用微信 OAuth 链接登录后，对一个课程做一次远场探测签到请求，
打印 CheckInRoomHandler.ashx 的原始响应文本，用于判断官方接口是否变更。

用法：
  python diagnose_location.py "<微信OAuth链接>" [课程序号]
  python diagnose_location.py --reuse [课程序号]   # 复用已保存的 Cookie
  课程序号从 1 开始，默认 1；只做探测请求，不提交真实签到结果。
  登录成功后 Cookie 保存到 diagnose_cookies.json，后续可用 --reuse。
"""

import json
import re
import sys

import requests

from duifene.api import DuifeneApi
from duifene.models import Course
from duifene.rate_limit import RateLimiter
from duifene.settings import Settings

COOKIE_FILE = "diagnose_cookies.json"


def load_session() -> requests.Session:
    session = requests.Session()
    try:
        with open(COOKIE_FILE, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                session.cookies.set(k, v)
    except FileNotFoundError:
        pass
    return session


def save_session(session: requests.Session) -> None:
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(session.cookies.get_dict(), f)


def main() -> None:
    args = [a for a in sys.argv[1:]]
    reuse = "--reuse" in args
    args = [a for a in args if a != "--reuse"]
    if not args:
        print(__doc__)
        sys.exit(1)
    index = int(args[1]) - 1 if len(args) > 1 else 0

    settings = Settings()
    session = load_session()
    api = DuifeneApi(session, settings, RateLimiter(settings))

    print("[1] 检查登录态 ...")
    if reuse or api.check_login() is True:
        if api.check_login() is not True:
            print("    已保存的 Cookie 已失效，请用新的微信链接重新运行")
            sys.exit(2)
        print("    复用已保存 Cookie，登录有效")
    else:
        link = args[0]
        match = re.search(r"(?<=code=)\S{32}", link)
        if match is None:
            print("链接中未找到 32 位 code")
            sys.exit(1)
        print("    用新链接登录 ...")
        response = api.login_by_link(match.group(0))
        print(f"    登录 HTTP {response.status_code}")
        if api.check_login() is not True:
            print("    登录失败（code 可能已过期，请重新复制链接）")
            sys.exit(2)
        save_session(session)
        print(f"    登录成功，Cookie 已保存到 {COOKIE_FILE}")

    print("[2] 拉取课程列表 ...")
    raw_courses = api.fetch_courses()
    if not raw_courses:
        print("    课程列表为空，无法继续")
        sys.exit(3)
    courses = [
        Course(
            course_id=str(r["CourseID"]),
            class_id=str(r["TClassID"]),
            name=str(r["CourseName"]),
        )
        for r in raw_courses
    ]
    for i, c in enumerate(courses):
        print(f"    [{i + 1}] {c.name}  course_id={c.course_id} class_id={c.class_id}")
    course = courses[index]
    print(f"[3] 选择课程: {course.name}")

    user_id = api._require_user_id()
    print(f"    sid={user_id}")

    # 远场探测点（北京市中心，远离真实教室，正常应返回“距离: xxxxx米”）
    lon, lat = 116.4074, 39.9042
    print("[4] 探测请求 CheckInRoomHandler.ashx action=signin ...")
    response = api._sign_post(
        "/_CheckIn/CheckInRoomHandler.ashx",
        api._location_payload(course, lon, lat),
        timeout=15,
    )
    print(f"    HTTP {response.status_code}")
    print("    --- 原始响应 ---")
    print(response.text[:2000])
    print("    --- 解析测试 ---")
    match = re.search(r"距离[:：]\s*([0-9]+(?:\.[0-9]+)?)\s*米", response.text)
    print(f"    距离正则匹配: {match.group(1) if match else '未匹配到！'}")

    print("[5] 当日活动列表原始字段（看 CheckInCode/CheckInType 是否还在）...")
    acts = api.fetch_activities(course.class_id, user_id)
    if acts is None:
        print("    fetch_activities 返回 None（接口或登录态异常）")
    elif not acts:
        print("    今日暂无签到活动")
    else:
        for a in acts[:5]:
            print(
                f"    id={a.id} type={a.type} code={a.code!r} "
                f"can_apply={a.can_apply} status={a.status_name}"
            )


if __name__ == "__main__":
    main()
