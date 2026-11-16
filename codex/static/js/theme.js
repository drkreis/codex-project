/* Apply the theme before styles load to avoid a flash during navigation. */
(() => {
  "use strict";

  const storageKey = "codex.theme";
  const root = document.documentElement;
  const colorScheme = document.querySelector('meta[name="color-scheme"]');
  const systemTheme = window.matchMedia("(prefers-color-scheme: dark)");
  const isTheme = (value) => value === "light" || value === "dark";
  let preference = null;

  try {
    const stored = window.localStorage.getItem(storageKey);
    if (isTheme(stored)) preference = stored;
  } catch {
    // Switching still works when the browser blocks local storage.
  }

  function applyTheme() {
    const theme = preference || (systemTheme.matches ? "dark" : "light");
    root.dataset.theme = theme;
    colorScheme.content = theme;

    const toggle = document.getElementById("theme-toggle");
    if (toggle) toggle.setAttribute("aria-checked", String(theme === "dark"));
  }

  applyTheme();

  document.addEventListener("DOMContentLoaded", () => {
    const toggle = document.getElementById("theme-toggle");
    if (!toggle) return;

    applyTheme();
    toggle.hidden = false;
    toggle.addEventListener("click", () => {
      preference = root.dataset.theme === "dark" ? "light" : "dark";
      applyTheme();
      try {
        window.localStorage.setItem(storageKey, preference);
      } catch {
        // Keep the selected theme for this page even if it cannot be saved.
      }
    });
  }, { once: true });

  systemTheme.addEventListener("change", () => {
    if (preference === null) applyTheme();
  });

  window.addEventListener("storage", (event) => {
    if (event.key !== storageKey && event.key !== null) return;
    preference = isTheme(event.newValue) ? event.newValue : null;
    applyTheme();
  });
})();
