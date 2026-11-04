"""App 接口诊断脚本：逐环打印原始响应，定位「为什么签不上」。

用法：
  python diagnose_sign.py 20210001 你的密码
  python diagnose_sign.py            # 交互式输入账号密码

脚本只读不写：不会提交任何签到，只把登录、课程、当前签到、人数、教室坐标
四环的原始返回打出来。平台再改版时，跑一遍就知道断在哪一环。
"""

from __future__ import annotations

import json
import sys

import requests
import urllib3

from duifene.api import DuifeneApi
from duifene.errors import AppApiError, AuthError
from duifene.rate_limit import RateLimiter
from duifene.settings import Settings


def head(title: str) -> None:
    """打印小节标题。"""
    print(f"\n=== {title} ===")


def brief(value: object, limit: int = 500) -> str:
    """压缩成单行短文本。"""
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return " ".join(str(text).split())[:limit]


def main() -> int:
    if len(sys.argv) >= 3:
        username, password = sys.argv[1], sys.argv[2]
    else:
        username = input("对分易账号：").strip()
        password = input("密码：").strip()
    if not username or not password:
        print("账号密码不能为空")
        return 2

    urllib3.disable_warnings()
    settings = Settings()
    session = requests.Session()
    session.headers["User-Agent"] = settings.user_agent
    session.trust_env = False
    limiter = RateLimiter(settings)
    # 诊断不落盘：传 None 作为 store，避免覆盖正式登录会话
    api = DuifeneApi(session, settings, limiter, None)

    head("[1] 登录")
    ok, message = api.login(username, password)
    print(f"    login() -> {ok}  message={message!r}")
    if not ok:
        print("    登录失败，后续步骤无法继续。常见原因：密码错误 / 账号未激活")
        return 1

    head("[2] 当前用户")
    identity = api.identity
    print(
        f"    账号={identity.account} userId={identity.user_id} "
        f"userTypeID={identity.user_type_id}（2=学生 1=教师）"
    )

    head("[3] 课程列表")
    try:
        courses = api.fetch_courses()
    except (AppApiError, AuthError) as exc:
        print(f"    ✗ 读取失败：{exc!s}")
        return 1
    print(f"    共 {len(courses)} 条（含教学班拆分）")
    for course in courses:
        print(f"    - courseID={course.course_id} classID={course.class_id} {course.name}")
    if not courses:
        return 1

    head("[4] 当前进行中的签到")
    target = courses[0]
    print(f"    以第一门课为例：{target.name}")
    try:
        activity = api.fetch_active_activity(target.course_id)
    except (AppApiError, AuthError) as exc:
        print(f"    ✗ 读取失败：{exc!s}")
        return 1
    if activity is None:
        print("    当前没有进行中的签到（这一步正常返回「无」也算通过）")
        return 0

    print(f"    ID={activity.id} 类型={activity.type} (1签到码 2二维码 3定位 4汉字)")
    print(
        f"    签到码={activity.code!r} 二维码令牌={activity.qr_token!r} "
        f"roomID={activity.room_id!r}"
    )
    print(
        f"    创建={activity.create_date} 截止={activity.valid_date} "
        f"已出勤={activity.attended}"
    )

    head("[5] 签到人数")
    try:
        signed, total = api.fetch_headcount(activity.id)
        print(f"    已参与 {signed}/{total}")
    except (AppApiError, AuthError) as exc:
        print(f"    ✗ 读取失败：{exc!s}（程序会自动跳过人数门槛，不影响签到）")

    head("[6] 教室坐标")
    try:
        coordinates = api.fetch_room_coordinates(activity)
    except (AppApiError, AuthError) as exc:
        coordinates = None
        print(f"    ✗ 读取失败：{exc!s}")
    if coordinates is None:
        print("    未取到教室坐标：定位签到会持续重试（签到未绑定教室时属正常）")
    else:
        print(f"    经度={coordinates[0]} 纬度={coordinates[1]}")

    head("[7] 结论")
    if activity.type == "1" and len(activity.code) != 4:
        print("    ✗ 签到码未下发：程序持续重试，也可在界面「手动签到」填 4 位码")
    elif activity.type == "2" and not activity.qr_token:
        print("    ✗ 二维码令牌未下发：程序持续重试；一直为空说明签到已结束")
    elif activity.type == "3" and coordinates is None:
        print("    ✗ 教室坐标缺失：定位签到无法提交")
    elif activity.type == "4" and len(activity.code) != 4:
        print("    ✗ 汉字答案未下发：程序持续重试，也可在界面「手动签到」填 4 个汉字")
    else:
        print("    ✓ 该活动的签到要素齐全，程序可直接提交")
    return 0


if __name__ == "__main__":
    sys.exit(main())
