// Optional template behaviour; voucher.js alone renders every card.
document.querySelector("[data-print]")?.addEventListener("click", () => window.print());
document.addEventListener("voucher:rendered", (event) => {
  const price = event.detail.batch.pesos;
  const colours = { "₱0": "#6b7280", "₱5": "#2563eb", "₱10": "#15803d", "₱20": "#b45309", "₱50": "#b91c1c" };
  for (const card of document.querySelectorAll('[data-voucher="card"]')) {
    card.style.setProperty("--strip", colours[price] || "#123c1c");
  }
});
