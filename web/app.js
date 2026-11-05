/* 对分易自动签到 — 前端逻辑
 * 与 Python 的交互只走 pywebview js_api；日志经轮询 drain_logs 拉取。 */

(function () {
  "use strict";

  var api = null;
  var logCursor = 0;
  var logTotal = 0;
  var autoScroll = true;
  var pollTimer = null;
  var clockTimer = null;
  // 运行态与课程可用性：二者共同决定课程下拉与「开始监听」的可用性，
  // 由 syncRunControls 统一推导，避免多处各写 disabled 而互相覆盖。
  var watching = false;
  var hasCourses = false;
  // 开始/停止请求在途：期间控件保持禁用，避免连点重复提交。
  var runBusy = false;

  var $ = function (id) {
    return document.getElementById(id);
  };

  // 设置字段定义：与后端 PERSISTABLE_FIELDS 对齐（标签与分组属表现层）。
  var SETTINGS_GROUPS = [
    {
      title: "轮询与退避",
      fields: [
        { key: "poll_min_s", label: "轮询最小间隔", hint: "秒" },
        { key: "poll_max_s", label: "轮询最大间隔", hint: "秒" },
        {
          key: "error_backoff_max_s",
          label: "异常退避上限",
          hint: "秒，连续出错时的最长等待",
        },
      ],
    },
    {
      title: "监听时间窗",
      fields: [
        { key: "monitor_start_hour", label: "起始小时", hint: "0–24，含" },
        { key: "monitor_end_hour", label: "结束小时", hint: "0–24，不含" },
      ],
    },
    {
      title: "签到门禁",
      fields: [
        {
          key: "headcount_ratio",
          label: "已签占比门槛",
          hint: "0=不限制（默认，检测到就签）；如 0.2 表示班里签够 20% 才跟签",
        },
        {
          key: "headcount_deadline_s",
          label: "门槛兜底时限",
          hint: "秒，等待超过该时长无论人数多少都直接签",
        },
        {
          key: "small_class_max",
          label: "小班人数阈值",
          hint: "总人数低于此值不等占比，直接签",
        },
        { key: "small_class_delay_s", label: "小班等待时长", hint: "秒" },
        {
          key: "sign_delay_s",
          label: "检测后延迟秒数",
          hint: "检测到签到后等待的秒数，0 为立即签",
        },
        {
          key: "stale_sign_minutes",
          label: "旧活动上限",
          hint: "分钟，创建超过则跳过",
        },
      ],
    },
    {
      title: "限流与网络",
      fields: [
        { key: "cooldown_seconds", label: "限流静默时长", hint: "秒" },
        {
          key: "proxy_url",
          label: "代理地址",
          hint: "留空=直连（系统代理对本程序无效）；填写后会自动关闭证书校验",
        },
      ],
    },
  ];

  var settingsValues = {};
  var settingsDefaults = {};

  function fmtTime(ts) {
    var d = new Date(ts * 1000);
    var p = function (n) {
      return (n < 10 ? "0" : "") + n;
    };
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function toast(text, kind) {
    var el = $("toast");
    el.textContent = text;
    el.className = "toast show" + (kind ? " " + kind : "");
    clearTimeout(el._t);
    el._t = setTimeout(function () {
      el.className = "toast";
    }, 2800);
  }

  // 模态提示：用于登录态失效等必须被用户确认的消息。
  function showModal(text, title) {
    var mask = $("modalMask");
    if (!mask) return;
    $("modalTitle").textContent = title || "提示";
    $("modalText").textContent = text;
    mask.classList.add("open");
    mask.setAttribute("aria-hidden", "false");
    $("modalOk").focus();
  }

  function hideModal() {
    var mask = $("modalMask");
    if (!mask) return;
    mask.classList.remove("open");
    mask.setAttribute("aria-hidden", "true");
  }

  function notifyResult(res) {
    if (!res) return;
    var msg = res.message || "";
    if (msg) toast(msg, res.ok ? "success" : "error");
  }

  function markInvalid(input) {
    input.classList.add("invalid");
    setTimeout(function () {
      input.classList.remove("invalid");
    }, 1200);
  }

  function setLoading(btnId, spinnerId, loading) {
    var btn = $(btnId),
      sp = $(spinnerId);
    if (btn) btn.disabled = loading;
    if (sp) sp.classList.toggle("hidden", !loading);
  }

  // 课程下拉与「开始监听」的可用性统一由此推导，单一来源避免冲突：
  //   - 监听中：两者禁用（锁定本次监听的课程，防止中途换课造成错配）；
  //   - 未登录（无课程）：两者禁用；
  //   - 请求在途：两者禁用，避免连点重复提交。
  function syncRunControls() {
    var startBtn = $("startBtn");
    var sel = $("courseSelect");
    var disabled = !hasCourses || watching || runBusy;
    if (startBtn) startBtn.disabled = disabled;
    if (sel) sel.disabled = disabled;
  }

  // ─ 日志渲染 ────────────────────────────────────────────
  function appendLogs(items) {
    if (!items || !items.length) return;
    var body = $("logBody");
    var empty = $("logEmpty");
    if (empty) empty.remove();
    var frag = document.createDocumentFragment();
    items.forEach(function (it) {
      var line = document.createElement("div");
      line.className = "log-line " + (it.level || "info");
      var t = document.createElement("span");
      t.className = "log-time";
      t.textContent = fmtTime(it.ts);
      var x = document.createElement("span");
      x.className = "log-text";
      x.textContent = it.text;
      line.appendChild(t);
      line.appendChild(x);
      frag.appendChild(line);
    });
    body.appendChild(frag);
    logTotal += items.length;
    var lc = $("logCount");
    if (lc) lc.textContent = logTotal + " 条";
    if (autoScroll) body.scrollTop = body.scrollHeight;
  }

  function clearLogs() {
    logTotal = 0;
    var lc = $("logCount");
    if (lc) lc.textContent = "0 条";
    $("logBody").innerHTML =
      '<div class="log-empty" id="logEmpty">' +
      '<div class="log-empty-title">暂无日志</div>' +
      '<div class="log-empty-sub">登录并开始监听后将在此显示</div></div>';
  }

  // ─ 状态刷新 ────────────────────────────────────────────
  function setLoginChip(loggedIn, account) {
    var chip = $("loginChip");
    if (chip) {
      chip.className = "chip chip-login " + (loggedIn ? "on" : "off");
      $("loginText").textContent = loggedIn ? "已登录" : "未登录";
    }
    var logoutBtn = $("logoutBtn");
    if (logoutBtn) logoutBtn.disabled = !loggedIn;
    var userInput = $("userInput");
    if (loggedIn && account && userInput && !userInput.value) {
      userInput.value = account;
    }
  }

  function applyState(st) {
    var pill = $("statusPill");
    var running = !!(st && st.running);
    watching = running;
    pill.className = "status-pill " + (running ? "running" : "stopped");
    $("statusText").textContent = running ? "运行中" : "已停止";
    $("stopBtn").disabled = !running;
    syncRunControls();

    if (st) {
      $("monCourse").textContent = st.course_name || "—";
      $("lastTick").textContent = st.last_tick_ts
        ? fmtTime(st.last_tick_ts)
        : "—";
      var left = Number(st.limiter_remaining_s) || 0;
      var item = $("limiterItem");
      if (item) {
        if (left > 0) {
          $("limiterLeft").textContent = Math.ceil(left) + " 秒";
          item.classList.remove("hidden");
        } else {
          item.classList.add("hidden");
        }
      }
      if (typeof st.logged_in === "boolean") {
        setLoginChip(st.logged_in, st.user_account);
        if (!st.logged_in && $("courseSelect").value) {
          fillCourses([], "");
        }
      }
      if (st.notice) showNotice(st.notice);
    }
  }

  function tickClock() {
    var el = $("clockTime");
    if (el) el.textContent = fmtTime(Date.now() / 1000);
  }

  function showNotice(notice) {
    var b = $("noticeBanner");
    if (!b) return;
    // 登录态失效用模态确认（横幅易被忽略），其余仍走横幅。
    if (String(notice.text || "").indexOf("重新登录") !== -1) {
      showModal(notice.text, "登录失效");
      b.classList.add("hidden");
      return;
    }
    b.className = "notice-banner " + (notice.level || "error");
    b.textContent = notice.text;
    b.classList.remove("hidden");
  }

  function fillCourses(courses, selected) {
    var sel = $("courseSelect");
    sel.innerHTML = "";
    hasCourses = !!(courses && courses.length);
    if (!hasCourses) {
      sel.innerHTML = '<option value="">请先登录</option>';
      syncRunControls();
      return;
    }
    courses.forEach(function (c) {
      var o = document.createElement("option");
      o.value = c.id;
      o.textContent = c.name;
      if (String(c.id) === String(selected)) o.selected = true;
      sel.appendChild(o);
    });
    syncRunControls();
  }

  // ─ 轮询 ────────────────────────────────────────────────
  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(function () {
      api.drain_logs(logCursor).then(function (r) {
        if (!r) return;
        logCursor = r.next_id;
        appendLogs(r.items);
      });
      api.get_state().then(applyState);
    }, 400);
  }

  // ─ 初始化 ──────────────────────────────────────────────
  function applyBootstrap(res) {
    if (!res) return;
    $("versionChip").textContent = "v" + res.version;
    document.title = "对分易自动签到 v" + res.version;
    fillCourses(res.courses, res.selected);
    setLoginChip(res.logged_in, res.user_account);
    if (res.saved_account && !$("userInput").value) {
      $("userInput").value = res.saved_account;
    }
    if (res.logs && res.logs.length) {
      logCursor = res.logs[res.logs.length - 1].id;
      appendLogs(res.logs);
    }
    if (res.message) {
      if (res.message.indexOf("重新登录") !== -1) {
        showModal(res.message, "登录失败");
      } else {
        toast(res.message, "error");
      }
    }
  }

  function bootstrap() {
    api.bootstrap().then(function (res) {
      applyBootstrap(res);
      startPolling();
    });
  }

  // ─ 事件绑定 ────────────────────────────────────────────
  function doLogin() {
    var u = $("userInput").value.trim();
    if (!u) {
      markInvalid($("userInput"));
      toast("请输入对分易账号", "error");
      return;
    }
    if (!$("passInput").value) {
      markInvalid($("passInput"));
      toast("请输入密码", "error");
      return;
    }
    setLoading("loginBtn", "loginSpinner", true);
    var remember = $("rememberChk").checked;
    api.login_by_password(u, $("passInput").value, remember).then(function (r) {
      setLoading("loginBtn", "loginSpinner", false);
      notifyResult(r);
      if (!r || !r.ok) return;
      $("passInput").value = "";
      api.bootstrap().then(function (b) {
        fillCourses(b.courses, b.selected);
        setLoginChip(b.logged_in, b.user_account || u);
      });
    });
  }

  function bind() {
    // 登录（回车同样提交，避免每次都要点按钮）
    $("loginBtn").addEventListener("click", doLogin);
    ["userInput", "passInput"].forEach(function (id) {
      $(id).addEventListener("keydown", function (e) {
        if (e.key === "Enter") doLogin();
      });
    });

    // 退出登录：清掉保存的密码与本机登录态
    $("logoutBtn").addEventListener("click", function () {
      api.logout().then(function (r) {
        notifyResult(r);
        fillCourses([], "");
        setLoginChip(false, "");
        $("passInput").value = "";
        applyState({ running: false });
      });
    });

    // 课程切换
    $("courseSelect").addEventListener("change", function () {
      if (!this.value) return;
      api.select_course(this.value).then(function (r) {
        if (!r.ok) toast(r.message, "error");
      });
    });

    // 开始监听
    $("startBtn").addEventListener("click", function () {
      runBusy = true;
      syncRunControls();
      $("startSpinner").classList.remove("hidden");
      api.start_watching().then(function (r) {
        runBusy = false;
        $("startSpinner").classList.add("hidden");
        toast(r.message, r.ok ? "success" : "error");
        if (r.ok) applyState({ running: true });
        else syncRunControls();
      });
    });

    // 手动签到：服务端没下发签到码/汉字时，把群里的 4 位码或 4 个汉字填进来，
    // 登记后 5 分钟内遇到对应活动即自动使用
    function submitManual() {
      var input = $("manualInput");
      var value = (input.value || "").replace(/\s/g, "");
      if (value.length !== 4) {
        toast("请输入 4 位签到码或 4 个汉字", "error");
        return;
      }
      api.submit_manual_code(value).then(function (r) {
        toast(r.message, r.ok ? "success" : "error");
        if (r.ok) input.value = "";
      });
    }
    $("manualBtn").addEventListener("click", submitManual);
    $("manualInput").addEventListener("keydown", function (e) {
      if (e.key === "Enter") submitManual();
    });

    // 停止监听
    $("stopBtn").addEventListener("click", function () {
      api.stop_watching().then(function (r) {
        toast(r.message, r.ok ? "success" : "error");
        applyState({ running: false });
      });
    });

    // 自动滚动开关
    $("autoScrollBtn").addEventListener("click", function () {
      autoScroll = !autoScroll;
      this.classList.toggle("active", autoScroll);
      if (autoScroll) $("logBody").scrollTop = $("logBody").scrollHeight;
    });

    // 手动清空：同时清前端显示与后端队列，否则旧日志会在下次 bootstrap 时被拉回
    $("clearBtn").addEventListener("click", function () {
      clearLogs();
      api.clear_logs().then(function (r) {
        if (r && r.ok) logCursor = r.next_id;
      });
    });

    // 遮罩仅用于关闭设置抽屉
    $("overlay").addEventListener("click", closeSettings);

    // 模态提示：点「确定」或遮罩关闭；Esc 亦可关闭
    $("modalOk").addEventListener("click", hideModal);
    $("modalMask").addEventListener("click", function (e) {
      if (e.target === this) hideModal();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") hideModal();
    });

    // 关窗时确保停止监控
    window.addEventListener("beforeunload", function () {
      if (api) api.stop_watching();
    });

    // 自绘窗口按钮（无边框窗口）
    $("winMin").addEventListener("click", function () {
      if (api && api.window_minimize) api.window_minimize();
    });
    $("winClose").addEventListener("click", function () {
      if (api && api.window_close) api.window_close();
    });

    // 顶栏是可拖区域（.pywebview-drag-region）；其内的交互控件须阻止
    // mousedown 冒泡，否则点按钮会被 pywebview 判定为拖窗。
    Array.prototype.forEach.call(
      document.querySelectorAll(".topbar button, .topbar select, .topbar input"),
      function (el) {
        el.addEventListener("mousedown", function (e) {
          e.stopPropagation();
        });
      },
    );

    // 设置抽屉
    $("settingsBtn").addEventListener("click", openSettings);
    $("settingsClose").addEventListener("click", closeSettings);
    $("settingsSave").addEventListener("click", saveSettings);
    $("settingsReset").addEventListener("click", function () {
      renderSettings(settingsDefaults);
      toast("已填入默认值，点击保存生效");
    });
  }

  // ─ 设置抽屉 ────────────────────────────────────────────
  function fieldInputId(key) {
    return "set_" + key;
  }

  function buildSettingsForm() {
    var form = $("settingsForm");
    form.innerHTML = "";
    SETTINGS_GROUPS.forEach(function (group) {
      var box = document.createElement("div");
      box.className = "settings-group";
      var title = document.createElement("h3");
      title.className = "settings-group-title";
      title.textContent = group.title;
      box.appendChild(title);

      group.fields.forEach(function (f) {
        var row = document.createElement("div");
        row.className = "settings-row";

        var text = document.createElement("div");
        text.className = "settings-row-text";
        var label = document.createElement("div");
        label.className = "settings-row-label";
        label.textContent = f.label;
        var hint = document.createElement("div");
        hint.className = "settings-row-hint";
        hint.textContent = f.hint || "";
        text.appendChild(label);
        text.appendChild(hint);

        var ctrl = document.createElement("div");
        ctrl.className = "settings-row-control";
        var input = document.createElement("input");
        input.className = "input";
        input.id = fieldInputId(f.key);
        // 数值字段也用 text：后端负责类型转换与范围校验，
        // number 的原生步进控件样式与整体表单不协调。
        input.type = "text";
        ctrl.appendChild(input);

        row.appendChild(text);
        row.appendChild(ctrl);
        box.appendChild(row);
      });
      form.appendChild(box);
    });
  }

  function renderSettings(values) {
    SETTINGS_GROUPS.forEach(function (group) {
      group.fields.forEach(function (f) {
        var el = $(fieldInputId(f.key));
        if (!el) return;
        var v = values[f.key];
        el.value = v === undefined || v === null ? "" : String(v);
        el.classList.remove("invalid");
      });
    });
  }

  function collectSettings() {
    var patch = {};
    SETTINGS_GROUPS.forEach(function (group) {
      group.fields.forEach(function (f) {
        var el = $(fieldInputId(f.key));
        if (!el) return;
        patch[f.key] = el.value.trim();
      });
    });
    return patch;
  }

  function openSettings() {
    api.get_settings().then(function (r) {
      if (!r || !r.ok) {
        toast(r && r.message ? r.message : "读取配置失败", "error");
        return;
      }
      if (!Object.keys(settingsDefaults).length)
        settingsDefaults = Object.assign({}, r.values);
      settingsValues = r.values;
      renderSettings(settingsValues);
      $("settingsDrawer").classList.add("open");
      $("overlay").classList.add("open");
    });
  }

  function closeSettings() {
    $("settingsDrawer").classList.remove("open");
    $("overlay").classList.remove("open");
  }

  function saveSettings() {
    var patch = collectSettings();
    api.update_settings(patch).then(function (r) {
      if (!r || !r.ok) {
        toast(r && r.message ? r.message : "保存失败", "error");
        return;
      }
      settingsValues = r.values;
      renderSettings(settingsValues);
      toast("设置已保存", "success");
      closeSettings();
    });
  }

  // 等待 pywebview 注入 js_api
  window.addEventListener("pywebviewready", function () {
    api = window.pywebview.api;
    buildSettingsForm();
    bind();
    tickClock();
    clockTimer = setInterval(tickClock, 1000);
    bootstrap();
  });
})();
