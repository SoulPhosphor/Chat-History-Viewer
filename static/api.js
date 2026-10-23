"use strict";

// ── Server calls and browser errors (AUDIT.md #23, #35) ──────────────────────
// Every call to the server goes through apiRequest(). It sends JSON bodies with
// the right Content-Type, checks the reply, and on failure:
//   - shows a short, plain-language message on screen (the server's own
//     {"error": "..."} text when it sent one), unless the caller shows it;
//   - sends the technical detail (status, address, stack) to the error log;
//   - throws, so code after the call ("Saved", new title, …) never runs for a
//     change that didn't happen.
// Uncaught errors and unhandled promise rejections are sent to the log too.
// Loaded first, so later modules and their start-up code are covered.

const API_MESSAGES = {
  offline:
    "The app's server isn't responding. Check that it's still running, then try again.",
  change: "Something went wrong. Your change wasn't saved. Please try again.",
  load: "Something went wrong while loading this. Please try again.",
  tooLarge: "This is too large to save. The limit is 20 MB.",
};

// The error apiRequest throws. `userMessage` is what was (or should be) shown;
// `reported` means it is already on screen and in the log, so the global
// handlers below leave it alone.
class ApiRequestError extends Error {
  constructor(userMessage, { status = 0, data = {} } = {}) {
    super(userMessage);
    this.name = "ApiRequestError";
    this.userMessage = userMessage;
    this.status = status;
    this.data = data;
    this.reported = true;
  }
}

// apiRequest(url, { method, body, signal, keepalive, headers, action, notify,
//                   log, allow, expect })
//   body    an object (sent as JSON), a JSON string, or FormData (sent as is);
//   action  what the user was doing, in a few words, for the log
//           ("Rename conversation");
//   notify  false when the caller shows the message itself (err.userMessage);
//   log     false for background polling, which would otherwise fill the log
//           while the server is stopped;
//   allow   statuses the caller handles as normal answers: they are returned
//           as data, not shown or logged;
//   expect  statuses the caller handles as a known outcome (a Scratch Pad
//           changed elsewhere, 409): still thrown, with err.status, but not
//           shown or logged.
async function apiRequest(url, options = {}) {
  const {
    method = "GET",
    body,
    signal,
    keepalive,
    headers = {},
    action = "",
    notify = true,
    log = true,
    allow = [],
    expect = [],
  } = options;
  const callStack = new Error().stack || "";
  const init = { method, headers: { ...headers }, signal };
  if (keepalive) init.keepalive = true;
  if (body !== undefined) {
    if (body instanceof FormData) {
      init.body = body;
    } else {
      init.headers["Content-Type"] = "application/json";
      init.body = typeof body === "string" ? body : JSON.stringify(body);
    }
  }
  const isChange = method !== "GET";
  let response;
  try {
    response = await fetch(url, init);
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw apiFailure(API_MESSAGES.offline, {
      method, url, action, notify, log, status: 0, callStack, cause: err,
    });
  }
  let data = {};
  let parsed = true;
  try {
    data = await response.json();
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    parsed = false;
  }
  if (response.ok && parsed && data && typeof data === "object") return data;
  if (allow.includes(response.status) && parsed) return data;
  const expected = expect.includes(response.status);
  let message = parsed && data && typeof data.error === "string" && data.error
    ? data.error
    : response.status === 413
      ? API_MESSAGES.tooLarge
      : isChange ? API_MESSAGES.change : API_MESSAGES.load;
  throw apiFailure(message, {
    method, url, action, status: response.status, callStack,
    notify: notify && !expected,
    log: log && !expected,
    data: parsed ? data : {},
    detail: response.ok ? "The reply was not readable." : "",
  });
}

function apiFailure(message, { method, url, action, notify, log, status, callStack, data = {}, cause, detail = "" }) {
  if (notify) showAppNotice(message);
  if (log) reportBrowserError({
    error_type: cause ? cause.name || "NetworkError" : "RequestFailed",
    message: detail || (cause ? String(cause.message || cause) : message),
    stack: stackWithout(callStack, "/static/api.js"),
    action: action || "Server call",
    request: { method, url },
    status,
  });
  return new ApiRequestError(message, { status, data });
}

// Drop the helper's own frames so the log points at the code that called it.
function stackWithout(stack, file) {
  return String(stack || "")
    .split("\n")
    .filter((line) => !line.includes(file))
    .join("\n");
}

// ── On-screen notice ─────────────────────────────────────────────────────────
// One shared line near the bottom of the window for failures. It stays until
// it is closed or a few seconds pass with no new failure.

let _appNoticeTimer = null;

function showAppNotice(message) {
  const box = document.getElementById("app-notice");
  const text = document.getElementById("app-notice-text");
  if (!box || !text) return;
  text.textContent = message;
  box.hidden = false;
  clearTimeout(_appNoticeTimer);
  _appNoticeTimer = setTimeout(hideAppNotice, 8000);
}

function hideAppNotice() {
  const box = document.getElementById("app-notice");
  if (box) box.hidden = true;
  clearTimeout(_appNoticeTimer);
}

document.addEventListener("click", (e) => {
  if (e.target.closest && e.target.closest("#app-notice-close")) hideAppNotice();
});

// ── Sending browser errors to the log ────────────────────────────────────────

function browserInfo() {
  const ua = navigator.userAgent || "";
  const known = [
    ["Edge", /Edg\/([\d.]+)/],
    ["Opera", /OPR\/([\d.]+)/],
    ["Firefox", /Firefox\/([\d.]+)/],
    ["Chrome", /Chrome\/([\d.]+)/],
    ["Safari", /Version\/([\d.]+).*Safari/],
  ];
  let name = "Unknown";
  let version = "";
  for (const [n, re] of known) {
    const m = ua.match(re);
    if (m) {
      name = n;
      version = m[1];
      break;
    }
  }
  return {
    name,
    version,
    window: `${window.innerWidth}×${window.innerHeight}`,
    screen: currentScreenName(),
  };
}

// Which part of the app was showing, in the app's own words.
function currentScreenName() {
  try {
    if (typeof state === "object" && state) {
      if (state.activeSpecialView) return String(state.activeSpecialView);
      if (state.activeId) return "conversation";
    }
  } catch (_) {
    /* state isn't defined yet during start-up */
  }
  return "start";
}

let _reportingError = false;
let _reportsSent = 0;

// Send one browser error to the server's log. Never throws and never reports
// its own failure, so a broken server can't cause a loop. The server keeps
// only the shape of the request (address pattern, field names), never text.
function reportBrowserError(entry) {
  if (_reportingError || _reportsSent >= 50) return;
  _reportingError = true;
  _reportsSent += 1;
  try {
    fetch("/api/error-log/browser", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...entry, browser: browserInfo() }),
      keepalive: true,
    }).catch(() => {});
  } catch (_) {
    /* nothing more can be done */
  } finally {
    _reportingError = false;
  }
}

window.addEventListener("error", (e) => {
  // Failed <img>/<script> loads also arrive here, without an error object.
  if (!e.error && !e.message) return;
  const err = e.error || {};
  reportBrowserError({
    error_type: err.name || "Error",
    message: String(err.message || e.message || ""),
    stack: err.stack || `at ${e.filename || "?"}:${e.lineno || 0}:${e.colno || 0}`,
    action: "Uncaught error",
  });
});

window.addEventListener("unhandledrejection", (e) => {
  const err = e.reason;
  if (err && err.reported) {
    // Already shown and logged by apiRequest; the caller simply stopped.
    e.preventDefault();
    return;
  }
  if (err && err.name === "AbortError") {
    e.preventDefault();
    return;
  }
  reportBrowserError({
    error_type: (err && err.name) || "UnhandledRejection",
    message: String((err && err.message) || err || ""),
    stack: (err && err.stack) || "",
    action: "Unhandled promise rejection",
  });
});
