/*
 * Review screen: extracted fields on the left, the document on the right.
 *
 * Every change is saved as soon as a field is left (or Enter is pressed), and the server answers with the new
 * validation messages and the list of values that still need a look. Selecting a field highlights where it was
 * found on the page; drawing a box on the page replaces the selected field's value with the text inside the box.
 */
(() => {
  "use strict";
  const { h, api, toast, dialog, flashNext } = window.ReviewUI;
  const boot = JSON.parse(document.getElementById("rv-boot").textContent);
  const { schema, urls } = boot;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const clamp = (value, low, high) => Math.min(Math.max(value, low), high);

  const ZOOM_STEPS = [0.5, 0.67, 0.8, 1, 1.25, 1.5, 2, 2.5, 3];
  const PREFS_KEY = "review.prefs";
  const prefs = { showConfidence: false, showHidden: false, thumbs: false, sideWidth: 440, collapsed: {}, ...readPrefs() };
  const state = { doc: boot.document, activePath: null, zoom: 1, saving: 0, saveFailed: false, currentPage: 0 };
  const drafts = new Map();

  const definitions = {};
  const sectionOf = {};
  for (const section of schema.sections) {
    for (const field of section.fields) {
      definitions[field.id] = field;
      sectionOf[field.id] = section.id;
    }
  }

  const fieldsForm = $("#rv-fields");
  const pagesEl = $("#rv-pages");
  const pageEls = [];
  const summaryStatus = h("div", { class: "rv-summary-status" });
  const toggles = {};

  function readPrefs() {
    try {
      return JSON.parse(localStorage.getItem(PREFS_KEY)) || {};
    } catch {
      return {};
    }
  }

  function savePrefs() {
    try {
      localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
    } catch {
      /* preferences are a convenience; the page works without them */
    }
  }

  // ---------- paths: "invoice_number" for a field, "tax_details.0.amount" for a table cell ----------

  function definitionFor(path) {
    const [id, , column] = path.split(".");
    const definition = definitions[id];
    if (!definition) return null;
    return column === undefined ? definition : definition.columns.find((item) => item.id === column) || null;
  }

  function stateFor(path) {
    const [id, row, column] = path.split(".");
    if (column === undefined) return state.doc.fields[id] || {};
    return ((state.doc.tables[id] || [])[Number(row)] || {})[column] || {};
  }

  function labelFor(path) {
    const [id, row, column] = path.split(".");
    const definition = definitions[id];
    if (!definition) return path;
    if (column === undefined) return definition.label;
    return `${definition.label} row ${Number(row) + 1}, ${definitionFor(path)?.label || column}`;
  }

  const editable = () => !state.doc.read_only;
  const capturable = (path) => editable() && Boolean(path) && !definitions[path.split(".")[0]]?.manual;

  function* everyState() {
    for (const [id, value] of Object.entries(state.doc.fields || {})) yield [id, value];
    for (const [id, rows] of Object.entries(state.doc.tables || {})) {
      for (const [index, row] of rows.entries()) {
        for (const [column, value] of Object.entries(row)) yield [`${id}.${index}.${column}`, value];
      }
    }
  }

  /* Scroll one container so the element is visible. Unlike scrollIntoView, this never moves the page itself. */
  function revealIn(container, element, margin = 12) {
    if (!container || !element) return;
    const box = element.getBoundingClientRect();
    const view = container.getBoundingClientRect();
    if (box.top < view.top + margin) container.scrollTop -= view.top + margin - box.top;
    else if (box.bottom > view.bottom - margin) container.scrollTop += Math.min(box.bottom - view.bottom + margin, box.top - view.top - margin);
    if (box.left < view.left + margin) container.scrollLeft -= view.left + margin - box.left;
    else if (box.right > view.right - margin) container.scrollLeft += Math.min(box.right - view.right + margin, box.left - view.left - margin);
  }

  function formatStamp(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString(undefined, { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function formatDuration(seconds) {
    const total = Math.round(seconds || 0);
    if (total < 60) return `${total} s`;
    const minutes = Math.floor(total / 60);
    if (minutes < 60) return `${minutes} min ${total % 60} s`;
    return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
  }

  // ---------- sidebar ----------

  let controlCounter = 0;

  function setControlValue(control, value) {
    if (control.tagName === "SELECT" && value && ![...control.options].some((option) => option.value === value)) {
      control.append(h("option", { value }, `${value} (not in the list)`));
    }
    control.value = value || "";
  }

  function makeControl(definition, path, value) {
    const attrs = { id: `rv-control-${++controlCounter}`, name: path, "data-path": path, disabled: !editable() };
    let control;
    if (definition.type === "enum") {
      control = h("select", attrs, h("option", { value: "" }, "—"), definition.options.map((option) => h("option", { value: option }, option)));
    } else if (definition.multiline) {
      control = h("textarea", { ...attrs, rows: 3, spellcheck: "false" });
    } else {
      control = h("input", {
        ...attrs,
        type: "text",
        spellcheck: "false",
        inputmode: definition.type === "number" ? "decimal" : null,
        placeholder: definition.type === "date" ? "YYYY-MM-DD" : null,
      });
    }
    setControlValue(control, drafts.has(path) ? drafts.get(path).value : value);
    return control;
  }

  function renderField(definition) {
    const path = definition.id;
    const control = makeControl(definition, path, stateFor(path).value);
    return h("div", { class: "rv-field", "data-path": path },
      h("div", { class: "rv-field-top" },
        h("label", { for: control.id, title: definition.description || null },
          definition.label,
          definition.required ? h("span", { class: "rv-required", title: "Required" }, " *") : null,
          definition.hidden ? h("span", { class: "rv-tag" }, "hidden") : null),
        h("span", { class: "rv-field-state" })),
      control,
      h("ul", { class: "rv-messages" }));
  }

  function renderTable(definition) {
    const rows = state.doc.tables[definition.id] || [];
    const canEdit = editable();
    let body;
    if (rows.length) {
      body = h("div", { class: "rv-table-scroll" },
        h("table", {},
          h("thead", {}, h("tr", {},
            h("th", { class: "rv-row-number", scope: "col" }, "#"),
            definition.columns.map((column) => h("th", { scope: "col", class: column.type === "number" ? "num" : null }, column.label)),
            canEdit ? h("th", { class: "rv-row-tools" }, h("span", { class: "visually-hidden" }, "Row actions")) : null)),
          h("tbody", {}, rows.map((row, index) => h("tr", {},
            h("td", { class: "rv-row-number" }, String(index + 1)),
            definition.columns.map((column) => {
              const path = `${definition.id}.${index}.${column.id}`;
              const control = makeControl(column, path, (row[column.id] || {}).value);
              control.setAttribute("aria-label", `${column.label}, row ${index + 1}`);
              return h("td", { class: `rv-cell${column.type === "number" ? " num" : ""}`, "data-path": path }, control);
            }),
            canEdit
              ? h("td", { class: "rv-row-tools" },
                h("button", {
                  type: "button",
                  class: "rv-icon-btn rv-row-delete",
                  title: `Delete row ${index + 1}`,
                  "aria-label": `Delete row ${index + 1}`,
                  onclick: () => deleteRow(definition.id, index),
                }, "×"))
              : null)))));
    } else {
      body = h("p", {
        class: "rv-table-empty",
        text: canEdit ? "No rows yet. Add a row, select a cell, then draw a box around its value on the document." : "No rows.",
      });
    }
    return h("div", { class: "rv-table", "data-table": definition.id },
      h("div", { class: "rv-table-head" },
        h("span", { class: "rv-table-title" }, definition.label,
          h("span", { class: "rv-table-count", text: ` · ${rows.length} ${rows.length === 1 ? "row" : "rows"}` })),
        canEdit ? h("button", { type: "button", class: "rv-btn rv-btn-small", onclick: () => addRow(definition.id) }, "+ Add row") : null),
      body,
      h("ul", { class: "rv-messages", "data-table-messages": definition.id }));
  }

  function renderSidebar() {
    const sections = [];
    for (const section of schema.sections) {
      const fields = section.fields.filter((field) => prefs.showHidden || !field.hidden);
      if (!fields.length) continue;
      const collapsed = Boolean(prefs.collapsed[section.id]);
      sections.push(h("section", { class: `rv-section${collapsed ? " collapsed" : ""}`, "data-section": section.id },
        h("button", { type: "button", class: "rv-section-head", "aria-expanded": String(!collapsed), onclick: () => toggleSection(section.id) },
          h("span", { class: "rv-chevron", "aria-hidden": "true" }),
          h("span", { class: "rv-section-title", text: section.label }),
          h("span", { class: "rv-section-badge" })),
        h("div", { class: "rv-section-body" }, fields.map((field) => (field.type === "table" ? renderTable(field) : renderField(field))))));
    }
    fieldsForm.replaceChildren(...sections);
    refresh();
  }

  function toggleSection(id, collapse = !prefs.collapsed[id]) {
    prefs.collapsed[id] = collapse;
    savePrefs();
    const section = fieldsForm.querySelector(`[data-section="${id}"]`);
    if (!section) return;
    section.classList.toggle("collapsed", collapse);
    section.querySelector(".rv-section-head").setAttribute("aria-expanded", String(!collapse));
  }

  function replaceTable(tableId) {
    const current = fieldsForm.querySelector(`[data-table="${tableId}"]`);
    if (current) current.replaceWith(renderTable(definitions[tableId]));
  }

  function switchControl(label, checked, onChange) {
    const input = h("input", { type: "checkbox", role: "switch" });
    input.checked = checked;
    input.addEventListener("change", () => onChange(input.checked));
    return { input, label: h("label", { class: "rv-switch" }, input, h("span", { text: label })) };
  }

  function buildSummary() {
    toggles.confidence = switchControl("Confidence", prefs.showConfidence, (on) => {
      prefs.showConfidence = on;
      savePrefs();
      refresh();
    });
    toggles.hidden = switchControl("Hidden fields", prefs.showHidden, (on) => {
      prefs.showHidden = on;
      savePrefs();
      renderSidebar();
    });
    $("#rv-summary").replaceChildren(summaryStatus, h("div", { class: "rv-summary-toggles" }, toggles.confidence.label, toggles.hidden.label));
  }

  function stateBadges(value, unsure) {
    const badges = [];
    if (prefs.showConfidence && typeof value.confidence === "number") {
      const level = value.confidence >= boot.threshold ? "high" : value.confidence >= 0.5 ? "medium" : "low";
      badges.push(h("span", { class: `rv-score ${level}`, title: "How sure the extractor is about this value" }, `${Math.round(value.confidence * 100)}%`));
    }
    if (value.validated && (value.source === "human" || value.source === "capture")) {
      badges.push(h("span", { class: "rv-state edited", title: "Changed by a reviewer" }, "Edited"));
    } else if (value.validated && editable()) {
      badges.push(h("span", { class: "rv-state checked", title: "Checked by a reviewer" }, "✓ Checked"));
    } else if (unsure) {
      badges.push(h("span", { class: "rv-state review", title: "The extractor is not sure about this value" }, "Check"));
    }
    return badges;
  }

  function messagesByPath() {
    const grouped = new Map();
    for (const message of state.doc.messages || []) {
      const key = message.path ?? "";
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(message);
    }
    return grouped;
  }

  /* Bring every field, message, badge and page highlight in line with state.doc, without re-rendering inputs. */
  function refresh() {
    const needs = new Set(state.doc.needs_review || []);
    const messages = messagesByPath();

    for (const wrapper of $$(".rv-field, .rv-cell", fieldsForm)) {
      const path = wrapper.dataset.path;
      const value = stateFor(path);
      const own = messages.get(path) || [];
      wrapper.classList.toggle("needs-review", needs.has(path));
      wrapper.classList.toggle("is-validated", Boolean(value.validated));
      wrapper.classList.toggle("has-error", failedPaths.has(path) || own.some((message) => message.level === "error"));
      wrapper.classList.toggle("has-warning", own.some((message) => message.level === "warning"));
      wrapper.classList.toggle("active", path === state.activePath);
      const control = wrapper.querySelector("input, select, textarea");
      if (control && !drafts.has(path) && control !== document.activeElement && control.value !== (value.value || "")) setControlValue(control, value.value);
      if (wrapper.classList.contains("rv-cell")) {
        if (own.length) control?.setAttribute("title", own.map((message) => message.text).join("\n"));
        else control?.removeAttribute("title");
        continue;
      }
      wrapper.querySelector(".rv-field-state").replaceChildren(...stateBadges(value, needs.has(path)));
      wrapper.querySelector(".rv-messages").replaceChildren(...own.map((message) => h("li", { class: message.level, text: message.text })));
    }

    for (const list of $$("[data-table-messages]", fieldsForm)) {
      const tableId = list.dataset.tableMessages;
      const items = [];
      for (const [path, entries] of messages) {
        if (path !== tableId && !path.startsWith(`${tableId}.`)) continue;
        for (const message of entries) {
          items.push(h("li", { class: message.level, text: path === tableId ? message.text : `${labelFor(path)}: ${message.text}` }));
        }
      }
      list.replaceChildren(...items);
    }

    for (const section of $$(".rv-section", fieldsForm)) {
      const count = section.querySelectorAll(".needs-review").length;
      const badge = section.querySelector(".rv-section-badge");
      badge.textContent = count ? String(count) : "";
      badge.title = count ? `${count} to check` : "";
    }

    renderSummary();
    $("#rv-doc-messages").replaceChildren(...(messages.get("") || []).map((message) => h("li", { class: message.level, text: message.text })));
    renderBanner();
    renderLabels();
    renderOverlays();
    updateCaptureHint();
  }

  function renderSummary() {
    const needs = state.doc.needs_review || [];
    const errors = (state.doc.messages || []).filter((message) => message.level === "error").length;
    const warnings = (state.doc.messages || []).filter((message) => message.level === "warning").length;
    const items = [];
    if (editable()) {
      items.push(needs.length
        ? h("button", { type: "button", class: "rv-summary-review", title: "Go to the next value to check (N)", onclick: () => focusPath(needs[0]) },
          h("b", { text: String(needs.length) }), ` ${needs.length === 1 ? "value" : "values"} to check`)
        : h("span", { class: "rv-summary-ok" }, "Nothing left to check"));
    }
    if (errors) items.push(h("button", { type: "button", class: "rv-summary-error", onclick: () => focusFirstMessage("error") }, `${errors} ${errors === 1 ? "error" : "errors"}`));
    if (warnings) items.push(h("button", { type: "button", class: "rv-summary-warning", onclick: () => focusFirstMessage("warning") }, `${warnings} ${warnings === 1 ? "warning" : "warnings"}`));
    summaryStatus.replaceChildren(...items);
  }

  function renderBanner() {
    const doc = state.doc;
    const banner = $("#rv-banner");
    const by = (name, stamp) => `${name ? ` by ${name}` : ""}${stamp ? ` on ${formatStamp(stamp)}` : ""}`;
    const texts = {
      importing: "Extracting data from the document…",
      failed_import: doc.note || "The data could not be extracted.",
      postponed: "This document is postponed. You can still edit and confirm it.",
      confirmed: `Confirmed${by(doc.confirmed_by, doc.confirmed_at)}${doc.automated ? " (automated)" : ""}. Export it to Excel, or return it to review to change values.`,
      exported: `Exported${by(doc.exported_by, doc.exported_at)}.`,
      rejected: `Rejected${by(doc.rejected_by, doc.rejected_at)}.`,
      deleted: `Deleted${by(doc.deleted_by, doc.deleted_at)}. Restore it to work on it again.`,
    };
    const text = texts[doc.status];
    banner.hidden = !text;
    if (!text) return;
    banner.className = `rv-banner s-${doc.status}`;
    banner.replaceChildren(
      h("p", { text }),
      doc.note && doc.status !== "failed_import" ? h("p", { class: "rv-banner-note", text: `Note: ${doc.note}` }) : null,
      doc.status === "exported" && doc.export_file
        ? h("a", { href: urls.export.replace("FILE", encodeURIComponent(doc.export_file)), text: `Download ${doc.export_file}` })
        : null);
  }

  function renderLabels() {
    const canChange = (state.doc.actions || []).includes("remove_label");
    $("#rv-labels").replaceChildren(...(state.doc.labels || []).map((label) => h("span", { class: "rv-label" }, label,
      canChange
        ? h("button", { type: "button", class: "rv-label-remove", title: "Remove label", "aria-label": `Remove label ${label}`, onclick: () => runAction("remove_label", { label }) }, "×")
        : null)));
  }

  function updateHeader() {
    const chip = $(".rv-doc-title .rv-status");
    chip.className = `rv-status s-${state.doc.status}`;
    chip.textContent = state.doc.status_label;
    $("#rv-queue-name").textContent = state.doc.queue_name;
  }

  // ---------- selecting fields ----------

  function activate(path, { scroll = false } = {}) {
    state.activePath = path;
    for (const element of $$(".rv-field, .rv-cell", fieldsForm)) element.classList.toggle("active", element.dataset.path === path);
    document.body.classList.toggle("capturing", capturable(path));
    renderOverlays();
    updateCaptureHint();
    if (scroll) scrollToValue(path);
  }

  function revealField(path) {
    const id = path.split(".")[0];
    if (definitions[id]?.hidden && !prefs.showHidden) {
      prefs.showHidden = true;
      toggles.hidden.input.checked = true;
      savePrefs();
      renderSidebar();
    }
    if (prefs.collapsed[sectionOf[id]]) toggleSection(sectionOf[id], false);
  }

  function focusPath(path) {
    if (!path || !definitionFor(path)) return;
    revealField(path);
    const control = $$("[name]", fieldsForm).find((element) => element.name === path);
    if (!control) return;
    control.focus({ preventScroll: true });
    revealIn(control.closest(".rv-table-scroll"), control.closest(".rv-cell"));
    revealIn(control.closest(".rv-panel"), control.closest(".rv-field, .rv-cell") || control);
    if (control.tagName === "INPUT") control.select();
  }

  function focusFirstMessage(level) {
    const message = (state.doc.messages || []).find((item) => item.level === level && item.path);
    if (!message) return;
    if (definitions[message.path]?.type === "table") {
      revealField(message.path);
      revealIn($("#panel-fields"), fieldsForm.querySelector(`[data-table="${message.path}"]`));
    } else {
      focusPath(message.path);
    }
  }

  /* After Enter: the next value that needs a look, else the next field, else the Confirm button. */
  function moveOn(path) {
    const order = $$("[name]", fieldsForm).filter((control) => !control.disabled).map((control) => control.name);
    const index = order.indexOf(path);
    const needs = new Set((state.doc.needs_review || []).filter((item) => item !== path));
    const next = order.slice(index + 1).find((item) => needs.has(item)) || order.slice(0, Math.max(index, 0)).find((item) => needs.has(item));
    if (next) return focusPath(next);
    if (index === order.length - 1) return $(".rv-side-foot [data-action=confirm]")?.focus();
    return focusPath(order[index + 1]);
  }

  // ---------- saving ----------

  let saveChain = Promise.resolve();
  const failedPaths = new Set();
  function enqueue(task) {
    // Saves and actions run one at a time and in order, so a quick edit-then-confirm can never overtake itself.
    const run = saveChain.then(task, task);
    saveChain = run.catch(() => {});
    return run;
  }

  function setSaving(delta) {
    state.saving += delta;
    $("#rv-save-state").textContent = state.saving > 0 ? "Saving…" : state.saveFailed ? "Some changes were not saved" : "All changes saved";
  }

  function applyReviewState(result) {
    if (!result) return;
    for (const key of ["fields", "tables", "messages", "needs_review", "modified_at"]) {
      if (key in result) state.doc[key] = result[key];
    }
  }

  function saveValue(path, payload) {
    drafts.set(path, payload);
    setSaving(1);
    return enqueue(async () => {
      try {
        if (!editable()) throw new Error("This document is now read-only.");
        applyReviewState(await api(`${urls.api}/fields`, { method: "PATCH", body: { path, ...payload } }));
        if (drafts.get(path) === payload) drafts.delete(path);
        failedPaths.delete(path);
        state.saveFailed = failedPaths.size > 0;
        refresh();
        return true;
      } catch (error) {
        failedPaths.add(path);
        state.saveFailed = true;
        refresh();
        toast(`${labelFor(path)} was not saved. ${error.message}`, "error");
        return false;
      } finally {
        setSaving(-1);
      }
    });
  }

  async function acceptValue(path, control) {
    const current = stateFor(path);
    if (control.value !== (current.value || "") || !current.validated || failedPaths.has(path)) {
      if (!(await saveValue(path, { value: control.value }))) return;
    }
    moveOn(path);
  }

  async function commitFocusedValue() {
    const control = document.activeElement;
    const path = control?.dataset?.path;
    if (!path || !fieldsForm.contains(control)) return;
    if (control.value !== (stateFor(path).value || "")) await saveValue(path, { value: control.value });
  }

  async function addRow(tableId) {
    await enqueue(async () => {
      try {
        if (failedPaths.size) throw new Error("Retry unsaved fields before changing table rows.");
        applyReviewState(await api(`${urls.api}/tables/${encodeURIComponent(tableId)}/rows`, { method: "POST" }));
        replaceTable(tableId);
        refresh();
      } catch (error) {
        toast(error.message, "error");
      }
    });
    const rows = state.doc.tables[tableId] || [];
    if (rows.length) focusPath(`${tableId}.${rows.length - 1}.${definitions[tableId].columns[0].id}`);
  }

  async function deleteRow(tableId, index) {
    await enqueue(async () => {
      try {
        if (failedPaths.size) throw new Error("Retry unsaved fields before changing table rows.");
        applyReviewState(await api(`${urls.api}/tables/${encodeURIComponent(tableId)}/rows/${index}`, { method: "DELETE" }));
        if (state.activePath?.startsWith(`${tableId}.`)) state.activePath = null;
        replaceTable(tableId);
        refresh();
      } catch (error) {
        toast(error.message, "error");
      }
    });
  }

  fieldsForm.addEventListener("submit", (event) => event.preventDefault());
  fieldsForm.addEventListener("focusin", (event) => {
    const path = event.target.dataset?.path;
    if (path && event.target.matches("input, select, textarea")) activate(path, { scroll: true });
  });
  fieldsForm.addEventListener("change", (event) => {
    const control = event.target;
    const path = control.dataset?.path;
    if (!path || !control.matches("input, select, textarea")) return;
    if (control.value === (stateFor(path).value || "") && !failedPaths.has(path)) return;
    saveValue(path, { value: control.value });
  });
  fieldsForm.addEventListener("keydown", (event) => {
    const control = event.target;
    const path = control.dataset?.path;
    if (!path || event.key !== "Enter" || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (control.tagName === "TEXTAREA") return; // Enter starts a new line; Tab moves on
    event.preventDefault();
    acceptValue(path, control);
  });
  fieldsForm.addEventListener("mouseover", (event) => highlightBox(event.target.closest(".rv-field, .rv-cell")?.dataset.path || null));
  fieldsForm.addEventListener("mouseleave", () => highlightBox(null));

  // ---------- document viewer ----------

  function buildPages() {
    const pages = state.doc.pages || [];
    $("#rv-page-total").textContent = String(pages.length);
    $("#rv-page-input").max = String(Math.max(pages.length, 1));
    const fragment = document.createDocumentFragment();
    pages.forEach(([width, height], index) => {
      const number = index + 1;
      const image = h("img", { alt: `Page ${number}`, draggable: "false", decoding: "async" });
      image.dataset.src = `${urls.base}/pages/${number}.png`;
      const page = h("div", { class: "rv-page", "data-page": String(number), style: `aspect-ratio: ${width} / ${height}` },
        image,
        h("div", { class: "rv-layer" }),
        h("span", { class: "rv-page-number", "aria-hidden": "true" }, String(number)));
      pageEls.push(page);
      fragment.append(page);
    });
    pagesEl.replaceChildren(fragment);

    // Long bills have hundreds of pages: only load the images near the visible part of the document.
    const loader = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const image = entry.target.querySelector("img");
        image.addEventListener("load", () => entry.target.classList.add("loaded"), { once: true });
        image.addEventListener("error", () => entry.target.classList.add("failed"), { once: true });
        image.src = image.dataset.src;
        loader.unobserve(entry.target);
      }
    }, { root: pagesEl, rootMargin: "1500px 0px" });
    for (const page of pageEls) loader.observe(page);
    pagesEl.addEventListener("scroll", onPagesScroll, { passive: true });
  }

  let scrollQueued = false;
  function onPagesScroll() {
    if (scrollQueued) return;
    scrollQueued = true;
    requestAnimationFrame(() => {
      scrollQueued = false;
      updateCurrentPage();
    });
  }

  function updateCurrentPage() {
    if (!pageEls.length) return;
    const line = pagesEl.scrollTop + pagesEl.clientHeight * 0.3;
    let low = 0;
    let high = pageEls.length - 1;
    let found = 0;
    while (low <= high) {
      const middle = (low + high) >> 1;
      if (pageEls[middle].offsetTop <= line) {
        found = middle;
        low = middle + 1;
      } else {
        high = middle - 1;
      }
    }
    setCurrentPage(found + 1);
  }

  function setCurrentPage(number) {
    if (state.currentPage === number) return;
    state.currentPage = number;
    const input = $("#rv-page-input");
    if (document.activeElement !== input) input.value = String(number);
    const rail = $("#rv-thumbs");
    rail.querySelector(".rv-thumb.current")?.classList.remove("current");
    const thumb = rail.querySelector(`[data-page="${number}"]`);
    if (thumb) {
      thumb.classList.add("current");
      if (!rail.hidden) revealIn(rail, thumb);
    }
  }

  function goToPage(number) {
    const page = pageEls[clamp(Math.round(number) || 1, 1, pageEls.length) - 1];
    if (page) pagesEl.scrollTo({ top: page.offsetTop - 16, behavior: "smooth" });
  }

  const pageInput = $("#rv-page-input");
  pageInput.addEventListener("change", () => goToPage(Number(pageInput.value)));
  pageInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    goToPage(Number(pageInput.value));
  });

  function setZoom(value) {
    const zoom = clamp(value, ZOOM_STEPS[0], ZOOM_STEPS[ZOOM_STEPS.length - 1]);
    const anchorY = (pagesEl.scrollTop + pagesEl.clientHeight / 2) / Math.max(pagesEl.scrollHeight, 1);
    const anchorX = (pagesEl.scrollLeft + pagesEl.clientWidth / 2) / Math.max(pagesEl.scrollWidth, 1);
    state.zoom = zoom;
    pagesEl.style.setProperty("--zoom", String(zoom));
    $("#rv-zoom-value").textContent = `${Math.round(zoom * 100)}%`;
    pagesEl.scrollTop = anchorY * pagesEl.scrollHeight - pagesEl.clientHeight / 2;
    pagesEl.scrollLeft = anchorX * pagesEl.scrollWidth - pagesEl.clientWidth / 2;
  }

  function zoomStep(direction) {
    const next = direction > 0
      ? ZOOM_STEPS.find((step) => step > state.zoom + 0.001)
      : [...ZOOM_STEPS].reverse().find((step) => step < state.zoom - 0.001);
    if (next) setZoom(next);
  }

  for (const button of $$("[data-zoom]")) {
    button.addEventListener("click", () => {
      if (button.dataset.zoom === "fit") setZoom(1);
      else zoomStep(button.dataset.zoom === "in" ? 1 : -1);
    });
  }

  // Ctrl + wheel (and trackpad pinch) zooms the document instead of the whole page.
  let wheelDelta = 0;
  pagesEl.addEventListener("wheel", (event) => {
    if (!event.ctrlKey) return;
    event.preventDefault();
    wheelDelta += event.deltaY;
    if (Math.abs(wheelDelta) < 40) return;
    zoomStep(wheelDelta < 0 ? 1 : -1);
    wheelDelta = 0;
  }, { passive: false });

  function renderOverlays() {
    for (const box of $$(".rv-box", pagesEl)) box.remove();
    const needs = new Set(state.doc.needs_review || []);
    for (const [path, value] of everyState()) {
      if (!value || !value.page || !value.bbox) continue;
      const definition = definitions[path.split(".")[0]];
      if (!definition || (definition.hidden && !prefs.showHidden && path !== state.activePath)) continue;
      const page = pageEls[value.page - 1];
      const size = (state.doc.pages || [])[value.page - 1];
      if (!page || !size) continue;
      const [width, height] = size;
      const [x0, top, x1, bottom] = value.bbox;
      const pad = 1.5;
      const box = h("div", {
        class: `rv-box${path === state.activePath ? " active" : ""}${needs.has(path) ? " needs-review" : ""}`,
        "data-path": path,
        "data-label": labelFor(path),
        title: labelFor(path),
      });
      box.style.left = `${((x0 - pad) / width) * 100}%`;
      box.style.top = `${((top - pad) / height) * 100}%`;
      box.style.width = `${((x1 - x0 + 2 * pad) / width) * 100}%`;
      box.style.height = `${((bottom - top + 2 * pad) / height) * 100}%`;
      page.querySelector(".rv-layer").append(box);
    }
    markThumbs();
  }

  function highlightBox(path) {
    for (const box of $$(".rv-box.hover", pagesEl)) box.classList.remove("hover");
    if (!path) return;
    for (const box of $$(".rv-box", pagesEl)) if (box.dataset.path === path) box.classList.add("hover");
  }

  function scrollToValue(path) {
    const value = stateFor(path);
    if (!value.page || !value.bbox) return;
    const page = pageEls[value.page - 1];
    const size = (state.doc.pages || [])[value.page - 1];
    if (!page || !size) return;
    const y = page.offsetTop + (value.bbox[1] / size[1]) * page.clientHeight;
    const x = page.offsetLeft + (value.bbox[0] / size[0]) * page.clientWidth;
    const target = {};
    if (y < pagesEl.scrollTop + 48 || y > pagesEl.scrollTop + pagesEl.clientHeight - 96) target.top = Math.max(0, y - pagesEl.clientHeight * 0.3);
    if (x < pagesEl.scrollLeft + 24 || x > pagesEl.scrollLeft + pagesEl.clientWidth - 160) target.left = Math.max(0, x - 48);
    if ("top" in target || "left" in target) pagesEl.scrollTo({ ...target, behavior: "smooth" });
  }

  function buildThumbs() {
    const rail = $("#rv-thumbs");
    if (rail.childElementCount) return;
    const loader = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const image = entry.target.querySelector("img");
        image.src = image.dataset.src;
        loader.unobserve(entry.target);
      }
    }, { root: rail, rootMargin: "600px 0px" });
    (state.doc.pages || []).forEach(([width, height], index) => {
      const number = index + 1;
      const image = h("img", { alt: "", draggable: "false" });
      image.dataset.src = `${urls.base}/pages/${number}.png?size=thumb`;
      const thumb = h("button", { type: "button", class: "rv-thumb", "data-page": String(number), title: `Go to page ${number}`, onclick: () => goToPage(number) },
        h("span", { class: "rv-thumb-image", style: `aspect-ratio: ${width} / ${height}` }, image),
        h("span", { class: "rv-thumb-number", text: String(number) }));
      rail.append(thumb);
      loader.observe(thumb);
    });
    markThumbs();
  }

  function markThumbs() {
    const rail = $("#rv-thumbs");
    if (!rail.childElementCount) return;
    const pages = new Set([...everyState()].filter(([, value]) => value?.page).map(([, value]) => value.page));
    for (const thumb of rail.children) thumb.classList.toggle("has-values", pages.has(Number(thumb.dataset.page)));
  }

  function setThumbs(open) {
    prefs.thumbs = open;
    savePrefs();
    if (open) buildThumbs();
    const rail = $("#rv-thumbs");
    rail.hidden = !open;
    $("#rv-thumbs-toggle").setAttribute("aria-pressed", String(open));
    if (open) {
      const current = rail.querySelector(`[data-page="${state.currentPage || 1}"]`);
      current?.classList.add("current");
      revealIn(rail, current);
    }
  }
  $("#rv-thumbs-toggle").addEventListener("click", () => setThumbs(!prefs.thumbs));

  // ---------- capturing a value by drawing a box on the page ----------

  let drag = null;

  function updateCaptureHint() {
    const hint = $("#rv-capture-hint");
    if (!editable()) {
      hint.textContent = state.doc.status === "importing" ? "" : "Read-only";
      return;
    }
    const path = state.activePath;
    hint.replaceChildren(...(capturable(path)
      ? ["Draw a box over the value for ", h("b", { text: labelFor(path) })]
      : ["Select a field, then draw a box on the page to capture its value"]));
  }

  function cancelDrag() {
    if (!drag) return;
    drag.box?.remove();
    drag = null;
  }

  // Keep the selected field focused while drawing, and stop the browser from selecting text or dragging images.
  pagesEl.addEventListener("mousedown", (event) => {
    if (event.target.closest(".rv-page")) event.preventDefault();
  });

  pagesEl.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || !event.isPrimary) return;
    const page = event.target.closest(".rv-page");
    if (!page) return;
    const rect = page.getBoundingClientRect();
    drag = {
      page,
      rect,
      pointerId: event.pointerId,
      target: event.target,
      startX: event.clientX - rect.left,
      startY: event.clientY - rect.top,
      endX: 0,
      endY: 0,
      box: null,
    };
    page.setPointerCapture(event.pointerId);
  });

  pagesEl.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const x = clamp(event.clientX - drag.rect.left, 0, drag.rect.width);
    const y = clamp(event.clientY - drag.rect.top, 0, drag.rect.height);
    if (!drag.box) {
      if (Math.hypot(x - drag.startX, y - drag.startY) < 5) return;
      drag.box = h("div", { class: "rv-selection" });
      drag.page.querySelector(".rv-layer").append(drag.box);
    }
    drag.endX = x;
    drag.endY = y;
    Object.assign(drag.box.style, {
      left: `${Math.min(x, drag.startX)}px`,
      top: `${Math.min(y, drag.startY)}px`,
      width: `${Math.abs(x - drag.startX)}px`,
      height: `${Math.abs(y - drag.startY)}px`,
    });
  });

  pagesEl.addEventListener("pointerup", (event) => {
    if (drag && event.pointerId === drag.pointerId) finishDrag();
  });
  pagesEl.addEventListener("pointercancel", cancelDrag);

  async function finishDrag() {
    const current = drag;
    drag = null;
    if (!current.box) {
      // A click rather than a drag: clicking a highlighted value selects its field.
      const box = current.target.closest?.(".rv-box");
      if (box) focusPath(box.dataset.path);
      return;
    }
    const path = state.activePath;
    if (!capturable(path)) {
      current.box.remove();
      toast(editable() ? "Select a field first, then draw a box around its value." : "This document is read-only.", "info");
      return;
    }
    const number = Number(current.page.dataset.page);
    const [width, height] = state.doc.pages[number - 1];
    const scaleX = width / current.rect.width;
    const scaleY = height / current.rect.height;
    const bbox = [
      Math.min(current.startX, current.endX) * scaleX,
      Math.min(current.startY, current.endY) * scaleY,
      Math.max(current.startX, current.endX) * scaleX,
      Math.max(current.startY, current.endY) * scaleY,
    ];
    current.box.classList.add("busy");
    try {
      const result = await api(`${urls.api}/capture`, { method: "POST", body: { page: number, bbox } });
      if (!(result.text || "").trim()) {
        toast("There is no text in that box. Draw it around the printed value.", "info");
        return;
      }
      await saveValue(path, { value: result.text, page: number, bbox: result.bbox || bbox });
      const control = $$("[name]", fieldsForm).find((element) => element.name === path);
      if (control) setControlValue(control, stateFor(path).value);
      focusPath(path);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      current.box.remove();
    }
  }

  // ---------- sidebar tabs: fields, activity, info ----------

  const ACTIVITY = {
    uploaded: () => "Uploaded the document",
    imported: (detail) => `Extracted the data${detail?.vendor && detail.vendor !== "Unknown" ? ` with the ${detail.vendor} template` : ""}${detail?.to_check ? ` (${detail.to_check} to check)` : ""}`,
    import_failed: (detail) => `Extraction failed: ${detail}`,
    field_updated: (detail) => `Changed ${detail.label}: "${detail.from || "empty"}" → "${detail.to || "empty"}"`,
    row_added: (detail) => `Added row ${detail.row} to ${detail.table}`,
    row_deleted: (detail) => `Deleted row ${detail.row} of ${detail.table}`,
    confirmed: (detail) => (detail ? `Confirmed. ${detail}` : "Confirmed"),
    postponed: (detail) => (detail ? `Postponed: ${detail}` : "Postponed"),
    rejected: (detail) => (detail ? `Rejected: ${detail}` : "Rejected"),
    deleted: () => "Deleted",
    restored: () => "Restored",
    reopened: () => "Returned to review",
    reextracted: () => "Started extracting the data again",
    exported: (detail) => `Exported to ${detail}`,
    moved: (detail) => `Moved from ${detail.from} to ${detail.to}`,
    label_added: (detail) => `Added the label "${detail}"`,
    label_removed: (detail) => `Removed the label "${detail}"`,
  };

  async function loadActivity() {
    const list = $("#rv-activity");
    list.replaceChildren(h("li", { class: "rv-muted", text: "Loading…" }));
    try {
      const entries = await api(`${urls.api}/activity`);
      if (!entries.length) {
        list.replaceChildren(h("li", { class: "rv-muted", text: "No activity yet." }));
        return;
      }
      list.replaceChildren(...entries.map((entry) => h("li", {},
        h("p", { text: (ACTIVITY[entry.action] || (() => entry.action))(entry.detail) }),
        h("small", { text: `${entry.actor || "System"} · ${formatStamp(entry.at)}` }))));
    } catch (error) {
      list.replaceChildren(h("li", { class: "error", text: error.message }));
    }
  }

  function renderInfo() {
    const doc = state.doc;
    const by = (stamp, name) => (stamp ? `${formatStamp(stamp)}${name ? ` by ${name}` : ""}` : null);
    const rows = [
      ["File", doc.file_name],
      ["Queue", doc.queue_name],
      ["Status", `${doc.status_label}${doc.automated ? " (automated)" : ""}`],
      ["Vendor template", doc.vendor || "None"],
      ["Pages", String(doc.page_count || 0)],
      ["Text layer", doc.has_text ? "Yes" : "No, the PDF is a scan"],
      ["Uploaded", by(doc.created_at, doc.created_by)],
      ["Last change", by(doc.modified_at, doc.modified_by)],
      ["Confirmed", by(doc.confirmed_at, doc.confirmed_by)],
      ["Exported", by(doc.exported_at, doc.exported_by)],
      ["Export file", doc.export_file],
      ["Rejected", by(doc.rejected_at, doc.rejected_by)],
      ["Deleted", by(doc.deleted_at, doc.deleted_by)],
      ["Note", doc.note],
      ["Labels", (doc.labels || []).join(", ") || "None"],
      ["Time spent reviewing", formatDuration(doc.time_spent)],
    ].filter(([, value]) => value);
    $("#rv-info").replaceChildren(...rows.flatMap(([term, value]) => [h("dt", { text: term }), h("dd", { text: value })]));
  }

  const tabs = $$("[data-tab]");
  function showTab(name) {
    for (const tab of tabs) tab.setAttribute("aria-selected", String(tab.dataset.tab === name));
    for (const panel of $$("[data-panel]")) panel.hidden = panel.dataset.panel !== name;
    if (name === "activity") loadActivity();
    if (name === "info") renderInfo();
  }
  for (const tab of tabs) {
    tab.addEventListener("click", () => showTab(tab.dataset.tab));
    tab.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      const next = tabs[(tabs.indexOf(tab) + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      next.focus();
      showTab(next.dataset.tab);
    });
  }

  // ---------- document actions ----------

  const menuToggle = $(".rv-menu-toggle");
  const menuList = $(".rv-menu-list");
  function closeMenu() {
    if (menuList.hidden) return;
    menuList.hidden = true;
    menuToggle.setAttribute("aria-expanded", "false");
  }
  menuToggle.addEventListener("click", (event) => {
    event.stopPropagation();
    const open = menuList.hidden;
    menuList.hidden = !open;
    menuToggle.setAttribute("aria-expanded", String(open));
    if (open) menuList.querySelector("a, button")?.focus();
  });
  menuList.addEventListener("click", (event) => {
    if (event.target.closest("a")) closeMenu();
  });

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".rv-menu")) closeMenu();
    const button = event.target.closest("[data-action]");
    if (button) {
      event.preventDefault();
      closeMenu();
      runAction(button.dataset.action);
      return;
    }
    if (event.target.closest("[data-command=shortcuts]")) {
      closeMenu();
      showShortcuts();
    }
  });

  const LEAVING_ACTIONS = { confirm: "Confirmed", postpone: "Postponed", reject: "Rejected", delete: "Deleted" };

  async function runAction(action, preset = {}) {
    const body = { ...preset };
    if (action === "reject") {
      const note = await dialog({ title: "Reject this document?", message: "It moves to the Rejected tab. You can return it to review later.", input: "textarea", label: "Reason (optional)", confirmLabel: "Reject", danger: true });
      if (note === null) return;
      body.note = note;
    } else if (action === "postpone") {
      const note = await dialog({ title: "Postpone this document", message: "It moves to the Postponed tab until someone picks it up again.", input: "textarea", label: "Note (optional)", confirmLabel: "Postpone" });
      if (note === null) return;
      body.note = note;
    } else if (action === "delete") {
      if (!(await dialog({ title: "Delete this document?", message: "It moves to the Deleted tab, where it can be restored.", confirmLabel: "Delete", danger: true }))) return;
    } else if (action === "reextract") {
      if (!(await dialog({ title: "Extract the data again?", message: "Every value on this document is replaced, including changes made by reviewers.", confirmLabel: "Re-extract", danger: true }))) return;
    } else if (action === "move") {
      const options = boot.queues.filter((queue) => queue.id !== state.doc.queue).map((queue) => ({ value: queue.id, label: queue.name }));
      const queue = await dialog({ title: "Move to another queue", input: "select", label: "Queue", options, confirmLabel: "Move" });
      if (!queue) return;
      body.queue = queue;
    } else if (action === "add_label") {
      const label = await dialog({ title: "Add a label", input: "text", label: "Label", placeholder: "e.g. urgent", required: true, confirmLabel: "Add label" });
      if (!label) return;
      body.label = label;
    }
    if (action === "confirm") await commitFocusedValue();

    let result;
    try {
      result = await enqueue(() => {
        if (failedPaths.size) {
          focusPath(failedPaths.values().next().value);
          throw new Error("Some changes were not saved. Press Enter in each affected field to retry before continuing.");
        }
        return api(`${urls.api}/actions/${action}`, { method: "POST", body });
      });
    } catch (error) {
      const first = (error.data?.messages || []).find((message) => message.path);
      if (first) focusPath(first.path);
      toast(error.message, "error");
      return;
    }

    if (LEAVING_ACTIONS[action]) {
      flashNext(`${LEAVING_ACTIONS[action]}: ${state.doc.file_name}`);
      window.location.href = urls.next || urls.list;
      return;
    }
    if (["export", "move", "add_label", "remove_label"].includes(action)) {
      Object.assign(state.doc, result.document);
      updateHeader();
      refresh();
      if (action === "export") {
        toast("Exported to Excel.", "success", { link: { href: result.export_url, label: "Download" }, timeout: 10000 });
        window.location.href = result.export_url;
      } else if (action === "move") {
        toast(`Moved to ${state.doc.queue_name}.`, "success");
      }
      return;
    }
    window.location.reload(); // restore, reopen and re-extract change which actions are available
  }

  function showShortcuts() {
    const shortcuts = [
      ["Enter", "Accept the value and go to the next one to check"],
      ["Tab / Shift + Tab", "Next / previous field"],
      ["Ctrl + Enter (⌘ + Enter)", "Confirm the document"],
      ["Drag on the page", "Capture the text in the box into the selected field"],
      ["N", "Go to the next value to check"],
      ["+ / − / 0", "Zoom in / zoom out / fit to width"],
      ["Ctrl + mouse wheel", "Zoom the document"],
      ["[ / ]", "Previous / next document"],
      ["Esc", "Leave the field or cancel a box"],
    ];
    dialog({
      title: "Keyboard shortcuts",
      body: h("dl", { class: "rv-shortcuts" }, shortcuts.flatMap(([keys, text]) => [h("dt", {}, h("kbd", { text: keys })), h("dd", { text })])),
      confirmLabel: "Close",
      hideCancel: true,
    });
  }

  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || document.querySelector("dialog[open]")) return;
    const inControl = Boolean(event.target.closest?.("input, textarea, select"));
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      if ((state.doc.actions || []).includes("confirm")) runAction("confirm");
      return;
    }
    if (event.key === "Escape") {
      if (drag) cancelDrag();
      else if (!menuList.hidden) closeMenu();
      else if (inControl) event.target.blur();
      return;
    }
    if (inControl || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      zoomStep(1);
    } else if (event.key === "-" || event.key === "_") {
      event.preventDefault();
      zoomStep(-1);
    } else if (event.key === "0") {
      event.preventDefault();
      setZoom(1);
    } else if (event.key === "]" && urls.next) {
      window.location.href = urls.next;
    } else if (event.key === "[" && urls.prev) {
      window.location.href = urls.prev;
    } else if (event.key === "n" || event.key === "N") {
      const next = (state.doc.needs_review || [])[0];
      if (next) {
        event.preventDefault();
        focusPath(next);
      }
    } else if (event.key === "?") {
      event.preventDefault();
      showShortcuts();
    }
  });

  // ---------- resizable sidebar ----------

  function applySideWidth(width) {
    const max = Math.max(360, Math.min(820, window.innerWidth - 380));
    const clamped = Math.round(clamp(width, 340, max));
    document.documentElement.style.setProperty("--side-width", `${clamped}px`);
    return clamped;
  }

  const resizer = $("#rv-resizer");
  resizer.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    resizer.setPointerCapture(event.pointerId);
    const startX = event.clientX;
    const startWidth = $("#rv-side").getBoundingClientRect().width;
    document.body.classList.add("resizing");
    const move = (moveEvent) => applySideWidth(startWidth + moveEvent.clientX - startX);
    const stop = () => {
      resizer.removeEventListener("pointermove", move);
      resizer.removeEventListener("pointerup", stop);
      resizer.removeEventListener("pointercancel", stop);
      document.body.classList.remove("resizing");
      prefs.sideWidth = $("#rv-side").getBoundingClientRect().width;
      savePrefs();
    };
    resizer.addEventListener("pointermove", move);
    resizer.addEventListener("pointerup", stop);
    resizer.addEventListener("pointercancel", stop);
  });
  resizer.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    prefs.sideWidth = applySideWidth($("#rv-side").getBoundingClientRect().width + (event.key === "ArrowRight" ? 24 : -24));
    savePrefs();
  });

  // ---------- time spent reviewing ----------

  let visibleSince = document.visibilityState === "visible" ? Date.now() : null;
  let unsentSeconds = 0;
  function flushTime() {
    if (visibleSince) {
      unsentSeconds += (Date.now() - visibleSince) / 1000;
      visibleSince = null;
    }
    if (!editable() || unsentSeconds < 1) return;
    const seconds = Math.min(Math.round(unsentSeconds), 3600);
    unsentSeconds = 0;
    fetch(`${urls.api}/time`, {
      method: "POST",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seconds }),
    }).catch(() => {});
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushTime();
    else visibleSince = Date.now();
  });
  window.addEventListener("pagehide", flushTime);
  window.addEventListener("beforeunload", (event) => {
    const hasUnsavedInput = editable() && $$("[name]", fieldsForm).some((control) => control.value !== (stateFor(control.name).value || ""));
    if (state.saving || state.saveFailed || hasUnsavedInput) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // ---------- start ----------

  function waitForImport() {
    $("#rv-importing").hidden = false;
    const poll = async () => {
      try {
        const latest = await api(urls.api);
        if (latest.status !== "importing") {
          window.location.reload();
          return;
        }
      } catch {
        /* keep waiting */
      }
      setTimeout(poll, 1500);
    };
    setTimeout(poll, 1500);
  }

  applySideWidth(prefs.sideWidth);
  buildSummary();
  renderSidebar();
  buildPages();
  renderOverlays();
  if (prefs.thumbs) setThumbs(true);
  updateCurrentPage();
  if (state.doc.status === "importing") {
    waitForImport();
  } else {
    const first = boot.focus && definitionFor(boot.focus) ? boot.focus : (state.doc.needs_review || [])[0];
    if (first && editable()) focusPath(first);
  }
})();
