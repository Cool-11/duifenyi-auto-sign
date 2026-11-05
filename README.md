# 对分易课堂助手（duifenyi desktop）

一个 Windows 桌面小工具，把对分易（duifenyi / duifene）的课程与考勤活动集中到一个
窗口里，并支持对**本人账号**下的签到活动做提交。用 Python + pywebview 写成，界面
是本地页面，只有你打开程序时才会连对分易服务器。

![version](https://img.shields.io/badge/version-1.6.2-orange)
![platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![License](https://img.shields.io/badge/License-MIT-yellow)

下载：<https://github.com/Cool-11/duifenyi-auto-sign/releases/latest>

---

## 能做什么

| 功能 | 说明 |
|---|---|
| 课程列表 | 登录后读取本人账号下的课程与教学班 |
| 考勤活动 | 按 3–5 秒的间隔查看当前课程有无进行中的考勤 |
| 四类签到提交 | 签到码、二维码、定位、汉字——要素取自服务端下发，不需要手输 |
| 记录 | 每个动作都写进界面与 `logs\run.log`，事后可查 |

## 快速开始

1. 下载 Release 里的 zip，解压到任意目录，双击 `对分易自动签到.exe`
2. 用**你自己的**对分易账号密码登录（第一次打开需要自己填，程序不预置任何账号）
3. 选课程 → 点「开始监听」→ 日志里会写清每一步结果

> 需要 Windows 10 1803+ / 11，正常情况下系统自带 WebView2；缺失时仓库根目录有安装包。

## 隐私与数据

- 登录令牌与账号信息只保存在**程序同目录**的 `duifenyi.ini`，不上传任何第三方；
- 密码用 Windows DPAPI 按当前用户加密，换机器无法解密；
- 发布包**不含**任何配置文件、日志或他人账号，首次运行一定是空的登录界面；
- 不想留痕就点「退出登录」，会同时清掉令牌与保存的密码。

## 几点说明

- 一个实例只盯一门课；多门课请复制一份目录、各开一个实例；
- 电脑睡眠期间线程会停，建议把电源计划里的睡眠关掉；
- 签到提交失败时日志会带服务端原文（如「签到码错误」「签到已结束」），照原文判断即可；
- 出问题先看 `logs\run.log`，也可以用 `python diagnose_sign.py` 逐环看接口返回。

## 使用前提

- 只用于管理**本人**账号的课程与考勤，不提供多账号、不替他人签到；
- 是否使用、如何使用由你自行判断，请遵守所在学校的课堂考勤规定与平台服务条款；
- 本项目仅作 Python 网络编程与桌面客户端开发的练习，不承诺任何考勤结果。

## 开发

```bash
pip install -r requirements.txt      # requests + pywebview
python main.py                       # 源码运行
python -m unittest discover -s tests # 87 项离线测试，不联网
python build_release.py --build      # 构建并生成「干净」发布包
```

`build_release.py` 会剔除 `duifenyi.ini` 与 `logs/` 并校验，避免把本地登录状态
打进发布包。

## 协议

[MIT](LICENSE)
