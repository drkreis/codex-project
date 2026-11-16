(() => {
  "use strict";

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  const extra = document.getElementById("extra-params");
  const add = document.getElementById("add-param");
  function bindRemove(row) {
    const button = row.querySelector(".remove-param");
    button.hidden = false;
    button.addEventListener("click", () => row.remove());
  }
  if (extra && add) {
    const template = extra.querySelector(".extra-param-row").cloneNode(true);
    extra.querySelectorAll(".extra-param-row").forEach(bindRemove);
    add.hidden = false;
    add.addEventListener("click", () => {
      if (extra.children.length >= 50) return;
      const row = template.cloneNode(true);
      row.querySelectorAll("input").forEach((input) => { input.value = ""; });
      bindRemove(row);
      extra.append(row);
      row.querySelector("input").focus();
    });
  }

  document.getElementById("print-comparison")?.addEventListener("click", () => window.print());

  const storageKey = "codex.selection";
  if (document.querySelector("[data-clear-selection]")) {
    try { sessionStorage.removeItem(storageKey); } catch { /* Storage is optional. */ }
  }
  const form = document.getElementById("selection-form");
  if (!form) return;

  const checkboxes = [...form.querySelectorAll(".record-select")];
  const count = document.getElementById("selection-count");
  const clear = document.getElementById("clear-selection");
  const all = document.getElementById("select-visible");
  const hidden = document.getElementById("selection-hidden");
  let chosen = new Set(checkboxes.filter((box) => box.checked).map((box) => box.value));
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
    if (Array.isArray(saved)) {
      saved.filter((id) => /^\d+$/.test(String(id))).forEach((id) => chosen.add(String(id)));
    }
  } catch { /* The ordinary checkbox form works without browser storage. */ }

  function update() {
    checkboxes.forEach((box) => { box.checked = chosen.has(box.value); });
    count.textContent = chosen.size ? " · выбрано: " + chosen.size : " · выберите изделия";
    clear.hidden = chosen.size === 0;
    all.checked = checkboxes.length > 0 && checkboxes.every((box) => box.checked);
    all.indeterminate = checkboxes.some((box) => box.checked) && !all.checked;
    hidden.replaceChildren();
    const visible = new Set(checkboxes.map((box) => box.value));
    chosen.forEach((id) => {
      if (!visible.has(id)) {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "record_ids";
        input.value = id;
        hidden.append(input);
      }
    });
    try { sessionStorage.setItem(storageKey, JSON.stringify([...chosen])); } catch { /* Optional. */ }
  }
  checkboxes.forEach((box) => box.addEventListener("change", () => {
    if (box.checked) chosen.add(box.value); else chosen.delete(box.value);
    update();
  }));
  all.addEventListener("change", () => {
    checkboxes.forEach((box) => {
      if (all.checked) chosen.add(box.value); else chosen.delete(box.value);
    });
    update();
  });
  clear.addEventListener("click", () => { chosen.clear(); update(); });
  update();
})();
