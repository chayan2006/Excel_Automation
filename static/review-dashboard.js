/* Document list: selection and bulk actions, row navigation, drag-and-drop upload, refresh while importing. */
(() => {
  "use strict";
  const { dialog } = window.ReviewUI;
  const data = JSON.parse(document.getElementById("dash-data").textContent);
  const bulkForm = document.getElementById("bulk-form");
  const bulkBar = document.getElementById("bulk-bar");
  const selectAll = document.getElementById("select-all");
  const uploadForm = document.getElementById("upload-form");
  const uploadInput = document.getElementById("upload-input");

  const boxes = () => [...bulkForm.querySelectorAll('input[name="ids"]')];
  const selected = () => boxes().filter((box) => box.checked);

  function refreshSelection() {
    const count = selected().length;
    const total = boxes().length;
    bulkBar.hidden = count === 0;
    document.getElementById("bulk-count").textContent = count;
    selectAll.checked = total > 0 && count === total;
    selectAll.indeterminate = count > 0 && count < total;
    document.body.classList.toggle("has-selection", count > 0);
    for (const box of boxes()) box.closest("tr").classList.toggle("selected", box.checked);
  }

  selectAll.addEventListener("change", () => {
    for (const box of boxes()) box.checked = selectAll.checked;
    refreshSelection();
  });
  bulkForm.addEventListener("change", (event) => {
    if (event.target.name === "ids") refreshSelection();
  });
  document.getElementById("bulk-clear").addEventListener("click", () => {
    for (const box of boxes()) box.checked = false;
    refreshSelection();
  });

  // Clicking anywhere on a row opens the document (Ctrl/Cmd-click opens a new tab).
  bulkForm.addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-href]");
    if (!row || event.target.closest("input, a, button, label")) return;
    if (event.metaKey || event.ctrlKey) window.open(row.dataset.href, "_blank");
    else window.location.href = row.dataset.href;
  });

  const queueOptions = Object.entries(data.queues)
    .filter(([id]) => id !== data.currentQueue)
    .map(([value, label]) => ({ value, label }));

  bulkBar.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-bulk]");
    if (!button) return;
    const action = button.dataset.bulk;
    const count = selected().length;
    const plural = count === 1 ? "document" : "documents";

    if (action === "move") {
      const queue = await dialog({ title: `Move ${count} ${plural}`, input: "select", label: "Queue", options: queueOptions, confirmLabel: "Move" });
      if (!queue) return;
      bulkForm.elements.target_queue.value = queue;
    } else if (action === "add_label" || action === "remove_label") {
      const adding = action === "add_label";
      const label = await dialog({
        title: adding ? `Add a label to ${count} ${plural}` : `Remove a label from ${count} ${plural}`,
        input: "text",
        label: "Label",
        placeholder: "e.g. urgent",
        suggestions: data.labels,
        required: true,
        confirmLabel: adding ? "Add label" : "Remove label",
      });
      if (!label) return;
      bulkForm.elements.label.value = label;
    } else if (action === "reject" || action === "postpone") {
      const note = await dialog({
        title: `${action === "reject" ? "Reject" : "Postpone"} ${count} ${plural}`,
        input: "textarea",
        label: action === "reject" ? "Reason (optional)" : "Note (optional)",
        confirmLabel: action === "reject" ? "Reject" : "Postpone",
        danger: action === "reject",
      });
      if (note === null) return;
      bulkForm.elements.note.value = note;
    } else if (action === "delete") {
      const confirmed = await dialog({
        title: `Delete ${count} ${plural}?`,
        message: "Deleted documents move to the Deleted tab, where they can be restored.",
        confirmLabel: "Delete",
        danger: true,
      });
      if (!confirmed) return;
    }
    bulkForm.elements.action.value = action;
    bulkForm.submit();
  });

  // Upload: picking files submits straight away; files can also be dropped anywhere on the page.
  uploadInput.addEventListener("change", () => {
    if (uploadInput.files.length) uploadForm.submit();
  });
  uploadForm.querySelector("label[for=upload-input]").addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      uploadInput.click();
    }
  });

  const overlay = document.getElementById("drop-overlay");
  const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
  let dragDepth = 0;
  window.addEventListener("dragenter", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    overlay.hidden = false;
  });
  window.addEventListener("dragover", (event) => {
    if (hasFiles(event)) event.preventDefault();
  });
  window.addEventListener("dragleave", (event) => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) overlay.hidden = true;
  });
  window.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    overlay.hidden = true;
    uploadInput.files = event.dataTransfer.files;
    uploadForm.submit();
  });

  // The reviewer name is saved as soon as it changes.
  const reviewerInput = document.getElementById("reviewer-name");
  reviewerInput.addEventListener("change", () => reviewerInput.form.submit());

  // "/" jumps to search, like most list views.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "/" || event.target.closest("input, textarea, select")) return;
    event.preventDefault();
    document.getElementById("dash-search").focus();
  });

  // Keep the list fresh while documents are being extracted, unless the user is in the middle of something.
  function refreshWhileImporting() {
    if (!document.querySelector('tr[data-status="importing"]')) return;
    setTimeout(() => {
      const busy = document.body.classList.contains("has-selection") || document.activeElement?.matches("input, select, textarea") || document.querySelector("dialog[open]");
      if (busy) refreshWhileImporting();
      else window.location.reload();
    }, 2500);
  }
  refreshWhileImporting();
  refreshSelection();
})();
