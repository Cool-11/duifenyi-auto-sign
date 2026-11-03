# 更新日志

本项目的所有重要变更都记录在此文件中，格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循语义化版本（`主版本.次版本.修订号`）。

**变更类型**：`新增` / `修复` / `变更` / `优化` / `移除` / `文档` / `测试` / `构建`

---

## [1.5.1] - 2026-11-04

定位签到的限流死结：一次「过于频繁」= 整场活动静默。

### 修复

- **修复** 限流静默默认 `300` 秒 → **`10` 秒**：服务端「过于频繁」的冷却窗口只有几秒，
  300 秒静默会让 30–60 秒的签到活动整场错过（实机日志 01:24:57 起连续「限流中」）
- **修复** 反推探测半径 `800km` → **`5km`**：距离值过于异常，容易被判风控
- **修复** 反推被限流打断后从头重采：现在保留已采样本，冷却结束**从断点续采**
- **修复** 探测点 `6` → `4`、间隔 `4s` → `3s`，一次反推约 10 秒内完成
- **修复** 推送等待上限默认 `15` → `10` 秒，给反推留出时间窗

### 变更

- **变更** 版本号 `1.5.0` → `1.5.1`

### 验证

- `python -m unittest discover -s tests -v` → **51 passed**（新增「限流后断点续采」用例）

---

## [1.5.0] - 2026-11-04

补齐签到码与汉字的可用来源：平台不再下发给学生，改为「人工提供 + 程序提交」。

### 新增

- **新增** 界面「手动签到」输入框（状态栏，4 位签到码或 4 个汉字，回车可提交）
- **新增** `Bridge.submit_manual_code()`：登记后 5 分钟内有效，命中签到码/汉字活动即取用
- **新增** `ActivityProcessor` 支持注入手动码来源，取码顺序变为「列表字段 → 手动登记 →
  考勤入口页」，只消费一次，不会串到下一个活动
- **新增** 手动码被使用时在日志标明「活动 X 提交签到码 8888」，可追溯

### 变更

- **变更** 版本号 `1.4.2` → `1.5.0`
- **变更** README 新增 5.1「手动提交签到码 / 汉字」，并把两类失败排查指向该入口

### 说明

- 自动抓取这两类已确认不可行：实测活动**进行中**时，列表 17 个字段无 `CheckInCode`，
  教师页 `HFCheckCodeKey` 对学生为空，入口页也取不到——没有数据源，代码无法凭空生成
- 若后续愿意用账号密码走 App 接口（`/CK/GetModelByInProgress` 含 `checkInCode`），
  可再把这层手动逻辑替换为自动

### 验证

- `python -m unittest discover -s tests -v` → **50 passed**（新增手动码使用与一次性消费用例）

---

## [1.4.2] - 2026-11-04

针对 v1.4.1 实机日志暴露的问题修复（日志见 00:39–00:43 那一轮）。

### 修复

- **修复** 二维码令牌未做 URL 编码：令牌是 base64 形态，含 `+` / `/` / `=` 时会被服务端
  截断，直接判「参数错误」——这是「取到图也签不上」的主因
- **修复** 二维码失败只报「参数错误,请重新尝试」：现在区分「取图失败（含服务端原文）」
  「响应无图」「图片解码失败」「内容无令牌」四种，分别对应不同处置
- **修复** 每轮都重复提交「活动 ID 直签」：该形态服务端已不认，改为仅首轮尝试，
  省下的请求额度用于真正的令牌重投
- **修复** 定位反推连发 6 次签到请求触发「过于频繁」，导致整场 300 秒静默：
  新增 `probe_interval_s`（默认 4 秒）节流，且静默期内不再探测
- **修复** 定位反推被限流打断时判为 `SKIPPED`（永不重试）：改为 `DEFERRED`，
  静默结束后继续
- **修复** 定位失败日志缺活动 ID，多条活动并发时无法归属
- **修复** 签到码/汉字最终放弃时的文案：补一句原因（服务端未下发 / 学生端无法取得）

### 变更

- **变更** 版本号 `1.4.1` → `1.4.2`

### 验证

- `python -m unittest discover -s tests -v` → **48 passed**（新增令牌编码、二维码错误原因、
  反推节流三组用例）

---

## [1.4.1] - 2026-11-04

仅版本标识与重新打包，功能同 1.4.0（含其全部修复）。

### 变更

- **变更** 版本号 `1.4` → `1.4.1`：旧包与新包版本号相同导致「无法判断运行的是哪份构建」，
  现已可从窗口标题直接区分
- **变更** 重新打包 onedir 产物（`dist\对分易自动签到\`），并保留 exe 同目录的
  `duifenyi.ini`（登录态不丢）

---

## [1.4.0] - 2026-11-03

工程化补齐：把上一轮手工排查固化为脚本与测试，并给取码/取答案补上收口。

### 任务列表

- [x] **T1｜测试**：新增 `tests/` 离线单元测试，覆盖定位表单构造、二维码 base64/state 解析、
  签到回执判定、签到码/汉字取码兜底、日志静音与 `force` 语义
  —— **已完成**：`tests/test_api.py`（14 例）、`tests/test_processor.py`（12 例）、
  `tests/test_settings.py`（7 例，前端表单/持久化字段/区间三方一致性），
  `python -m unittest discover -s tests` 33 例全绿
- [x] **T2｜新增**：新增 `diagnose_sign.py`，把四类签到的链路排查固化为可复用脚本
  （原始响应打印、字段存在性核对、旧表单对照）
  —— **已完成**：八步诊断；`--reuse` 复用 Cookie、`--compare-location` 做新旧表单对照
- [x] **T3｜优化**：签到码与汉字答案的等待加轮次上限（`answer_wait_rounds`），
  避免整场活动无限重试刷屏
  —— **已完成**：新增 `ActivityProcessor._wait_answer()`，超限后判失败并给出确定结论
- [x] **T4｜优化**：二维码依赖可见化——启动与失败日志区分「缺 Pillow/pyzbar」与「未取到令牌」
  —— **已完成**：`bridge.bootstrap()` 启动即提示；`_sign_qr` 的失败文案按依赖状态分流
- [x] **T5｜变更**：设置面板暴露 `qr_retry_interval_s`、`qr_max_rounds`、`answer_wait_rounds`
  —— **已完成**：`PERSISTABLE_FIELDS` / `SETTING_LIMITS` / 前端「二维码与取码」分组 /
  README 参数表同步
- [x] **T6｜测试**：四类签到全链路仿真（活动 → 门禁 → 分发 → 提交 → 回执 → 去重）
  —— **已完成**：`tests/test_pipeline.py`（8 例）；四类各跑通一遍 `process()`，
  并覆盖“已出勤跳过”“签到成功后不重复”“定位无坐标跳过”“未知类型跳过”
- [ ] **外部依赖（非代码任务）**：真机验收——需有效登录凭证与进行中的活动，
  本机 Cookie 已过期且无活动，故未执行；用 `diagnose_sign.py` 一条命令即可补测

### 新增

- **新增** `tests/test_api.py`：定位表单六件套、二维码 base64 与 `state` 解析（含
  `G.aspx` 302 分支）、签到码三级取码、汉字答案回退、登录探测容错
- **新增** `tests/test_processor.py`：回执判定矩阵、日志静音与 `force`、签到码/汉字轮次收口、
  二维码「直签 → 令牌重投 → 轮次用尽」
- **新增** `tests/test_settings.py`：`web/app.js` 表单 key、`PERSISTABLE_FIELDS`、
  `SETTING_LIMITS` 三方一致性 + 新参数的校验与落盘
- **新增** `tests/test_pipeline.py`：以假传输层跑完四类签到的完整调度链路，
  断言提交参数（`checkincode` / `state` 令牌 / 经纬度）与去重行为
- **新增** `diagnose_sign.py`：登录态 → 课程 → 活动字段 ✓/✗ 核对 → 人数 → 签到码探针 →
  二维码直签与令牌 → 汉字/取码来源 → 定位远场测距，共八节
- **新增** `ActivityProcessor._wait_answer()`：取不到码/答案时的统一收口逻辑

### 修复

- **修复** `_extract_base64_image()`：服务端返回 `{"data":"iVBOR..."}` 且把 base64 中的
  `/` 转义成 `\/`，旧正则会被切断导致「有图却解不出令牌」；改为先按 JSON 取 `data`，
  回退时先还原转义再匹配（实测 23:55 的二维码活动已能拿到图，此前解析结果为 None）

### 优化

- **优化** `duifene/processor.py`：签到码与汉字的等待改为「先重试、满轮次再放弃」，
  日志只在首轮与收口时各打一条
- **优化** `duifene/bridge.py`：启动时检测二维码解码依赖，缺失则直接记一条 error 级日志
- **优化** 二维码失败文案：缺依赖时提示 `pip install -r requirements.txt`，
  而不是笼统的「无响应」

### 变更

- **变更** `duifene/settings.py`：`qr_retry_interval_s`、`qr_max_rounds` 进入可持久化字段，
  新增 `answer_wait_rounds`（默认 12）
- **变更** 监听时间窗默认值 `6–23` → **`0–24`（全天轮询）**；夜间实测时不再被静默拦住，
  需要限制时段可在设置里改回
- **变更** `web/app.js`：设置面板新增「二维码与取码」分组（3 项）
- **变更** 版本号 `1.3` → `1.4`
- **变更** README：参数表补 3 行、排查章节按新收口逻辑改写、调试脚本拆为三小节

### 构建

- **构建** `build.spec`：改用 `collect_dynamic_libs("pyzbar")` 收集 `libzbar-64.dll` 与
  `libiconv.dll`，修复「源码能解码、打包后二维码不可用」的隐性缺陷
  （仅加 hiddenimports 只能保证模块进 PYZ，动态库仍需显式收集）
- **构建** 重新产出 onedir 包 `dist\对分易自动签到\`（约 73.2 MB / 290 个文件）

### 验证

- `python -m unittest discover -s tests -v` → **41 passed**
- 打包产物冒烟：`对分易自动签到.exe` 启动后持续存活、stderr 为空
- 冻结环境自检：单独构建的校验程序输出 `available=True`，证明打包后 Pillow/pyzbar
  及其 DLL 可正常加载
- `python -m compileall duifene main.py diagnose_sign.py diagnose_location.py tests` → 通过
- `python diagnose_sign.py --help` 正常输出用法；`--reuse` 在 Cookie 失效时给出明确引导
- 启动冒烟：`python main.py` 进程持续存活、stderr 为空（GUI 正常拉起，未登录故未签到）
- 编辑器静态检查（basedpyright）对本次改动零报错（仅 `config.py` 存在既有告警）

---

## [1.3.0] - 2026-11-03

适配对分易 2026-10 下旬平台改版，修复四类签到的断链环节。

### 新增

- **新增** `duifene/qrdecode.py`：二维码解码模块（Pillow + pyzbar），把教师端实时二维码
  还原为 `state` 令牌；依赖缺失时抛 `QrDecodeUnavailable`，不静默失败
- **新增** `DuifeneApi.fetch_qr_state()`：`action=getcodeimage&cid=` 取图 → base64 提取 →
  解码 → 解析 `state`；兼容 `G.aspx?OnlyId=` 短链形态（跟随一次 302 取 `Location`）
- **新增** `DuifeneApi.prime_checkin_entry()`：按官方客户端路径预访 `Module.aspx?data=` 与
  考勤入口页，建立会话上下文（每教学班一次）
- **新增** `DuifeneApi.fetch_code_from_entry()`：入口页隐藏域／内联脚本兜底取 4 位签到码
- **新增** `DuifeneApi._location_payload()`：定位签到与距离探测共用的表单构造器
- **新增** `Settings.qr_retry_interval_s` / `qr_max_rounds`：二维码重试节拍与轮次上限
- **新增** 设置项 `sign_delay_s`（检测后延迟秒数）、`min_headcount`（最低已签人数）
- **新增** `config.resolve_ini_path()`：`duifenyi.ini` 锚定到程序目录，避免受工作目录影响
- **新增** `diagnose_location.py`：定位链路诊断脚本（远场测距 + 原始响应打印）
- **新增** `LICENSE`（MIT）、`.gitignore` 排除 `diagnose_cookies.json` 等本地凭证

### 修复

- **修复** 定位签到缺 `cid` / `tcid`：补齐 `action/cid/tcid/sid/经纬度` 六件套与
  `X-Requested-With`，修复「拿到正确坐标仍签不上」
- **修复** 二维码签到：服务端不再接受活动 ID 直签，改为直签失败后解码实时滚动令牌重投；
  删除把「参数错误」误报成「非微信链接登录」的错误映射
- **修复** 签到码/汉字取不到 `CheckInCode` 时静默跳过：改为入口页兜底，仍取不到则明确报错
- **修复** 汉字签到取不到答案时无限静默 `DEFERRED`：改为提示一次并持续重试
- **修复** `check_login()` 用 GET 携带表单体（可能被网关丢弃）：改为 POST，并容错空响应
- **修复** 人数接口异常时无日志地无限等待：补一条「人数获取失败，暂不签到」
- **修复** 「先失败、后重试成功」的成功回执被日志静音吞掉：结果日志支持 `force`
- **修复** 签到回执识别过窄：`已签到` / `已经签到` / `请勿重复签到` 也判定为成功
- **修复** 二维码提交未纳入限流状态机：补 `raise_if_blocking` 与限流文案判定

### 变更

- **变更** `DuifeneApi.sign_by_location()` 签名改为 `(course, longitude, latitude)`
- **变更** `DuifeneApi._sign_post()` 统一附带 `X-Requested-With: XMLHttpRequest`
- **变更** 版本号 `1.2` → `1.3`
- **变更** 仓库迁移至 `Cool-11/duifenyi-remote-sign`，原仓库保留为 `upstream`

### 构建

- **构建** `requirements.txt` 增加 `Pillow>=10.0.0`、`pyzbar>=0.1.9`
- **构建** `build.spec` 为 `pyzbar` / `PIL` 补充 hiddenimports

### 文档

- **文档** 重写 `README.md`：修复对照表、两种登录方式、四类签到工作原理、参数表、
  常见问题排查、调试脚本、安全隐私与免责声明

---

## [1.2.0] - 2026-10-21

### 新增

- **新增** WebView2 运行时检测（注册表四个安装通道 + 版本校验），缺失时弹窗引导安装
- **新增** 启动期原生加载遮罩（`before_show` 叠加 Label，`loaded` 后移除，15 秒看门狗兜底）

### 变更

- **变更** 窗口改为无边框模式，仅顶栏 `.pywebview-drag-region` 可拖动
- **变更** 版本号 `1.1` → `1.2`

---

## [1.1.0] - 2026-10-12

### 新增

- **新增** 汉字签到支持（`type=4`）：读取 `HFCheckCodeKey` 取正确顺序后按序提交
- **新增** `DuifeneApi.fetch_hanzi_answer()`：从教师端考勤页解析答案

### 文档

- **文档** README 补充汉字签到说明与四类签到策略表

---

## [1.0.0] - 2026-09-16

首个可用版本：网页接口链路 + pywebview 桌面界面。

### 新增

- **新增** 两种登录方式：微信 OAuth 链接免密登录、账号密码登录，Cookie 持久化到 `duifenyi.ini`
- **新增** 课程列表拉取与教学班选择（`/_UserCenter/CourseInfo.ashx`）
- **新增** 当日签到活动轮询（`/_CheckIn/MBCount.ashx`）与签到类型分发
- **新增** 签到码签到（`/_CheckIn/CheckIn.ashx`）
- **新增** 二维码签到（`/_CheckIn/MB/QrCodeCheckOK.aspx`）
- **新增** 定位签到三路坐标来源：messageHub WebSocket 推送跟随 → 班级坐标缓存 → 三边测量反推
- **新增** 距离预言机（`CheckInRoomHandler.ashx` 的「距离：x 米」回执）+ 多方位探测解算教室坐标
- **新增** 签到门禁：监听时间窗、小班快速通道、大班已签占比、旧活动上限、限流静默
- **新增** 登录态常驻哨兵（`LoginSentinel`）与登录失败回滚，失败不写盘
- **新增** pywebview 单页界面：登录面板、课程选择、运行状态、彩色日志（`web/`）
- **新增** PyInstaller onedir 打包配置（`build.spec`）与 WebView2 安装包
