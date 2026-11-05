/* Website assistant: a chat panel that sends questions to the server and shows the answers.
   Answers are built from text nodes only, so nothing in an answer is ever parsed as HTML. */
(() => {
  "use strict";

  /* Split an answer into paragraphs and lists, with **bold** and [title](link) inside a line. */
  function parseAnswer(text) {
    const blocks = [];
    let current = null; // the paragraph or list that the next line continues
    for (const rawLine of String(text).split(/\r?\n/)) {
      const line = rawLine.trim().replace(/^#{1,6}\s+/, "");
      const item = line.match(/^([-*•]|\d+[.)])\s+(.+)$/);
      if (!line) {
        current = null;
      } else if (item) {
        const ordered = /\d/.test(item[1]);
        if (current?.type !== "list" || current.ordered !== ordered) blocks.push(current = { type: "list", ordered, items: [] });
        current.items.push(parseInline(item[2]));
      } else if (current?.type === "paragraph") {
        current.lines.push(parseInline(line));
      } else {
        blocks.push(current = { type: "paragraph", lines: [parseInline(line)] });
      }
    }
    return blocks;
  }

  function parseInline(text) {
    const parts = [];
    let last = 0;
    // A link target may contain one level of brackets, as in /help(1).
    for (const match of text.matchAll(/\*\*(.+?)\*\*|\[([^\]]+)\]\(((?:[^()\s]|\([^()\s]*\))+)\)/g)) {
      if (match.index > last) parts.push({ text: text.slice(last, match.index) });
      if (match[1] !== undefined) parts.push({ text: match[1], bold: true });
      else if (isSiteLink(match[3])) parts.push({ text: match[2], href: match[3] });
      else parts.push({ text: match[2] }); // a link to another website is shown as its title only
      last = match.index + match[0].length;
    }
    if (last < text.length) parts.push({ text: text.slice(last) });
    return parts;
  }

  function isSiteLink(href) {
    return href.startsWith("/") && !href.startsWith("//") && !href.includes("\\");
  }

  function buildAnswer(blocks) {
    return blocks.map((block) => {
      if (block.type === "list") {
        const list = document.createElement(block.ordered ? "ol" : "ul");
        for (const item of block.items) {
          const entry = document.createElement("li");
          entry.append(...inlineNodes(item));
          list.append(entry);
        }
        return list;
      }
      const paragraph = document.createElement("p");
      block.lines.forEach((line, index) => {
        if (index) paragraph.append(document.createElement("br"));
        paragraph.append(...inlineNodes(line));
      });
      return paragraph;
    });
  }

  function inlineNodes(parts) {
    return parts.map((part) => {
      if (!part.href && !part.bold) return document.createTextNode(part.text);
      const element = document.createElement(part.href ? "a" : "strong");
      if (part.href) element.href = part.href;
      element.textContent = part.text;
      return element;
    });
  }

  const root = document.querySelector(".assistant");
  if (!root) return;
  const launcher = root.querySelector(".assistant-launcher");
  const panel = root.querySelector(".assistant-panel");
  const log = root.querySelector(".assistant-log");
  const intro = root.querySelector(".assistant-intro");
  const form = root.querySelector(".assistant-form");
  const input = form.querySelector("textarea");
  const send = form.querySelector(".assistant-send");
  let conversation = null; // signed by the server; carries the last few questions and answers
  let busy = false;

  function setOpen(open) {
    panel.hidden = !open;
    launcher.hidden = open;
    launcher.setAttribute("aria-expanded", String(open));
    (open ? input : launcher).focus();
  }

  function message(kind, speaker, nodes) {
    const element = document.createElement("div");
    element.className = `assistant-message ${kind}`;
    const label = document.createElement("span");
    label.className = "assistant-sr";
    label.textContent = `${speaker}: `;
    element.append(label, ...nodes);
    return element;
  }

  function show(element, replacing = null) {
    if (replacing) replacing.replaceWith(element);
    else log.append(element);
    log.scrollTop = log.scrollHeight;
    return element;
  }

  function textParagraph(text) {
    const paragraph = document.createElement("p");
    paragraph.textContent = text;
    return paragraph;
  }

  async function askQuestion(question) {
    const response = await fetch(root.dataset.askUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ question, conversation }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `The assistant could not answer (error ${response.status}).`);
    return data;
  }

  launcher.addEventListener("click", () => setOpen(true));
  root.querySelector(".assistant-close").addEventListener("click", () => setOpen(false));
  panel.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    setOpen(false);
  });
  root.querySelector(".assistant-reset").addEventListener("click", () => {
    conversation = null;
    log.replaceChildren(intro);
    input.focus();
  });

  // Enter sends; Shift+Enter starts a new line.
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    form.requestSubmit();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question || busy) return;
    busy = true;
    send.disabled = true;
    input.value = "";
    show(message("question", "You", [textParagraph(question)]));
    const pending = show(message("answer pending", "Assistant", [textParagraph("Looking through the help pages…")]));
    log.setAttribute("aria-busy", "true");
    try {
      const data = await askQuestion(question);
      conversation = data.conversation;
      show(message("answer", "Assistant", buildAnswer(parseAnswer(data.answer))), pending);
    } catch (error) {
      const text = error instanceof TypeError ? "The assistant could not be reached. Check your connection and try again." : error.message;
      show(message("error", "Assistant", [textParagraph(text)]), pending);
      if (!input.value) input.value = question; // keep the question so it can be sent again
    } finally {
      log.removeAttribute("aria-busy");
      busy = false;
      send.disabled = false;
      input.focus();
    }
  });
})();
