
    // Show which files were picked, and accept files dropped on a dropzone (otherwise the browser opens them).
    document.querySelectorAll(".dropzone").forEach((zone) => {
      const input = zone.querySelector("input[type=file]");
      const hint = zone.querySelector("small");
      const original = hint.textContent;
      const show = () => {
        const names = [...input.files].map((file) => file.name);
        hint.textContent = names.length ? `${names.length} selected: ${names.join(", ")}` : original;
        zone.classList.toggle("has-files", names.length > 0);
      };
      input.addEventListener("change", show);
      zone.addEventListener("dragover", (event) => { event.preventDefault(); zone.classList.add("dragging"); });
      zone.addEventListener("dragleave", () => zone.classList.remove("dragging"));
      zone.addEventListener("drop", (event) => {
        event.preventDefault();
        zone.classList.remove("dragging");
        const files = new DataTransfer();
        for (const file of event.dataTransfer.files) {
          files.items.add(file);
          if (!input.multiple) break;
        }
        input.files = files.files;
        show();
      });
    });
    // Forms marked data-async are sent by their own script and never leave the page, so they are not locked here.
    document.querySelectorAll("form:not([data-async])").forEach((form) => {
      form.addEventListener("submit", (event) => {
        if (form.dataset.busy) { event.preventDefault(); return; }
        form.dataset.busy = "true";
        const button = form.querySelector("button[type=submit]");
        button.disabled = true;
        button.textContent = form.dataset.busyLabel || (form.action.endsWith("/run") ? "Processing workbooks…" : form.action.endsWith("/extract-invoices") ? "Extracting invoices…" : "Uploading…");
        form.setAttribute("aria-busy", "true");
      });
    });
    window.addEventListener("pageshow", (event) => { if (event.persisted) window.location.reload(); });
    // Dropping a file anywhere else would replace the page with the file.
    window.addEventListener("dragover", (event) => event.preventDefault());
    window.addEventListener("drop", (event) => event.preventDefault());

    const queueSearch = document.getElementById("queue-search");
    queueSearch?.addEventListener("input", () => {
      const query = queueSearch.value.trim().toLowerCase();
      let visible = 0;
      document.querySelectorAll(".queue-item").forEach((item) => {
        item.hidden = !item.dataset.filename.toLowerCase().includes(query);
        if (!item.hidden) visible++;
      });
      document.getElementById("queue-search-status").textContent = `${visible} matching file${visible === 1 ? "" : "s"}`;
    });
