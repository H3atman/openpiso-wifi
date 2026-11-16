// core.js drives the Default Portal and any Custom Portal built on the same
// markers (docs/portal-contract.md). It reads GET /portal/api/state and sends
// each action as a POST under /portal/api/; every response is the fresh state
// plus one {kind, code, text} message. Each render does three things: sets
// html[data-portal-state], fills the data-portal value markers, and emits a
// "portal:state" event. Clicks on data-action controls are handled by one
// delegated listener, and portal.css decides what each state shows.
"use strict";
(() => {
  const root = document.documentElement;
  const RING = 2 * Math.PI * 22; // the Pause ring's circumference (r=22)
  let state = null;
  let readAt = 0; // when state arrived, for the display-only countdown
  let poll = 0, toastTimer = 0;
  let zeroAsked = false, autoConnected = false;
  let shownCode = ""; // the message code on screen, so polls don't re-toast it

  const all = (name) => document.querySelectorAll('[data-portal="' + name + '"]');
  const fill = (name, value) => all(name).forEach((element) => { element.textContent = value; });
  const pad = (n) => String(n).padStart(2, "0");
  const clock = (s) => pad(Math.floor(s / 3600)) + ":" + pad(Math.floor(s % 3600 / 60)) + ":" + pad(s % 60);
  const points = (milli) => String(milli / 1000); // thousandths to "1.1"
  const when = (unix) => new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
    .format(new Date(unix * 1000));

  // duration reads like "1 day 2 hr", "30 min" or "10 sec".
  function duration(seconds) {
    const d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600), m = Math.floor(seconds % 3600 / 60);
    const parts = [];
    if (d) parts.push(d + (d > 1 ? " days" : " day"));
    if (h) parts.push(h + " hr");
    if (m && !d) parts.push(m + " min");
    return parts.length ? parts.join(" ") : seconds + " sec";
  }

  // preview prices an open Coin Window's pesos for display: the highest rate
  // reached prices every coin. The Controller credits the real amount at close.
  function preview(rates, pesos) {
    const tier = rates.filter((rate) => rate.pesos <= pesos).pop() || rates[0];
    return tier ? Math.floor(pesos * tier.seconds / tier.pesos) : 0;
  }

  // phase names the page's overall state for CSS and portal:state listeners.
  function phase(s) {
    const tc = s.time_credit;
    if (s.rebind) return "rebind";
    if (s.coin_window) return "coin";
    if (!tc) return s.message && s.message.kind === "error" ? "error" : "new";
    if (tc.expired || (tc.usage_known && tc.remaining === 0)) return "out";
    if (tc.paused) return "paused";
    return tc.online ? "online" : "ready";
  }

  // canConnect: time is left, and only Connect (never a read) logs the Device in.
  const canConnect = (tc) => tc && !tc.paused && !tc.online && !tc.expired && tc.usage_known && tc.remaining > 0;

  function left() {
    const tc = state.time_credit;
    return tc.online ? Math.max(0, tc.remaining - Math.floor((Date.now() - readAt) / 1000)) : tc.remaining;
  }

  function status(s, current) {
    const tc = s.time_credit, cheapest = s.rates[0];
    const until = tc && " · Valid until " + when(tc.valid_until);
    switch (current) {
      case "online": return "Online" + until;
      case "ready": return "Not connected" + until;
      case "paused": return "Paused" + until;
      case "out": if (tc.expired) return tc.points_milli && s.points.length
        ? "This time's Validity ended. Insert coins or redeem Points."
        : "This time's Validity ended. Insert coins or use a Voucher.";
    }
    return cheapest ? "From ₱" + cheapest.pesos + " for " + duration(cheapest.seconds) : "Insert coins or use a Voucher";
  }

  // tick redraws the countdown each second. It only displays: reaching zero
  // asks the Router, the only meter, by reading state.
  function tick() {
    if (!state) return;
    const tc = state.time_credit, w = state.coin_window, current = root.dataset.portalState;
    if (!tc) fill("remaining", "No time yet");
    else if (current === "out") fill("remaining", "Time's up");
    else if (!tc.usage_known) fill("remaining", "—");
    else {
      fill("remaining", clock(left()));
      if (tc.online && left() === 0 && !zeroAsked) { zeroAsked = true; refresh(); }
    }
    if (w) {
      fill("coin-closes", w.status === "open"
        ? "Closes in " + Math.max(0, w.closes_at - Math.floor(Date.now() / 1000)) + "s without a coin"
        : "Counting your last coins…");
    }
  }

  // ring draws pause_limit segments and fills pauses_left of them.
  function ring(tc) {
    const gap = tc.pause_limit > 1 ? 4 : 0, segment = RING / tc.pause_limit - gap;
    all("pause-ring").forEach((svg) => {
      const [base, on] = svg.querySelectorAll("circle");
      base.setAttribute("stroke-dasharray", segment + " " + gap);
      const filled = [];
      for (let i = 0; i < tc.pauses_left; i++) filled.push(segment, gap);
      on.setAttribute("stroke-dasharray", filled.concat(0, RING).join(" "));
    });
  }

  // say shows a message as the toast: success hides after 3 s, warning after
  // 5 s, an error stays until dismissed or replaced.
  function say(m) {
    const box = all("message")[0];
    clearTimeout(toastTimer);
    box.dataset.kind = m.kind;
    box.dataset.code = m.code;
    box.setAttribute("role", m.kind === "error" ? "alert" : "status");
    const text = box.querySelectorAll('[data-portal="message-text"]');
    if (text.length) text.forEach((element) => { element.textContent = m.text; });
    else box.textContent = m.text;
    box.hidden = false;
    shownCode = m.code;
    if (m.kind !== "error") toastTimer = setTimeout(() => { box.hidden = true; }, m.kind === "success" ? 3000 : 5000);
  }

  function render(next, fromAction) {
    state = next;
    readAt = Date.now();
    zeroAsked = false;
    const tc = next.time_credit, w = next.coin_window, m = next.message, current = phase(next);
    root.dataset.portalState = current;
    root.dataset.vendos = next.vendos.length;
    root.dataset.pointsTable = next.points.length;

    fill("status", status(next, current));
    fill("valid-until", tc ? when(tc.valid_until) : "");
    fill("resume-code", (tc && tc.resume_code) || "");
    fill("points", points(tc ? tc.points_milli : 0));
    if (tc) {
      fill("pauses-left", tc.pauses_left);
      fill("pause-limit", tc.pause_limit);
      root.dataset.pausesLeft = tc.pauses_left;
      root.dataset.pauseStyle = tc.pause_limit <= 4 ? "ring" : "badge";
      ring(tc);
    }
    if (w) {
      const index = next.vendos.findIndex((vendo) => vendo.id === w.vendo_id);
      const rates = index >= 0 ? next.vendos[index].rates : next.rates;
      root.dataset.coinStatus = w.status;
      fill("coins", "₱" + w.coins);
      fill("coin-credit", w.credited_seconds ? "+" + duration(w.credited_seconds)
        : w.coins ? "About +" + duration(preview(rates, w.coins)) : "Insert a coin");
      fill("coin-vendo", next.vendos.length > 1 && index >= 0 ? "Vendo " + (index + 1) : "the Vendo");
    }

    // With several Vendos, each one's rates follow a "Vendo N" heading row.
    const several = next.vendos.length > 1;
    all("rates").forEach((table) => {
      table.textContent = "";
      (several ? next.vendos : [next]).forEach((group, index) => {
        if (several) {
          const heading = document.createElement("th");
          heading.colSpan = 2;
          heading.textContent = "Vendo " + (index + 1);
          table.insertRow().appendChild(heading);
        }
        group.rates.forEach((rate) => {
          const row = table.insertRow();
          row.insertCell().textContent = "₱" + rate.pesos;
          row.insertCell().textContent = duration(rate.seconds);
        });
      });
    });
    all("points-table").forEach((table) => {
      table.textContent = "";
      next.points.forEach((row) => {
        const line = table.insertRow();
        line.insertCell().textContent = points(row.points_milli) + " Points";
        line.insertCell().textContent = duration(row.seconds);
        const button = document.createElement("button");
        button.type = "button";
        button.className = "redeem";
        button.dataset.action = "redeem-points";
        button.dataset.pointsMilli = row.points_milli;
        button.dataset.seconds = row.seconds;
        button.textContent = "Redeem";
        line.insertCell().appendChild(button);
      });
    });
    all("vendos").forEach((list) => {
      list.textContent = "";
      next.vendos.forEach((vendo, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "choice";
        button.dataset.action = "vendo";
        button.dataset.vendoId = vendo.id;
        button.textContent = "Vendo " + (index + 1);
        const rates = document.createElement("small");
        rates.textContent = vendo.rates.map((rate) => "₱" + rate.pesos + " " + duration(rate.seconds)).join(" · ");
        button.appendChild(rates);
        list.appendChild(button);
      });
    });

    const box = all("message")[0];
    if (!m) { box.hidden = true; box.dataset.code = shownCode = ""; }
    else if (fromAction || m.code !== shownCode) say(m);
    if (fromAction && m && m.kind === "success") {
      document.querySelectorAll("dialog[open]").forEach((dialog) => {
        dialog.querySelectorAll("form").forEach((form) => form.reset());
        dialog.close();
      });
    }

    tick();
    document.dispatchEvent(new CustomEvent("portal:state", { detail: Object.assign({ state: current }, next) }));

    // Rebind and reconnecting after an Idle Pause are POSTs; try once per page
    // load, then leave the Connect button for the customer.
    if (!autoConnected && (next.rebind || canConnect(tc))) {
      autoConnected = true;
      send("connect");
      return;
    }
    clearTimeout(poll);
    poll = setTimeout(refresh, w ? 1000 : m && m.code === "device-joining" ? 3000 : 30000);
  }

  function load(path, options) {
    const fromAction = !!options;
    return fetch("/portal/api/" + path, Object.assign({ credentials: "same-origin" }, options))
      .then((response) => response.json())
      .then((next) => render(next, fromAction), () => {
        render(Object.assign({ vendos: [], rates: [], points: [] }, state, {
          message: { kind: "error", code: "offline", text: "Can't reach the Portal. Check your WiFi, then try again." },
        }), fromAction);
      });
  }

  const refresh = () => load("state");
  const send = (path, form) => load(path, { method: "POST", body: new URLSearchParams(form || {}) });

  function copyCode() {
    const code = all("resume-code")[0];
    const copied = () => say({ kind: "success", code: "copied", text: "Resume Code copied." });
    const failed = () => {
      // Plain-HTTP Portals have no Clipboard API: select the code for a manual copy.
      getSelection().selectAllChildren(code);
      if (document.execCommand("copy")) copied();
      else say({ kind: "warning", code: "copy-failed", text: "Couldn't copy. Press and hold the code to copy it." });
    };
    if (navigator.clipboard) navigator.clipboard.writeText(code.textContent).then(copied, failed);
    else failed();
  }

  const actions = {
    connect: () => send("connect"),
    pause: () => send("pause"),
    resume: () => send("resume"),
    "insert-coin": () => (state.vendos.length > 1 ? all("sheet-vendos")[0].showModal() : send("coins/open")),
    vendo: (button) => send("coins/open", { vendo_id: button.dataset.vendoId }),
    "coins-done": () => send("coins/done", { window_id: state.coin_window.id }),
    "redeem-points": (button) => send("points/redeem", { points_milli: button.dataset.pointsMilli, seconds: button.dataset.seconds }),
    "copy-code": copyCode,
    dismiss: () => { all("message")[0].hidden = true; },
  };

  // Shared with the admin preview; open-<name> and sheet-<name> are dynamic.
  window.portalMarkers = {
    values: ["remaining", "status", "valid-until", "pauses-left", "pause-limit", "pause-ring", "resume-code", "rates", "vendos",
      "points", "points-table",
      "coins", "coin-credit", "coin-closes", "coin-vendo", "message", "message-text"],
    actions: Object.keys(actions).concat("redeem", "recover"),
  };

  document.addEventListener("click", (event) => {
    if (event.target.tagName === "DIALOG") return event.target.close(); // a tap on the backdrop
    const control = event.target.closest("[data-action]");
    if (!control || control.tagName === "FORM" || !state) return;
    const name = control.dataset.action;
    if (name.startsWith("open-")) all("sheet-" + name.slice(5))[0].showModal();
    else if (actions[name]) actions[name](control);
  });
  document.addEventListener("submit", (event) => {
    const form = event.target, name = form.dataset.action;
    if (!name) return; // the × forms close their dialog natively
    event.preventDefault();
    send(name, new FormData(form));
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
  setInterval(tick, 1000);
  refresh();
})();
