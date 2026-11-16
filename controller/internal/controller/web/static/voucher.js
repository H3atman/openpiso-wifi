// Controller-owned marker contract. Keep this independent of custom.js so a
// broken Operator script cannot prevent the Print sheet from rendering.
"use strict";
(() => {
  function render() {
    const data = JSON.parse(document.querySelector('script[data-voucher-batch]').textContent);
    const template = document.querySelector('[data-voucher="card"]');
    const anchor = document.createComment("Voucher cards");
    template.replaceWith(anchor);
    for (const voucher of data.vouchers) {
      const card = template.cloneNode(true);
      const values = { ...data.batch, ...voucher };
      for (const element of card.querySelectorAll("[data-voucher]")) {
        const name = element.dataset.voucher;
        if (!Object.hasOwn(values, name)) continue;
        if (name === "qr") element.innerHTML = values.qr;
        else element.textContent = values[name];
      }
      delete card.dataset.used;
      if (voucher.used) card.dataset.used = "";
      anchor.before(card);
    }
    document.dispatchEvent(new CustomEvent("voucher:rendered", { detail: data }));
  }
  // custom.js gets to subscribe first; a throw in that separate script doesn't
  // cancel this listener or the browser's DOMContentLoaded event.
  document.addEventListener("DOMContentLoaded", render, { once: true });
})();
