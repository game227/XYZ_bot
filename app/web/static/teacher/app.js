/* Teacher Mini App (AUTH). The session token lives only in this page's memory.
 * Every request carries Telegram initData + Bearer token; the server re-checks both each time.
 * All user-supplied text is rendered with textContent (never innerHTML). */
(() => {
  "use strict";

  const tg = window.Telegram && window.Telegram.WebApp;
  const params = new URLSearchParams(location.search);
  const API = "/teacher/api";
  const THREAD_POLL_MS = 5000;
  const LIST_POLL_MS = 15000;

  let token = null;
  let currentThread = null;
  let lastMessageId = 0;
  let pollTimer = null;
  let pendingNewPassword = false;

  const $ = (id) => document.getElementById(id);
  const views = ["loading", "error", "login", "threads", "thread", "settings"];

  function show(name) {
    for (const v of views) $("view-" + v).hidden = v !== name;
    $("topbar").hidden = !token || name === "login";
    stopPolling();
    if (tg && tg.BackButton) {
      if (name === "thread" || name === "settings") tg.BackButton.show();
      else tg.BackButton.hide();
    }
  }

  function fatal(text) {
    $("fatal-text").textContent = text;
    show("error");
  }

  function setError(el, text) {
    el.textContent = text || "";
    el.hidden = !text;
  }

  class ApiError extends Error {
    constructor(status, code, message) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  async function api(method, path, body) {
    const headers = { "X-Telegram-Init-Data": tg.initData };
    if (token) headers["Authorization"] = "Bearer " + token;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let resp;
    try {
      resp = await fetch(API + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store",
        credentials: "omit",
      });
    } catch (e) {
      throw new ApiError(0, "network", "Tarmoq xatosi. Internetni tekshiring.");
    }
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    if (resp.ok) return data;
    const detail = data && data.detail;
    let code = "error";
    let message = "Xatolik yuz berdi.";
    if (detail && typeof detail === "object") {
      code = detail.code || code;
      message = detail.message || message;
    } else if (typeof detail === "string") {
      code = resp.status === 401 ? "init_data" : code;
      message = detail;
    }
    if (resp.status === 401 && code === "session") {
      onSessionLost(message);
    } else if (code === "init_data") {
      token = null;
      fatal(message);
    }
    throw new ApiError(resp.status, code, message);
  }

  function onSessionLost(message) {
    token = null;
    currentThread = null;
    resetLoginForm();
    setError($("login-error"), message || "Sessiya tugagan. Qayta kiring.");
    show("login");
  }

  function applySession(data) {
    token = data.token;
    $("teacher-name").textContent = data.teacher.full_name;
    $("session-until").textContent = "Sessiya " + data.expires_at_local + " gacha";
  }

  // ------------------------------------------------------------------ login

  function resetLoginForm() {
    pendingNewPassword = false;
    $("new-password-block").hidden = true;
    for (const id of ["login-password", "login-new-password", "login-new-password2"]) $(id).value = "";
    $("login-submit").textContent = "Kirish";
  }

  async function onLogin(ev) {
    ev.preventDefault();
    const login = $("login-login").value.trim();
    const password = $("login-password").value;
    setError($("login-error"), "");
    if (!login || !password) return setError($("login-error"), "Login va parolni kiriting.");
    const body = { login, password };
    if (pendingNewPassword) {
      const p1 = $("login-new-password").value;
      const p2 = $("login-new-password2").value;
      if (!p1) return setError($("login-error"), "Yangi parolni kiriting.");
      if (p1 !== p2) return setError($("login-error"), "Yangi parollar mos emas.");
      body.new_password = p1;
    }
    $("login-submit").disabled = true;
    try {
      const data = await api("POST", "/login", body);
      resetLoginForm();
      applySession(data);
      if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      await openStart();
    } catch (e) {
      if (e.code === "password_change_required") {
        pendingNewPassword = true;
        $("new-password-block").hidden = false;
        $("login-submit").textContent = "Parolni almashtirib kirish";
        setError($("login-error"), e.message);
        $("login-new-password").focus();
      } else if (e.code !== "init_data") {
        if (e.code === "bad_credentials") $("login-password").value = "";
        setError($("login-error"), e.message);
      }
    } finally {
      $("login-submit").disabled = false;
    }
  }

  // ------------------------------------------------------------------ threads

  async function openThreads() {
    show("threads");
    await loadThreads();
    pollTimer = setInterval(() => loadThreads().catch(() => {}), LIST_POLL_MS);
  }

  async function loadThreads() {
    const data = await api("GET", "/threads");
    const list = $("thread-list");
    list.replaceChildren();
    for (const t of data.threads) {
      const li = document.createElement("li");
      const grow = document.createElement("div");
      grow.className = "grow";
      const title = document.createElement("div");
      title.className = "title";
      title.textContent = t.student_name + (t.student_username ? " (@" + t.student_username + ")" : "");
      const sub = document.createElement("div");
      sub.className = "muted small";
      sub.textContent = (t.course_title || "") + " · " + t.last_message_at_local;
      grow.append(title, sub);
      li.append(grow);
      if (t.unread > 0) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = String(t.unread);
        li.append(badge);
      }
      li.addEventListener("click", () => openThread(t.id).catch(() => {}));
      list.append(li);
    }
    $("threads-empty").hidden = data.threads.length > 0;
  }

  // ------------------------------------------------------------------ one thread

  function renderMessage(m) {
    const div = document.createElement("div");
    div.className = "msg " + (m.from === "teacher" ? "msg-teacher" : "msg-student");
    const body = document.createElement("span");
    body.textContent = m.body;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = m.created_at_local;
    div.append(body, meta);
    $("messages").append(div);
  }

  function scrollToBottom() {
    window.scrollTo(0, document.body.scrollHeight);
  }

  async function openThread(id) {
    currentThread = id;
    lastMessageId = 0;
    $("messages").replaceChildren();
    setError($("send-error"), "");
    show("thread");
    const data = await api("GET", "/threads/" + id + "/messages");
    $("thread-title").textContent = data.thread.student_name +
      (data.thread.student_username ? " (@" + data.thread.student_username + ")" : "");
    $("thread-sub").textContent = data.thread.course_title || "";
    $("send-form").hidden = data.thread.is_closed;
    appendMessages(data.messages);
    scrollToBottom();
    pollTimer = setInterval(() => pollThread().catch(() => {}), THREAD_POLL_MS);
  }

  function appendMessages(messages) {
    for (const m of messages) {
      if (m.id <= lastMessageId) continue;
      renderMessage(m);
      lastMessageId = m.id;
    }
  }

  async function pollThread() {
    if (!currentThread) return;
    const id = currentThread;
    const data = await api("GET", "/threads/" + id + "/messages?after_id=" + lastMessageId);
    if (id !== currentThread) return;
    const before = lastMessageId;
    appendMessages(data.messages);
    if (lastMessageId !== before) scrollToBottom();
  }

  async function onSend(ev) {
    ev.preventDefault();
    const text = $("send-body").value.trim();
    if (!text || !currentThread) return;
    $("send-submit").disabled = true;
    setError($("send-error"), "");
    try {
      const data = await api("POST", "/threads/" + currentThread + "/messages", { body: text });
      $("send-body").value = "";
      appendMessages([data.message]);
      if (!data.delivered) setError($("send-error"), "Xabar saqlandi, lekin o‘quvchiga yetkazilmadi (botni bloklagan bo‘lishi mumkin).");
      scrollToBottom();
    } catch (e) {
      if (e.code !== "session" && e.code !== "init_data") setError($("send-error"), e.message);
    } finally {
      $("send-submit").disabled = false;
    }
  }

  // ------------------------------------------------------------------ settings

  async function onChangePassword(ev) {
    ev.preventDefault();
    setError($("pw-error"), "");
    $("pw-ok").hidden = true;
    const current = $("pw-current").value;
    const p1 = $("pw-new").value;
    if (p1 !== $("pw-new2").value) return setError($("pw-error"), "Yangi parollar mos emas.");
    try {
      const data = await api("POST", "/password", { current_password: current, new_password: p1 });
      applySession(data);
      for (const id of ["pw-current", "pw-new", "pw-new2"]) $(id).value = "";
      $("pw-ok").hidden = false;
    } catch (e) {
      if (e.code !== "session" && e.code !== "init_data") setError($("pw-error"), e.message);
    }
  }

  async function onLogout() {
    try { await api("POST", "/logout"); } catch (e) { /* session may already be gone */ }
    token = null;
    if (tg.close) tg.close();
    onSessionLost("Sessiya yakunlandi.");
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  async function openStart() {
    const threadId = parseInt(params.get("thread") || "", 10);
    if (threadId > 0) {
      params.delete("thread");
      try { return await openThread(threadId); } catch (e) { /* fall through to the list */ }
      if (!token) return;
    }
    await openThreads();
  }

  // ------------------------------------------------------------------ boot

  async function boot() {
    if (!tg || !tg.initData) {
      fatal("Bu sahifa faqat Telegram bot orqali ochiladi.");
      return;
    }
    tg.ready();
    tg.expand();
    if (tg.BackButton) {
      tg.BackButton.onClick(() => {
        if (!$("view-thread").hidden || !$("view-settings").hidden) openThreads().catch(() => {});
      });
    }
    $("login-form").addEventListener("submit", onLogin);
    $("send-form").addEventListener("submit", onSend);
    $("password-form").addEventListener("submit", onChangePassword);
    $("btn-logout").addEventListener("click", onLogout);
    $("btn-back").addEventListener("click", () => { currentThread = null; openThreads().catch(() => {}); });
    $("btn-settings").addEventListener("click", () => show("settings"));
    $("btn-settings-back").addEventListener("click", () => openThreads().catch(() => {}));
    $("btn-refresh").addEventListener("click", () => loadThreads().catch(() => {}));
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && token && currentThread) pollThread().catch(() => {});
    });

    try {
      const data = await api("POST", "/resume");
      applySession(data);
      await openStart();
    } catch (e) {
      if (e.code === "session") {
        setError($("login-error"), "");
        show("login");
      } else if (e.code !== "init_data") {
        fatal(e.message);
      }
    }
  }

  boot();
})();
