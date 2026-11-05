/* Helpers shared by the review workspace pages: DOM building, JSON requests, toasts and dialogs. */
window.ReviewUI = (() => {
  "use strict";

  const FLASH_KEY = "review.flash";

  /* Build an element. Text is always set as text (never parsed as HTML), so values read from PDFs are safe. */
  function h(tag, attrs = {}, ...children) {
    const element = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") element.className = value;
      else if (key === "text") element.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") element.addEventListener(key.slice(2), value);
      else element.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      element.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return element;
  }

  async function api(url, { method = "GET", body } = {}) {
    const options = { method, headers: { Accept: "application/json" } };
    if (method !== "GET") {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body || {});
      options.keepalive = true; // a save started just before leaving the page still reaches the server
    }
    const response = await fetch(url, options);
    let data = null;
    if (response.status !== 204) {
      try {
        data = await response.json();
      } catch {
        data = null;
      }
    }
    if (!response.ok) {
      const error = new Error((data && (data.error || data.message)) || `The request failed (${response.status}).`);
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return data;
  }

  function toast(message, kind = "info", { link = null, timeout = 5000 } = {}) {
    const host = document.getElementById("rv-toasts");
    if (!host) return;
    const item = h("div", { class: `rv-toast ${kind}`, role: kind === "error" ? "alert" : "status" },
      h("span", { text: message }),
      link ? h("a", { href: link.href, text: link.label }) : null,
      h("button", { type: "button", class: "rv-toast-close", "aria-label": "Dismiss", onclick: () => item.remove() }, "×"));
    host.append(item);
    if (timeout) setTimeout(() => item.remove(), timeout);
  }

  /* A message to show on the next page, for actions that navigate away. */
  function flashNext(message, kind = "success") {
    try {
      sessionStorage.setItem(FLASH_KEY, JSON.stringify({ message, kind }));
    } catch {
      /* storage can be unavailable; the message is only a courtesy */
    }
  }

  function showPendingFlash() {
    try {
      const stored = sessionStorage.getItem(FLASH_KEY);
      if (!stored) return;
      sessionStorage.removeItem(FLASH_KEY);
      const { message, kind } = JSON.parse(stored);
      toast(message, kind);
    } catch {
      /* ignore */
    }
  }

  /*
   * A modal dialog. Resolves with true (confirmation), the entered value (input dialogs) or null (cancelled).
   * options: title, message, body (a Node), input ("text" | "textarea" | "select"), label, options [{value, label}],
   *          suggestions [string], placeholder, required, confirmLabel, cancelLabel, hideCancel, danger
   */
  function dialog(options) {
    const element = document.getElementById("rv-dialog");
    return new Promise((resolve) => {
      let field = null;
      if (options.input === "textarea") {
        field = h("textarea", { rows: 3, placeholder: options.placeholder || null });
      } else if (options.input === "select") {
        field = h("select", {}, (options.options || []).map((option) => h("option", { value: option.value }, option.label)));
      } else if (options.input === "text") {
        field = h("input", { type: "text", maxlength: 60, placeholder: options.placeholder || null, list: options.suggestions ? "rv-dialog-suggestions" : null });
      }

      const close = (result) => {
        element.removeEventListener("cancel", onCancel);
        if (element.open) element.close();
        resolve(result);
      };
      const onCancel = (event) => {
        event.preventDefault();
        close(null);
      };

      const form = h("form", { class: "rv-dialog-form", method: "dialog" },
        h("h2", { text: options.title }),
        options.message ? h("p", { text: options.message }) : null,
        options.body || null,
        field ? h("label", { class: "rv-dialog-field" }, options.label ? h("span", { text: options.label }) : null, field) : null,
        options.suggestions ? h("datalist", { id: "rv-dialog-suggestions" }, options.suggestions.map((value) => h("option", { value }))) : null,
        h("div", { class: "rv-dialog-actions" },
          options.hideCancel ? null : h("button", { type: "button", class: "rv-btn", onclick: () => close(null) }, options.cancelLabel || "Cancel"),
          h("button", { type: "submit", class: `rv-btn ${options.danger ? "rv-btn-danger" : "rv-btn-primary"}` }, options.confirmLabel || "OK")));

      form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (field && options.required && !field.value.trim()) {
          field.focus();
          return;
        }
        close(field ? field.value : true);
      });

      element.replaceChildren(form);
      element.addEventListener("cancel", onCancel);
      element.showModal();
      (field || form.querySelector("[type=submit]")).focus();
    });
  }

  document.addEventListener("DOMContentLoaded", showPendingFlash);

  return { h, api, toast, dialog, flashNext };
})();
