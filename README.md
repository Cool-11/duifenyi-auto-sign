# 🎓 对分易远程签到

> 对分易（duifene）课堂考勤自动签到工具。登录一次后持续监听考勤活动，自动完成
> **签到码 / 二维码 / 定位 / 汉字** 四类签到，支持微信链接免密登录与账号密码登录。

![Python](https://img.shields.io/badge/Python-3.12+-blue?logo=python)
![pywebview](https://img.shields.io/badge/pywebview-%3E%3D5.0-green?logo=web)
![License](https://img.shields.io/badge/License-MIT-yellow)

仓库地址：<https://github.com/Cool-11/duifenyi-remote-sign>

![界面预览](assets/gui.png)

---

## 一、这个版本修了什么

对分易在 2026 年 10 月下旬改版后，旧版依赖的网页链路大面积失效（见上游 issue #14、
PR #16 / #17）。**v1.3 针对四类签到各修了一处**：

| 类型 | 旧版失败原因 | v1.3 的处理 |
|---|---|---|
| 签到码 | 列表接口不再下发 `CheckInCode`，取不到码直接跳过 | 列表取不到时回退到考勤入口页隐藏域／内联脚本取值；仍取不到则**明确报错**并说明原因 |
| 二维码 | 服务端不再接受「活动 ID 当 state」，恒返回 `参数错误`，且被误报成"非微信登录" | 先按官方客户端路径预访入口，直签失败后拉取教师端实时二维码、本地解码出滚动令牌并重投；按间隔重试、有轮次上限，失败文案原样呈现 |
| 定位 | 提交表单缺 `cid` / `tcid`，服务端无从判定教室，坐标正确也签不上 | 与距离探测共用同一表单构造器，补齐 `cid/tcid/sid/经纬度` 与 AJAX 头 |
| 汉字 | 答案同源于已消失的 `CheckInCode`，兜底走教师端页面拿不到，且失败被静默吞掉 | 三级取答案（列表 → 教师端 → 入口页），取不到时**提示一次**并持续重试，不再静默空转 |

另外修了几处会掩盖故障的地方：登录态探测改用 POST 并容错空响应；人数接口失败不再
静默等待；「失败后重试成功」的成功回执不再被日志静音吞掉；`已签到/请勿重复签到`
也被正确识别为签到成功。

**v1.4** 在其上补齐工程化能力：`diagnose_sign.py` 全链路诊断脚本、离线单元测试、
签到码/汉字取不到时的轮次收口、二维码依赖缺失的显式提示，以及三个可调参数
（`qr_retry_interval_s` / `qr_max_rounds` / `answer_wait_rounds`）。

---

## 二、环境要求

- **Windows 10（1803+）/ Windows 11**
- 界面基于 WebView2 渲染。系统通常自带；若启动提示缺少运行时，运行仓库根目录的
  `WebView2Setup.exe` 安装即可
- **Python 3.12+**（仅源码运行需要）
- 网络可访问 `www.duifene.com`（定位签到的 WebSocket 还会用到 `wsdf.duifene.com`）

`pyzbar` 在 Windows 的 wheel 自带 `libzbar`；Linux/macOS 需先装系统库
（Debian/Ubuntu：`apt install libzbar0`）。

---

## 三、安装与启动

```bash
# 1. 获取源码
git clone git@github.com:Cool-11/duifenyi-remote-sign.git
cd duifenyi-remote-sign

# 2. 安装依赖（建议用虚拟环境）
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 3. 启动
python main.py
```

首次启动会在程序同目录生成 `duifenyi.ini`（保存登录 Cookie、运行参数与教室坐标缓存）。
**该文件含登录凭证，请勿分享或提交到仓库**（已在 `.gitignore` 中排除）。

---

## 四、登录

左侧登录面板提供两种方式，二者可随时切换，登录成功后课程列表自动加载并默认选中第一门。

### 方式一：微信链接（免密，推荐；二维码签到必须用它）

1. 复制界面上的微信授权链接，在**电脑版微信**里发到「文件传输助手」并点开。
2. 在微信内置浏览器里点右上角「…」→「复制链接」。
3. 把复制的完整链接粘贴到「登录链接」输入框，点击**登录**。

> 链接里的 `code` 是 32 位、**一次性且几分钟内过期**。若提示「链接有误」说明没复制到
> `code=`；若提示「登录失败，未取得登录凭证」说明 code 已过期，重新复制一次即可。

### 方式二：账号密码

直接填写对分易账号（手机号/学号）与密码登录。

> 账号密码登录拿不到微信授权态，因此**无法完成二维码签到**；其余三类不受影响。
> 需要二维码签到时请改用方式一。

登录成功后 Cookie 会写入 `duifenyi.ini`，下次启动自动复用；后台每 30 秒探测一次
登录态，失效时弹窗提示并自动停止监听。

---

## 五、使用步骤

1. 登录 → 右栏「监控课程」下拉选择要监听的课程（按教学班区分）。
2. 点击**开始监听**。程序按 3–5 秒随机间隔轮询当日考勤活动。
3. 检测到新活动后自动判定并签到，日志区显示每一步结果（成功为绿色、失败为红色）。
4. 需要停止时点**停止监听**；关闭窗口会先停线程与 WebSocket。

---

## 六、四类签到的工作方式

| 类型 | 触发与提交 |
|---|---|
| 签到码 `type=1` | 取 4 位签到码 → `POST /_CheckIn/CheckIn.ashx`（`action=studentcheckin&studentid&checkincode`） |
| 二维码 `type=2` | 先直签；被拒后拉教师端实时二维码 → 本地解码 `state` 令牌 → 立即重投。令牌十几秒轮换，故每轮现取现用，默认 6 秒一次、最多 8 轮 |
| 定位 `type=3` | 坐标优先级：**WebSocket 推送跟随** → 班级缓存 → 三边测量反推。反推用「距离预言机」：远场多方位探测拿到到教室的距离，再最小二乘解算坐标 |
| 汉字 `type=4` | 取 4 个汉字的正确顺序 → 与签到码同一接口提交 |

签到前的门禁（可在设置里调整）：

- 时间窗：默认 **0–24（全天轮询）**；如需限制（例如只在白天工作）可在设置里把起始/结束小时改成 6 / 23，窗外不轮询；
- 新活动延迟：`sign_delay_s` 秒后才签，用于规避「秒签」风控；
- 人数门禁：已签人数未达 `min_headcount`，或大班占比未达 `headcount_ratio` 时继续等待；
- 小班快速通道：总人数小于 `small_class_max` 时，创建满 `small_class_delay_s` 秒即签；
- 旧活动上限：创建超过 `stale_sign_minutes` 分钟的活动不再处理。

---

## 七、参数说明

| 参数 | 默认 | 作用 |
|---|---|---|
| `poll_min_s` / `poll_max_s` | 3 / 5 | 轮询间隔随机区间（秒） |
| `login_check_interval_s` | 30 | 登录态探测间隔（秒） |
| `error_backoff_max_s` | 60 | 连续异常时的退避上限（秒） |
| `monitor_start_hour` / `monitor_end_hour` | 0 / 24 | 监听时间窗（左闭右开），0–24 为全天 |
| `small_class_max` | 5 | 小班人数阈值 |
| `small_class_delay_s` | 5 | 小班活动创建后等待秒数 |
| `headcount_ratio` | 0.2 | 大班已签占比门槛 |
| `sign_delay_s` | 0 | 检测到签到后延迟多少秒再签（0=立即） |
| `min_headcount` | 0 | 最低已签人数门槛（0=不启用） |
| `stale_sign_minutes` | 30 | 创建超过多少分钟的旧活动不再处理 |
| `ws_follow_distance_m` | 50 | 只跟随距教室小于该值的推送坐标（米） |
| `ws_wait_timeout_s` | 15 | 等待他人推送坐标的时长（秒） |
| `qr_retry_interval_s` | 6 | 二维码重试间隔（秒），令牌滚动，太短无意义 |
| `qr_max_rounds` | 8 | 二维码重试轮次上限，超过即判定活动结束 |
| `answer_wait_rounds` | 12 | 签到码/汉字取不到时的重试轮次上限 |
| `cooldown_seconds` | 300 | 命中服务端限流后的静默时长（秒） |
| `proxy_url` | 空 | 抓包/代理地址，如 `http://127.0.0.1:8080`；填了会自动关闭 TLS 校验 |

---

## 八、常见问题排查

**一直没反应**：默认已是全天轮询（0–24），先看状态行的「最近轮询时间」是否在刷新；
若日志出现「当前不在监听时间窗」，说明设置里把时间窗改窄了。日志只记录真实事件，
空轮询不会刷日志。

**登录失效**：链接登录的 Cookie 有效期有限，重新复制微信链接登录即可，不必重装。

**签到码签到失败（日志：未取到有效签到码）**：说明服务端这次没把码下发给学生端
（平台改版后的常见情况）。程序会先逐轮重试 `answer_wait_rounds` 轮，仍取不到才判失败并
给出结论。这不是解析 bug——用 `diagnose_sign.py` 看 `MBCount.ashx`
（`action=getstudentinlogbyday`）返回的行里是否还有 `CheckInCode`。

**二维码签到失败**：
1. 必须用**微信链接登录**；账号密码登录一定失败；
2. 必须已安装 `Pillow` 与 `pyzbar`，否则取到图也解不出令牌——缺依赖时**启动日志就会提示**，
   失败日志也会明确写「本机缺少 Pillow/pyzbar」；
3. 日志会区分失败环节：`取图失败：<服务端原文>`（多为「签到已结束」）、`响应无图`、
   `图片解码失败`、`二维码内容无令牌`，以及令牌重投后服务端仍返回的文案；
4. 二维码令牌是滚动令牌，教师端关闭页面或活动结束后必然失败，日志会显示轮次用尽。

> 令牌按 base64 处理并做 URL 编码：早期版本直接拼进查询串，令牌含 `+` / `/` / `=` 时会被
> 服务端截断成「参数错误」，这是「明明拿到图却签不上」的常见原因。

**定位签到失败**：
1. 日志会写清是「未取得教室坐标」还是「服务端拒绝并给出原因」，并带上活动 ID；
2. 若无人签到、WebSocket 收不到推送，会退化为三边反推：每次探测都是一次真实签到请求，
   因此默认 **4 秒一次**（`probe_interval_s`）限速，静默期内不探测；反推被限流打断时
   会显示「定位反推被限流打断，N 秒后重试」，静默结束后自动继续；
3. 首次成功后会把该教学班的教室坐标写入 `duifenyi.ini` 的 `[LOC_<班级ID>]`，之后
   直接复用。

**汉字签到失败（日志：未取到有效汉字答案）**：答案来源受限，程序先逐轮重试
`answer_wait_rounds` 轮，仍取不到会给出确定结论而不是无限空转；把该行日志连同活动 ID
反馈，便于适配新的答案字段。

**人数获取失败 / 停在等待**：说明 `MBCount.ashx` 的人数接口没返回预期字段，日志会留痕；
此时可临时把 `min_headcount` 设为 0、`headcount_ratio` 设为 0 观察。

**要抓包看请求**：设置里填 `http://127.0.0.1:8080`（会自动关闭证书校验），抓完清空即可。

---

## 九、调试脚本

### 9.1 `diagnose_sign.py`（四类签到全链路诊断）

```bash
python diagnose_sign.py "<微信OAuth链接>"        # 登录并诊断
python diagnose_sign.py --reuse                  # 复用 diagnose_cookies.json
python diagnose_sign.py --reuse --compare-location   # 额外用旧表单做定位对照
```

八步逐环打印**原始响应**：登录态 → 课程/学生 ID → 当日活动字段（`CheckInType` /
`CheckInCode` / `CanApply` 是否还在，缺哪个直接标 `✗`）→ 人数接口 → 签到码探针（用无效码
`0000`，不会真签到）→ 二维码（直签回执 + 实时令牌是否解得出来）→ 汉字/取码三个来源 →
定位（远场测距，不会签到成功）。平台再改版时，跑一遍就知道断在哪一环。

### 9.2 `diagnose_location.py`（定位专项）

```bash
python diagnose_location.py "<微信OAuth链接>"   # 远场探测，只测距
python diagnose_location.py --reuse            # 复用 Cookie
```

只覆盖定位链路：打印 `CheckInRoomHandler.ashx` 的原始响应与距离正则命中情况。

### 9.3 离线单元测试

```bash
python -m unittest discover -s tests -v
```

不联网、不需要登录，覆盖定位表单构造、二维码解析与令牌提取、回执判定、取码兜底与重试收口，
以及**四类签到的全链路仿真**（活动 → 门禁 → 分发 → 提交 → 回执 → 去重）。

---

## 十、安全与隐私

- 登录 Cookie 以明文保存在程序目录的 `duifenyi.ini`，仅本机使用；仓库已忽略该文件。
- 本工具不向任何第三方服务器发送数据，所有请求都直达 `duifene.com`。
- 抓包代理与 TLS 校验开关只影响本机发起的请求，请勿长期关闭校验。
- 请勿把含 Cookie 的日志、`duifenyi.ini`、`diagnose_cookies.json` 贴到 issue 里。

---

## 十一、打包发布（Windows）

```bash
python -m venv build_env
build_env\Scripts\pip install -r requirements.txt pyinstaller
build_env\Scripts\pyinstaller build.spec --noconfirm
# 产物：dist\对分易自动签到\
```

`build.spec` 已把 `PIL` / `pyzbar` 加入 hiddenimports，并用 `collect_dynamic_libs("pyzbar")`
收集 `libzbar-64.dll` / `libiconv.dll`——**只加 hiddenimports 是不够的**：模块进 PYZ 了，但
解码用的动态库仍需显式收集，否则打包后二维码签到会提示「缺少 Pillow/pyzbar」。

产物结构（PyInstaller 6 onedir）：

```
dist\对分易自动签到\
├─ 对分易自动签到.exe
└─ _internal\
   ├─ web\            # 界面资源
   ├─ pyzbar\         # libzbar-64.dll / libiconv.dll
   └─ ...
```

**打包后自检**：在没装 Python 的机器上跑一次，确认日志里没有「缺少 Pillow/pyzbar」。

---

## 十二、免责声明

本项目仅供学习 Python 网络编程与自动化测试使用。使用本工具代签可能违反学校的考勤
规定与平台服务协议，由此产生的成绩、纪律或账号风险由使用者自行承担。请遵守课堂纪律，
尊重教师的教学安排。

## 十三、开源协议

[MIT](LICENSE)
