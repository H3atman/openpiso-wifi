// The Portal preview's harness. The Controller inlines it just before core.js
// in a draft rendered for the admin panel's sandboxed iframe (portal_preview.go).
// It answers every fetch with the sample state in data-state, then reports to
// the admin page: whether core.js rendered that state, which markers the page
// has, and every JS error.
"use strict";
(() => {
  const script = document.currentScript;
  const sample = script.dataset.state, expected = script.dataset.name;
  const errors = [];
  let read = false;

  // Resolved on a later task, like a network response: custom.js is listening by then.
  window.fetch = () => new Promise((resolve) => setTimeout(() => resolve(new Response(sample, {
    headers: { "Content-Type": "application/json" },
  }))));
  addEventListener("error", (event) => errors.push(event.error ? String(event.error) : event.message));
  addEventListener("unhandledrejection", (event) => errors.push("Unhandled rejection: " + event.reason));
  document.addEventListener("portal:state", (event) => { read = read || event.detail.state === expected; });

  function report() {
    // core.js loads after this harness; read its catalogue only when reporting.
    const { values, actions } = window.portalMarkers || { values: [], actions: [] };
    const names = (attribute) => [...new Set([...document.querySelectorAll("[" + attribute + "]")]
      .map((element) => element.getAttribute(attribute)))].sort();
    const portal = names("data-portal"), action = names("data-action");
    const known = (name) => values.includes(name) || name.startsWith("sheet-");
    const knownAction = (name) => actions.includes(name) || (name.startsWith("open-") && portal.includes("sheet-" + name.slice(5)));
    parent.postMessage({
      portalPreview: {
        state: expected,
        read,
        bound: portal.filter(known).concat(action.filter(knownAction)),
        unknown: portal.filter((name) => !known(name)).concat(action.filter((name) => !knownAction(name))),
        errors,
      },
    }, "*");
  }
  // Long enough for the first read, its render and any portal:state listeners.
  addEventListener("load", () => setTimeout(report, 500));
})();
