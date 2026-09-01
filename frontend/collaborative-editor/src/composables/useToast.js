/**
 * useToast.js - Toast notification system
 *
 * Lightweight toast composable. Replaces all alert() calls.
 * Types: success, error, warning, info, loading.
 *
 * Usage:
 *   import { useToast } from "../composables/useToast.js";
 *   const toast = useToast();
 *   toast.success("Submit OK");
 *   toast.error("Failed", { duration: 5000 });
 */

const TOAST_CONTAINER_ID = "umo-toast-container";

const TYPE_DURATION = {
  success: 3000,
  error: 5000,
  warning: 4000,
  info: 3000,
  loading: 0,
};

let toastId = 0;

function ensureContainer() {
  let c = document.getElementById(TOAST_CONTAINER_ID);
  if (!c) {
    c = document.createElement("div");
    c.id = TOAST_CONTAINER_ID;
    c.className = "umo-toast-container";
    document.body.appendChild(c);
  }
  return c;
}

function safeString(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  if (typeof v === "object") {
    try { return JSON.stringify(v); } catch { return String(v); }
  }
  return String(v);
}

function getIcon(type) {
  switch (type) {
    case "success": return "\u2713";
    case "error": return "\u2717";
    case "warning": return "\u26A0";
    case "info": return "\u2139";
    case "loading": return "\u27F3";
    default: return "\u2139";
  }
}

function createToast(type, message, options) {
  const id = ++toastId;
  const dur = (options && options.duration != null) ? options.duration : (TYPE_DURATION[type] || 3000);
  const container = ensureContainer();

  const el = document.createElement("div");
  el.className = "umo-toast umo-toast-" + type;
  el.dataset.toastId = id;

  const icon = document.createElement("span");
  icon.className = "umo-toast-icon";
  icon.textContent = getIcon(type);

  const msg = document.createElement("span");
  msg.className = "umo-toast-message";
  msg.textContent = safeString(message);

  const close = document.createElement("button");
  close.className = "umo-toast-close";
  close.innerHTML = "&times;";
  close.setAttribute("aria-label", "Close");
  close.addEventListener("click", function () { dismiss(id); });

  el.appendChild(icon);
  el.appendChild(msg);
  el.appendChild(close);
  container.appendChild(el);

  requestAnimationFrame(function () { el.classList.add("umo-toast-enter"); });

  var timer = null;
  if (dur > 0) {
    timer = setTimeout(function () { dismiss(id); }, dur);
  }

  return {
    id: id,
    dismiss: function () { dismiss(id, timer); },
    updateMessage: function (newMsg) {
      var m = el.querySelector(".umo-toast-message");
      if (m) m.textContent = safeString(newMsg);
    },
    updateType: function (t) {
      el.className = "umo-toast umo-toast-" + t + " umo-toast-enter";
      var ic = el.querySelector(".umo-toast-icon");
      if (ic) ic.textContent = getIcon(t);
    },
  };
}

function dismiss(id, timer) {
  if (timer) clearTimeout(timer);
  var el = document.querySelector('[data-toast-id="' + id + '"]');
  if (!el) return;
  el.classList.remove("umo-toast-enter");
  el.classList.add("umo-toast-exit");
  setTimeout(function () {
    if (el.parentNode) el.parentNode.removeChild(el);
    var c = document.getElementById(TOAST_CONTAINER_ID);
    if (c && !c.children.length && c.parentNode) {
      c.parentNode.removeChild(c);
    }
  }, 300);
}

function dismissAll() {
  var c = document.getElementById(TOAST_CONTAINER_ID);
  if (!c) return;
  var arr = [].slice.call(c.querySelectorAll(".umo-toast"));
  arr.forEach(function (el) {
    dismiss(Number(el.dataset.toastId));
  });
}

export function useToast() {
  return {
    success: function (message, options) { return createToast("success", message, options); },
    error: function (message, options) { return createToast("error", message, options); },
    warning: function (message, options) { return createToast("warning", message, options); },
    info: function (message, options) { return createToast("info", message, options); },
    loading: function (message, options) { return createToast("loading", message, options); },
    dismiss: dismiss,
    dismissAll: dismissAll,
  };
}

var _globalToast = null;
export function getGlobalToast() {
  if (!_globalToast) _globalToast = useToast();
  return _globalToast;
}
