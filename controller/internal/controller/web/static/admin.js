// Admin UI behaviour: Alpine.data() components plus a few htmx/DOM hooks.
// The Alpine CSP build forbids arrow functions, template literals and assignments
// in attributes, so every non-trivial expression lives here as a component method.
// Loaded synchronously in <head> so the dark class is set before first paint and
// 'alpine:init' is registered before the deferred Alpine script starts. Nothing
// here is inside a swap target, so boosted navigation never re-runs it.

const THEME_KEY = 'openpiso.theme';
const SIDEBAR_KEY = 'openpiso.sidebar';

// Sales Inventory state that outlives a #sales-report swap; see salesFilters.
const salesUI = { panelOpen: false, sheetOpen: false, focus: '' };
document.addEventListener('toggle', (e) => {
  if (e.target instanceof Element && e.target.matches('.sales-filter-panel')) salesUI.panelOpen = e.target.open;
}, true);
document.addEventListener('focusin', (e) => {
  salesUI.focus = (e.target instanceof HTMLElement && e.target.closest('#sales-report') && e.target.dataset.focus) || '';
});

const savedTheme = localStorage.getItem(THEME_KEY);
document.documentElement.classList.toggle(
  'dark',
  savedTheme ? savedTheme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches,
);

// Dropdown behaviour shared by row menus and the user menu. Menus are
// position:fixed and placed from the trigger rect so scroll containers (tables,
// the sidebar) never clip them. Markup: x-ref="trigger", x-ref="menu",
// data-side="top|bottom" on the x-data element.
function dropdownBase() {
  return {
    open: false,
    toggle() { this.open ? this.close() : this.show(); },
    show() {
      this.open = true;
      this.$nextTick(() => this.place());
    },
    close() { this.open = false; },
    place() {
      const t = this.$refs.trigger.getBoundingClientRect();
      const m = this.$refs.menu;
      const side = this.$root.dataset.side || 'bottom';
      if (side === 'top') m.style.minWidth = t.width + 'px';
      const w = m.offsetWidth;
      const h = m.offsetHeight;
      const left = side === 'top' ? t.left : t.right - w;
      const top = side === 'top' ? t.top - h - 4 : t.bottom + 4;
      m.style.left = Math.max(8, Math.min(left, innerWidth - w - 8)) + 'px';
      m.style.top = Math.max(8, Math.min(top, innerHeight - h - 8)) + 'px';
    },
  };
}

document.addEventListener('alpine:init', () => {
  Alpine.store('theme', {
    dark: document.documentElement.classList.contains('dark'),
    get label() { return this.dark ? 'Light mode' : 'Dark mode'; },
    toggle() {
      this.dark = !this.dark;
      document.documentElement.classList.toggle('dark', this.dark);
      localStorage.setItem(THEME_KEY, this.dark ? 'dark' : 'light');
    },
  });

  Alpine.data('dropdown', dropdownBase);

  Alpine.data('voucherBatches', () => ({
    custom: true,
    sample: '',
    codeSpace: '',
    get tierSelected() { return !this.custom; },
    init() {
      this.choose();
      this.format();
      if (this.$refs.generate.dataset.reopen === 'true') this.show();
    },
    choose() { this.custom = this.$refs.tier.value === 'custom'; },
    format() {
      const option = this.$refs.preset.selectedOptions[0];
      const alphabet = option.dataset.alphabet;
      const length = Number(this.$refs.length.value);
      if (!alphabet || !Number.isInteger(length) || length < 4 || length > 10) {
        this.sample = '—';
        this.codeSpace = '—';
        return;
      }
      this.sample = this.$refs.prefix.value;
      for (let i = 0; i < length; i++) this.sample += alphabet[(i * 17 + 5) % alphabet.length];
      const size = Number(option.dataset.size);
      this.codeSpace = (BigInt(size) ** BigInt(length)).toLocaleString();
    },
    show() { this.$refs.generate.showModal(); },
    close() { this.$refs.generate.close(); },
    remove(event) {
      const form = event.currentTarget;
      if (window.confirm(form.dataset.confirm)) form.submit();
    },
    print(event) {
      if (event.target.closest('a, button')) return;
      const link = event.currentTarget.querySelector('a');
      window.open(link.href, '_blank', 'noopener');
    },
  }));

  // Each filter change re-renders #sales-report. salesUI carries what the
  // replaced report had open (the table's filter panel, the phone filter sheet)
  // and the focused control's data-focus, so the new report continues where the user was.
  // Not ids: htmx settles id-matched elements with their old attributes, which leaves a
  // focused input showing its previous value.
  Alpine.data('salesFilters', () => ({
    media: null,
    onResize: null,
    init() {
      const focus = salesUI.focus;
      const panel = this.$root.querySelector('.sales-filter-panel');
      if (panel) panel.open = salesUI.panelOpen;
      if (this.$refs.filters) {
        this.media = matchMedia(panel ? '(min-width: 1180px)' : '(min-width: 1000px)');
        const form = this.$root.querySelector('.sales-filters');
        this.onResize = () => {
          const focused = document.activeElement;
          const filterFocused = form.contains(focused);
          const sheet = this.$refs.filters;
          if (sheet.open) sheet.close();
          // Reuse the form without detaching controls already in the right place.
          if (panel) {
            const container = this.media.matches ? panel : sheet;
            if (form.parentElement !== container) container.append(form);
            if (this.media.matches && filterFocused) panel.open = true;
          }
          if (this.media.matches && !panel) {
            // A desktop pane is non-modal; displaying it must not run dialog autofocus.
            sheet.setAttribute('open', '');
          } else if (!this.media.matches && (salesUI.sheetOpen || filterFocused)) {
            salesUI.sheetOpen = true;
            sheet.showModal();
          }
          if (filterFocused) focused.focus({ preventScroll: true });
        };
        this.$refs.filters.addEventListener('cancel', () => { salesUI.sheetOpen = false; });
        this.media.addEventListener('change', this.onResize);
        this.onResize();
      }
      // show()/showModal() move focus into the dialog; put it back on the control in use.
      const focused = focus && this.$root.querySelector(`[data-focus="${focus}"]`);
      if (focused) focused.focus();
    },
    destroy() {
      if (this.media) this.media.removeEventListener('change', this.onResize);
    },
    show() { salesUI.sheetOpen = true; this.$refs.filters.showModal(); this.$refs.filters.querySelector('select').focus(); },
    close() { salesUI.sheetOpen = false; this.$refs.filters.close(); },
  }));

  // WAI-ARIA Tabs (automatic activation). Markup: role="tablist" with the
  // arrow, Home and End keydown bindings; each role="tab" names its panel in
  // data-tab and binds :aria-selected="selected('x')" and :tabindex="tabIndex('x')";
  // each role="tabpanel" binds x-show="shows('x')". The first tab starts selected.
  Alpine.data('tabs', () => ({
    tab: '',
    init() { this.tab = this.tabs()[0].dataset.tab; },
    tabs() { return [...this.$root.querySelectorAll('[role=tab]')]; },
    selected(name) { return String(this.tab === name); },
    tabIndex(name) { return this.tab === name ? 0 : -1; },
    shows(name) { return this.tab === name; },
    select(e) { this.tab = e.currentTarget.dataset.tab; },
    current() { return this.tabs().findIndex((tab) => tab.dataset.tab === this.tab); },
    // Select and focus the tab at index, wrapping at either end.
    go(index) {
      const tabs = this.tabs();
      const tab = tabs[(index + tabs.length) % tabs.length];
      this.tab = tab.dataset.tab;
      tab.focus();
    },
    next() { this.go(this.current() + 1); },
    previous() { this.go(this.current() - 1); },
    first() { this.go(0); },
    last() { this.go(-1); },
  }));

  // Collapsible sidebar group: x-data="collapsible(true|false)".
  Alpine.data('collapsible', (open) => ({
    open,
    get state() { return this.open ? 'open' : 'closed'; },
    toggle() { this.open = !this.open; },
  }));

  // The updating page (update.html): polls /admin/update/state while the root helper installs
  // and restarts the Controller, then opens the Dashboard, which shows the outcome. A failed
  // fetch means the Controller is down for the restart.
  Alpine.data('updateWait', () => ({
    message: '',
    timer: 0,
    init() {
      this.message = this.$refs.message.textContent;
      const started = Date.now();
      this.timer = setInterval(async () => {
        if (Date.now() - started > 10 * 60 * 1000) {
          clearInterval(this.timer);
          this.message = 'The update is taking longer than expected. Open the Dashboard to see its status.';
          return;
        }
        try {
          const response = await fetch('/admin/update/state', { headers: { Accept: 'application/json' } });
          if (!response.ok) throw new Error(`status ${response.status}`);
          if (!(await response.json()).pending) location.assign('/admin/dashboard');
        } catch {
          this.message = 'The Controller is restarting. This page opens the Dashboard when it is back.';
        }
      }, 2000);
    },
    destroy() { clearInterval(this.timer); },
  }));

  // Sidebar open/collapse (persisted) and the mobile off-canvas sheet.
  Alpine.data('shell', () => ({
    collapsed: false,
    mobile: false,
    init() {
      this.collapsed = localStorage.getItem(SIDEBAR_KEY) === '1';
      // Close the sheet after picking a destination (not on every htmx swap: polls swap too).
      this.$root.querySelector('#sidebar').addEventListener('click', (e) => {
        if (e.target.closest('a[data-nav]')) this.mobile = false;
      });
    },
    get state() { return this.collapsed ? 'collapsed' : 'expanded'; },
    toggle() {
      if (matchMedia('(max-width: 767px)').matches) {
        this.mobile = !this.mobile;
        return;
      }
      this.collapsed = !this.collapsed;
      localStorage.setItem(SIDEBAR_KEY, this.collapsed ? '1' : '0');
    },
    closeMobile() { this.mobile = false; },
  }));

  // Ctrl/Cmd+K command palette. Static entries arrive in data-items; on open it
  // also fetches data-src, a JSON array of {label, hint, href}, and appends them.
  // Key bindings are declared in the markup so Alpine removes them with the element.
  Alpine.data('palette', () => ({
    open: false,
    query: '',
    cursor: 0,
    base: [],
    extra: [],
    init() {
      this.base = JSON.parse(this.$root.dataset.items);
      this.$watch('query', () => { this.cursor = 0; });
    },
    get items() {
      return this.base.concat(this.extra).map((item, i) => ({ ...item, key: i }));
    },
    get results() {
      const q = this.query.trim().toLowerCase();
      const items = this.items;
      return q ? items.filter((i) => (i.label + ' ' + i.hint).toLowerCase().includes(q)) : items;
    },
    get isEmpty() { return this.results.length === 0; },
    toggle() { this.open ? this.hide() : this.show(); },
    show() {
      this.open = true;
      this.query = '';
      this.cursor = 0;
      this.$nextTick(() => this.$refs.input.focus());
      // Dynamic entries go stale, so refetch on every open.
      fetch(this.$root.dataset.src)
        .then((r) => r.json())
        .then((items) => { this.extra = items; })
        .catch(() => {});
    },
    hide() { this.open = false; },
    move(delta) {
      const n = this.results.length;
      if (n) this.cursor = (this.cursor + delta + n) % n;
    },
    isActive(i) { return i === this.cursor; },
    hover(i) { this.cursor = i; },
    choose() {
      const item = this.results[this.cursor];
      if (item) location.assign(item.href);
    },
  }));
});

// ---- Portal cover and logo ----

// The browser does all the image work, so the SBC never decodes an image: it
// crops to the slot's size around a focus point (0 to 1 along the axis that
// overflows) and encodes WebP, lowering quality until it fits the cap.
const PORTAL_IMAGES = {
  logo: { width: 256, height: 256, cap: 20 * 1024 },
  cover: { width: 1200, height: 400, cap: 100 * 1024 },
};

async function portalImage(file, slot, focus) {
  const { width, height, cap } = PORTAL_IMAGES[slot];
  const bitmap = await createImageBitmap(file).catch(() => {
    throw new Error("Can't read that image. Use a JPG, PNG or WebP.");
  });
  const scale = Math.max(width / bitmap.width, height / bitmap.height);
  const sw = width / scale;
  const sh = height / scale;
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  canvas.getContext('2d').drawImage(bitmap, (bitmap.width - sw) * focus, (bitmap.height - sh) * focus, sw, sh, 0, 0, width, height);
  bitmap.close();
  let blob;
  for (let quality = 0.9; quality > 0.2; quality -= 0.1) {
    blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/webp', quality));
    if (blob?.type !== 'image/webp') throw new Error("This browser can't make WebP images. Use Chrome or Firefox.");
    if (blob.size <= cap) break;
  }
  return blob; // still over the cap after the lowest quality: the Controller refuses it
}

// Markup: data-portal-image="logo|cover" holding a file input, an optional
// range input (the focus, 0-100), a button and an <output> for errors.
document.addEventListener('click', async (e) => {
  const button = e.target instanceof Element && e.target.closest('[data-portal-image] button');
  if (!button) return;
  const box = button.closest('[data-portal-image]');
  const slot = box.dataset.portalImage;
  const file = box.querySelector('input[type=file]').files[0];
  const focus = box.querySelector('input[type=range]');
  const status = box.querySelector('output');
  if (!file) {
    status.textContent = 'Choose an image first.';
    return;
  }
  button.disabled = true;
  status.textContent = 'Uploading…';
  try {
    const body = await portalImage(file, slot, focus ? focus.value / 100 : 0.5);
    const response = await fetch('/admin/portal/images/' + slot, { method: 'POST', headers: { 'Content-Type': 'image/webp' }, body });
    if (!response.ok) throw new Error(await response.text());
    location.reload();
  } catch (error) {
    status.textContent = error.message;
    button.disabled = false;
  }
});

// ---- DOM and htmx hooks (htmx 4 event names) ----

// Rows with data-href open that page; links, buttons, menus and form controls keep their own clicks.
document.addEventListener('click', (e) => {
  if (!(e.target instanceof Element) || e.defaultPrevented) return;
  const row = e.target.closest('[data-href]');
  if (row && !e.target.closest('a, button, input, select, textarea, label, .menu')) location.assign(row.dataset.href);
});

// A polled region (data-poll) is not swapped while one of its menus is open.
document.addEventListener('htmx:before:request', (e) => {
  const el = e.target;
  if (el instanceof Element && el.hasAttribute('data-poll') && el.querySelector('[aria-expanded="true"]')) e.preventDefault();
});

// A boosted swap replaces #view but not the sidebar, so mirror the active page onto it.
document.addEventListener('htmx:after:settle', () => {
  const key = document.querySelector('#view [data-nav-key]')?.dataset.navKey;
  if (key === undefined) return;
  const links = [...document.querySelectorAll('#sidebar [data-nav]')];
  const exact = links.filter((a) => a.dataset.nav === key);
  // A sub-item that shares its parent's href takes the highlight, as on the server.
  const subs = exact.filter((a) => a.classList.contains('menu-sub-btn'));
  const winners = subs.length ? subs : exact;
  links.forEach((a) => a.toggleAttribute('data-active', winners.includes(a)));
});

// Portal preview (/admin/portal): the picker loads one sample state into the
// sandboxed iframe, and "Check every state" steps through all of them. The
// iframe's harness (portal_preview.js) posts one report per load, which fills
// that state's row.
let previewQueue = [];

function previewState(state) {
  const frame = document.querySelector('[data-preview] iframe');
  frame.src = '/admin/portal/preview?state=' + encodeURIComponent(state);
  document.querySelectorAll('[data-preview-state]').forEach((button) => {
    button.setAttribute('aria-pressed', String(button.dataset.previewState === state));
  });
}

document.addEventListener('click', (e) => {
  if (!(e.target instanceof Element)) return;
  const pick = e.target.closest('[data-preview-state]');
  if (pick) {
    previewQueue = [];
    previewState(pick.dataset.previewState);
  } else if (e.target.closest('[data-preview-all]')) {
    previewQueue = [...document.querySelectorAll('[data-preview-state]')].map((button) => button.dataset.previewState);
    previewState(previewQueue.shift());
  }
});

window.addEventListener('message', (e) => {
  const frame = document.querySelector('[data-preview] iframe');
  const report = e.data && e.data.portalPreview;
  if (!frame || e.source !== frame.contentWindow || !report) return;
  const row = document.querySelector('[data-preview] tr[data-state="' + CSS.escape(report.state) + '"]');
  if (!row) return;
  row.dataset.read = report.read ? 'ok' : 'failed';
  row.querySelector('[data-field=read]').textContent = report.read ? 'OK' : 'Failed: core.js did not show this state';
  for (const field of ['bound', 'unknown', 'errors']) {
    row.querySelector('[data-field=' + field + ']').replaceChildren(...report[field].map((text) => {
      const item = document.createElement('li');
      item.textContent = String(text);
      return item;
    }));
  }
  if (previewQueue.length) previewState(previewQueue.shift());
});

// The login page is standalone; no inline handlers are allowed by the admin CSP.
document.addEventListener('DOMContentLoaded', () => {
  const toggle = document.querySelector('[data-password-toggle]');
  if (!toggle) return;
  const input = document.getElementById('password');
  toggle.addEventListener('click', () => {
    const showing = input.type === 'password';
    input.type = showing ? 'text' : 'password';
    toggle.textContent = showing ? 'Hide' : 'Show';
    toggle.setAttribute('aria-pressed', String(showing));
  });
});

const tierUnits = { weeks: 604800, days: 86400, hours: 3600, minutes: 60 };

// duration formats minutes as days, or h / min / s parts.
function duration(minutes) {
  const seconds = Math.round(minutes * 60);
  if (seconds && seconds % 86400 === 0) return seconds / 86400 + (seconds === 86400 ? ' day' : ' days');
  return [Math.floor(seconds / 3600) ? Math.floor(seconds / 3600) + ' h' : '', Math.floor(seconds % 3600 / 60) ? Math.floor(seconds % 3600 / 60) + ' min' : '', seconds % 60 ? seconds % 60 + ' s' : ''].filter(Boolean).join(' ') || '0 min';
}

// tierEditor builds and checks a tier editor form (Rate tiers or Points rows),
// shared by the Vendo dialog's tabs and the Shop pages; each owner posts it.
const tierEditor = {
  prepare(root) {
    root.querySelectorAll('[data-tier-seed]').forEach((seed) => seed.replaceWith(this.row(seed.closest('form').dataset.tiers, seed.dataset)));
    root.querySelectorAll('[data-tiers]').forEach((form) => {
      const cols = document.createElement('div'); cols.className = 'tier-cols'; cols.setAttribute('aria-hidden', 'true');
      const grid = document.createElement('div'); grid.className = 'tier-grid';
      const headings = form.dataset.tiers === 'rates' ? ['Price', 'Time', 'Points earned', 'Validity', 'Pause Limit'] : ['Points cost', 'Time', 'Validity', 'Pause Limit'];
      for (const name of headings) { const label = document.createElement('span'); label.textContent = name; grid.append(label); }
      cols.append(grid); form.querySelector('[data-tier-list]').before(cols);
      this.refresh(form);
    });
  },
  refresh(form) {
    const rows = [...form.querySelectorAll('.tier')];
    const noun = form.dataset.tiers === 'rates' ? 'Tier' : 'Row';
    rows.forEach((row, index) => {
      row.querySelector('.tier-title').textContent = noun + ' ' + (index + 1);
      row.setAttribute('role', 'group'); row.setAttribute('aria-label', noun + ' ' + (index + 1));
      const remove = row.querySelector('[data-tier-delete]');
      remove.setAttribute('aria-label', 'Remove ' + noun.toLowerCase() + ' ' + (index + 1)); remove.disabled = noun === 'Tier' && rows.length === 1;
    });
    form.querySelector('.tier-none')?.remove();
    if (!rows.length) { const empty = document.createElement('p'); empty.className = 'tier-none'; empty.textContent = 'No rows: customers can’t redeem Points here.'; form.querySelector('[data-tier-list]').append(empty); }
  },
  row(kind, seed = {}) {
    const el = document.createElement('div'); el.className = 'tier';
    const title = document.createElement('span'); title.className = 'tier-title'; title.textContent = kind === 'rates' ? 'Tier' : 'Row';
    const sum = document.createElement('span'); sum.className = 'tier-sum trunc';
    const del = document.createElement('button'); del.type = 'button'; del.className = 'btn ghost icon sm tier-del'; del.dataset.tierDelete = ''; del.title = 'Remove';
    const glyph = document.createElement('span'); glyph.textContent = '×'; glyph.setAttribute('aria-hidden', 'true'); del.append(glyph);
    const grid = document.createElement('div'); grid.className = 'tier-grid';
    let validity = seed.validity || '86400', unit = 'days';
    if (validity === 'no limit') { validity = ''; unit = 'none'; }
    else {
      const seconds = Number(validity); validity = seconds / 60; unit = 'minutes';
      for (const [u, factor] of Object.entries(tierUnits)) {
        if (seconds % factor === 0) { validity = seconds / factor; unit = u; break; }
      }
    }
    const fields = [[kind === 'rates' ? 'price' : 'cost', kind === 'rates' ? 'Price' : 'Points cost', seed.cost || '', kind === 'rates' ? 1 : 0.001], ['minutes', 'Time', seed.seconds ? Number(seed.seconds) / 60 : '', 1 / 60]];
    if (kind === 'rates') fields.push(['points', 'Points earned', seed.points || '0', 0]);
    fields.push(['validity', 'Validity', String(validity), 1], ['pause', 'Pause Limit', seed.pause || '0', 0]);
    for (const [name, label, value, min] of fields) {
      const field = document.createElement('label'); field.className = 'field f-' + (name === 'pause' ? 'pauses' : name) + ' tier-span-' + (kind === 'rates' && !['validity', 'pause'].includes(name) ? 'two' : 'three');
      const caption = document.createElement('span'); caption.className = 'field-label'; caption.textContent = label;
      const group = document.createElement('span'); group.className = 'input-group';
      const input = document.createElement('input'); input.name = 'tier_' + name; input.value = value; input.type = 'number'; input.min = String(name === 'validity' ? 0.000001 : min); input.step = ['cost', 'points'].includes(name) ? '0.001' : ['minutes', 'validity'].includes(name) ? 'any' : '1'; input.required = true; input.inputMode = 'decimal'; input.autocomplete = 'off';
      if (name === 'price') { const prefix = document.createElement('span'); prefix.className = 'adorn'; prefix.textContent = '₱'; group.append(prefix); }
      group.append(input);
      const suffix = { cost: 'pts', points: 'pts', minutes: 'min', pause: 'pauses' }[name];
      if (suffix) { const adorn = document.createElement('span'); adorn.className = 'adorn'; adorn.textContent = suffix; group.append(adorn); }
      if (name === 'validity') {
        const select = document.createElement('select'); select.name = 'tier_unit'; select.setAttribute('aria-label', 'Validity unit');
        for (const [value, text] of [['minutes', 'minutes'], ['hours', 'hours'], ['days', 'days'], ['weeks', 'weeks'], ['none', 'No limit']]) {
          const option = document.createElement('option'); option.value = value; option.textContent = text; select.append(option);
        }
        select.value = unit; group.append(select); input.disabled = unit === 'none';
      }
      field.append(caption, group); grid.append(field);
    }
    el.append(title, sum, del, grid); this.summary(el); return el;
  },
  summary(row) {
    const get = (name) => row.querySelector('[name=tier_' + name + ']');
    const cost = get('price') || get('cost');
    const validity = get('validity'); const unit = get('unit');
    validity.disabled = unit.value === 'none';
    for (const option of unit.options) {
      if (option.value !== 'none') option.textContent = Number(validity.value) === 1 ? option.value.slice(0, -1) : option.value;
    }
    get('pause').closest('.input-group').querySelector('.adorn').textContent = Number(get('pause').value) === 1 ? 'pause' : 'pauses';
    row.querySelector('.tier-sum').textContent = !cost.value || !get('minutes').value ? 'New tier' : (get('price') ? '₱' : '') + cost.value + (get('price') ? '' : ' pts') + ' → ' + duration(Number(get('minutes').value)) + ' · ' + (unit.value === 'none' ? 'No limit' : 'valid ' + validity.value + ' ' + unit.selectedOptions[0].textContent);
  },
  // check validates a tier editor form in place: on a problem it marks the
  // fields, prepends the message and returns false; otherwise it sorts the rows.
  check(form) {
    const kind = form.dataset.tiers;
    const rows = [...form.querySelectorAll('.tier')], costs = new Set(), messages = []; let first;
    rows.forEach((row, index) => {
      const noun = (kind === 'rates' ? 'Tier ' : 'Row ') + (index + 1) + ': ';
      for (const input of row.querySelectorAll('input')) {
        input.removeAttribute('aria-invalid');
        if (input.disabled) continue;
        const name = input.name.slice(5), value = Number(input.value);
        let valid = input.checkValidity();
        if (name === 'minutes') valid = valid && value * 60 >= 1 && Math.abs(value * 60 - Math.round(value * 60)) < 0.000001;
        if (name === 'validity') {
          const factor = tierUnits[row.querySelector('[name=tier_unit]').value];
          valid = valid && value * factor >= 1 && Math.abs(value * factor - Math.round(value * factor)) < 0.000001;
        }
        if (!valid) {
          input.setAttribute('aria-invalid', 'true'); first ||= input;
          const reasons = { price: 'Price must be whole pesos, at least ₱1.', cost: 'Points cost must be positive, with up to three decimal places.', minutes: 'check ' + (kind === 'rates' ? 'Price' : 'Points cost') + ' and Time.', points: 'Points earned must be nonnegative, with up to three decimal places.', validity: 'Validity must be positive and resolve to whole seconds, or choose No limit.', pause: 'Pause limit must be a nonnegative whole number.' };
          messages.push(noun + reasons[name]);
        }
      }
      const cost = row.querySelector('[name=tier_price], [name=tier_cost]');
      if (costs.has(Number(cost.value))) { cost.setAttribute('aria-invalid', 'true'); first ||= cost; messages.push(noun + (kind === 'rates' ? 'Price' : 'Points cost') + ' duplicates another row.'); }
      costs.add(Number(cost.value));
    });
    if (first || (kind === 'rates' && !rows.length)) {
      const error = document.createElement('p'); error.dataset.saveError = ''; error.className = 'field-error'; error.setAttribute('role', 'alert'); error.textContent = messages.join(' ') || 'Rates need at least one tier.';
      form.prepend(error); first?.focus(); return false;
    }
    rows.sort((a, b) => Number(a.querySelector('[name=tier_price], [name=tier_cost]').value) - Number(b.querySelector('[name=tier_price], [name=tier_cost]').value));
    form.querySelector('[data-tier-list]').replaceChildren(...rows);
    this.refresh(form);
    return true;
  },
  // body is a tier editor form's post body. Disabled no-limit numbers still
  // need a placeholder to keep repeated fields aligned.
  body(form) {
    const body = new FormData(form);
    body.delete('tier_validity'); form.querySelectorAll('[name=tier_validity]').forEach((input) => body.append('tier_validity', input.disabled ? '' : input.value));
    return body;
  },
};

// The Vendo manager lives outside fetched cards. Undo deadlines and drafts must
// survive a refreshed card, including when another Vendo is edited meanwhile.
const vendoUI = {
  card: null, selected: '', filter: 'all', edit: null, hold: null,
  pending: new Map(), busy: new Set(), errors: new Map(), observer: null,
  init() {
    const card = document.querySelector('[data-vendos]');
    if (!card || card === this.card) return;
    this.card = card;
    const url = new URL(location.href);
    this.selected = url.searchParams.get('vendo') || '';
    this.observe();
    this.update();
    this.detail();
    // ?tab=rates|points (from a Shop page's Edit tiers) opens the selected Vendo's dialog on that tab, once.
    const tab = url.searchParams.get('tab');
    const article = this.card.querySelector('[data-detail-slot] .vdetail, [data-sheet-slot] .vdetail');
    if (['rates', 'points'].includes(tab) && article) {
      url.searchParams.delete('tab'); history.replaceState(null, '', url);
      this.openEdit(article); this.tab(tab); this.edit.root.querySelector('[data-edit-tab=' + tab + ']').focus();
    }
  },
  observe() {
    this.observer?.disconnect();
    this.observer = new ResizeObserver(() => {
      if (this.edit) this.editor.toggleAttribute('data-narrow', this.narrow());
      this.detail();
    });
    this.observer.observe(this.card);
  },
  narrow() { return getComputedStyle(this.card.querySelector('.vpane')).display === 'none'; },
  rows() { return [...this.card.querySelectorAll('[data-select]')]; },
  group(status) {
    if (status === 'Online') return 'online';
    if (['Deactivated', 'Unpaired'].includes(status)) return 'off';
    return 'attention';
  },
  update() {
    const rows = this.rows();
    const active = rows.filter((r) => r.dataset.status !== 'Unpaired');
    for (const kind of ['current', 'total']) {
      this.card.querySelector('[data-total=' + kind + ']').textContent = '₱' + active.reduce((sum, r) => sum + Number(r.dataset[kind]), 0).toLocaleString();
    }
    this.card.querySelector('[data-total=online]').textContent = active.filter((r) => r.dataset.status === 'Online').length + '/' + active.length;
    this.card.querySelector('[data-total=attention]').textContent = rows.filter((r) => this.group(r.dataset.status) === 'attention').length;
    this.card.querySelectorAll('[data-filter]').forEach((b) => {
      b.setAttribute('aria-pressed', String(this.filter === b.dataset.filter));
      const count = rows.filter((r) => b.dataset.filter === 'all' || this.group(r.dataset.status) === b.dataset.filter).length;
      b.querySelector('.filter-count').textContent = count;
      b.querySelector('.filter-count').classList.toggle('warn', b.dataset.filter === 'attention' && count > 0);
    });
    for (const row of rows) {
      row.hidden = this.filter !== 'all' && this.group(row.dataset.status) !== this.filter;
      row.setAttribute('aria-current', String(row.dataset.select === this.selected));
      row.toggleAttribute('data-pending', this.pending.has(row.dataset.select));
    }
    this.card.querySelector('[data-filter-empty]').hidden = !rows.length || rows.some((r) => !r.hidden);
    this.card.querySelectorAll('[data-duration]').forEach((el) => { el.textContent = duration(Number(el.dataset.duration) / 60); });
  },
  select(id, writeURL = true) {
    this.cancelHold();
    this.selected = id;
    if (writeURL) {
      const url = new URL(location.href);
      if (id) url.searchParams.set('vendo', id); else url.searchParams.delete('vendo');
      history.replaceState(null, '', url);
    }
    this.update(); this.detail();
  },
  detail() {
    if (!this.card?.isConnected || this.hold) return;
    const source = [...this.card.querySelectorAll('[data-vendo-source]')].find((t) => t.dataset.vendoSource === this.selected);
    const sheet = this.card.querySelector('[data-vendo-sheet]');
    const pane = this.card.querySelector('[data-detail-slot]');
    this.card.querySelector('.pane-empty').hidden = !!source;
    if (!source) { pane.replaceChildren(); sheet.close(); this.placeToasts(); return; }
    const slot = this.narrow() ? this.card.querySelector('[data-sheet-slot]') : pane;
    if (this.narrow()) pane.replaceChildren(); else sheet.close();
    slot.replaceChildren(source.content.cloneNode(true));
    if (this.narrow() && !sheet.open) {
      const focused = document.activeElement;
      if (this.edit) this.editor.close();
      sheet.showModal();
      if (this.edit) { this.editor.showModal(); focused?.focus(); }
    }
    const error = this.errors.get(this.selected);
    if (error) {
      const output = document.createElement('p'); output.className = 'field-error'; output.dataset.vendoError = ''; output.setAttribute('role', 'alert'); output.textContent = error;
      slot.querySelector('.vdetail').prepend(output);
    }
    const pending = this.pending.get(this.selected);
    if (pending) {
      slot.querySelectorAll('button:not([data-vendo-close])').forEach((b) => { b.disabled = true; });
      const msg = document.createElement('p');
      msg.className = 'banner'; msg.textContent = pending.kind === 'unpair' ? (pending.committing ? 'Unpairing…' : 'Unpair scheduled. Undo is available for 30 seconds.') : 'Deactivated. Undo is available for 30 seconds.';
      slot.querySelector('.vdetail').prepend(msg);
    }
    this.placeToasts();
  },
  async refresh(id) {
    if (!this.card?.isConnected) return;
    const response = await fetch('/admin/vendos?vendo=' + encodeURIComponent(id), { headers: { Accept: 'text/html' } });
    if (!response.ok) throw new Error(await response.text());
    const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
    const fresh = doc.querySelector('[data-vendos]');
    if (!fresh) throw new Error('Session expired or the Vendo page could not be loaded. Reload and sign in again.');
    const sheet = this.card.querySelector('[data-vendo-sheet]');
    this.cancelHold();
    // Never detach an open dialog: doing so removes its modal/top-layer state,
    // including any Undo popover inside it. Replace the other card regions.
    fresh.querySelector('[data-vendo-sheet]').remove();
    [...this.card.childNodes].filter((node) => node !== sheet).forEach((node) => node.remove());
    while (fresh.firstChild) this.card.insertBefore(fresh.firstChild, sheet);
    this.update(); this.detail();
  },
  async post(url, body) {
    const response = await fetch(url, { method: 'POST', body: new URLSearchParams(body), redirect: 'follow' });
    if (!response.ok) throw new Error((await response.text()).trim() || 'The action failed.');
    if (new URL(response.url).pathname === '/admin/login') throw new Error('Your session expired. Sign in again before retrying.');
  },
  ensureDialogs() {
    if (this.editor) return;
    this.editor = document.createElement('dialog'); this.editor.className = 'vendo-editor'; this.editor.setAttribute('aria-label', 'Edit Vendo');
    this.confirm = document.createElement('dialog'); this.confirm.className = 'vendo-confirm'; this.confirm.setAttribute('aria-labelledby', 'vendo-confirm-title');
    this.toasts = document.createElement('div'); this.toasts.className = 'vendo-toasts'; this.toasts.setAttribute('popover', 'manual'); this.toasts.setAttribute('aria-live', 'polite');
    document.body.append(this.editor, this.confirm, this.toasts);
  },
  placeToasts() {
    if (!this.toasts) return;
    const host = [...document.querySelectorAll('dialog:modal')].at(-1) || document.body;
    if (this.toasts.parentElement !== host) { this.toasts.hidePopover(); host.append(this.toasts); }
    if (this.toasts.children.length) {
      this.toasts.showPopover();
      for (const item of this.toasts.children) {
        for (const animation of item.querySelector('.toast-bar').getAnimations()) animation.currentTime = performance.now() - Number(item.dataset.started);
      }
    }
  },
  ask(title, message, ok = 'Confirm', danger = false, amount = '') {
    this.ensureDialogs();
    if (this.toasts.parentElement === this.confirm) document.body.append(this.toasts);
    return new Promise((resolve) => {
      this.confirm.replaceChildren();
      const h = document.createElement('h3'); h.id = 'vendo-confirm-title'; h.textContent = title;
      const p = document.createElement('p'); p.textContent = message;
      const foot = document.createElement('div'); foot.className = 'confirm-foot';
      const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'btn outline sm'; cancel.textContent = 'Cancel';
      const yes = document.createElement('button'); yes.type = 'button'; yes.className = 'btn sm' + (danger ? ' destructive' : ''); yes.textContent = ok;
      let settled = false;
      const finish = (accepted) => {
        if (settled) return;
        settled = true;
        this.confirm.close(); this.placeToasts(); resolve(accepted);
      };
      cancel.onclick = () => finish(false); yes.onclick = () => finish(true);
      this.confirm.oncancel = (e) => { e.preventDefault(); finish(false); };
      this.confirm.onclick = (e) => {
        if (e.target !== this.confirm) return;
        const rect = this.confirm.getBoundingClientRect();
        if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) finish(false);
      };
      this.confirm.append(h);
      if (amount) { const value = document.createElement('p'); value.className = 'confirm-amount'; value.textContent = amount; this.confirm.append(value); }
      foot.append(cancel, yes); this.confirm.append(p, foot);
      this.confirm.showModal(); this.placeToasts(); cancel.focus();
    });
  },
  toast(message, undo, duration = 5000) {
    this.ensureDialogs();
    const item = document.createElement('div'); item.className = 'vendo-toast'; item.dataset.started = performance.now();
    if (!undo) item.dataset.short = '';
    const text = document.createElement('span'); text.textContent = message; item.append(text);
    if (undo) {
      const button = document.createElement('button'); button.type = 'button'; button.textContent = 'Undo';
      button.onclick = async () => { button.disabled = true; try { await undo(); item.remove(); } catch (error) { button.disabled = false; this.toast(error.message); } };
      item.append(button);
    }
    const bar = document.createElement('div'); bar.className = 'toast-bar'; item.append(bar);
    this.toasts.append(item); this.placeToasts();
    setTimeout(() => { item.remove(); if (!this.toasts.children.length) this.toasts.hidePopover(); }, duration);
    return item;
  },
  async action(form) {
    const id = new FormData(form).get('id');
    if (this.busy.has(id) || this.pending.has(id)) return;
    const kind = form.dataset.vendoAction;
    const body = new FormData(form);
    const off = kind === 'power' && body.get('active') === '0';
    const name = form.closest('[data-name]').dataset.name;
    let title = '', message = '', ok = 'Confirm', amount = '';
    if (kind === 'coins') { title = 'Coins Out for ' + name + '?'; amount = '₱' + form.dataset.amount; ok = 'Record ' + amount; message = 'Empty the coin box, then record it. Current Sales restarts at ₱0; Total Sales keeps it.'; }
    if (kind === 'power') { ok = off ? 'Deactivate' : 'Reactivate'; title = ok + ' ' + name + '?'; message = off ? 'It stops taking coins until you reactivate it.' : 'It starts taking coins again.'; }
    if (title && !await this.ask(title, message, ok, off, amount)) return;
    this.busy.add(id);
    try {
      await this.post(form.getAttribute('action'), body);
      if (off) {
        const state = { kind: 'deactivate' }; this.pending.set(id, state);
        state.timer = setTimeout(() => { this.pending.delete(id); this.update(); this.detail(); }, 30000);
        this.toast(name + ' deactivated', async () => {
          const undo = new FormData(); undo.set('id', id); undo.set('active', '1');
          await this.post('/admin/vendos/active', undo); clearTimeout(state.timer); this.pending.delete(id);
          await this.refresh(id); this.toast(name + ' is active again');
        }, 30000);
      } else this.toast(kind === 'coins' ? 'Coins Out recorded: ' + amount + ' from ' + name : name + ' reactivated');
      this.errors.delete(id);
      await this.refresh(id);
    } catch (error) { this.errors.set(id, error.message); this.detail(); this.toast(error.message); }
    finally { this.busy.delete(id); }
  },
  startHold(button, event) {
    if (this.hold || this.pending.has(button.dataset.hold) || this.busy.has(button.dataset.hold)) return;
    if (event.type === 'pointerdown') { if (event.button !== 0) return; button.setPointerCapture(event.pointerId); }
    button.dataset.holding = 'true';
    this.hold = { button, pointer: event.pointerId, started: performance.now(), interval: setInterval(() => this.tickHold(), 50) };
    this.tickHold();
  },
  tickHold() {
    if (!this.hold) return;
    const { button, started } = this.hold;
    const progress = Math.min(1, (performance.now() - started) / 3000);
    button.style.setProperty('--p', progress);
    button.querySelector('.hold-txt').textContent = 'Keep holding… ' + Math.max(1, Math.ceil(3 * (1 - progress))) + 's';
    if (progress < 1) return;
    const id = button.dataset.hold, name = button.closest('[data-name]').dataset.name;
    this.cancelHold();
    const state = { kind: 'unpair', committing: false }; this.pending.set(id, state);
    state.timer = setTimeout(async () => {
      // Browser-local grace period: no unpair POST exists before this deadline.
      state.committing = true; toast.querySelector('button').disabled = true; toast.remove();
      this.busy.add(id); this.detail();
      try {
        const body = new FormData(); body.set('id', id);
        await this.post('/admin/vendos/unpair', body);
        this.pending.delete(id); this.errors.delete(id); await this.refresh(id); this.toast(name + ' unpaired');
      } catch (error) {
        this.pending.delete(id); this.errors.set(id, error.message);
        try { await this.refresh(id); } catch (refreshError) { this.update(); this.detail(); this.toast(refreshError.message); }
        this.toast('Unpair failed: ' + error.message);
      } finally { this.busy.delete(id); }
    }, 30000);
    const toast = this.toast('Unpairing ' + name + ' in 30s', () => {
      if (state.committing) return;
      clearTimeout(state.timer); this.pending.delete(id); this.update(); this.detail(); this.toast('Unpair cancelled');
    }, 30000);
    this.update(); this.detail();
  },
  cancelHold() {
    if (!this.hold) return;
    const hold = this.hold; this.hold = null;
    clearInterval(hold.interval);
    hold.button.style.setProperty('--p', 0);
    delete hold.button.dataset.holding; hold.button.querySelector('.hold-txt').textContent = 'Hold to unpair';
    if (hold.pointer !== undefined && hold.button.hasPointerCapture(hold.pointer)) hold.button.releasePointerCapture(hold.pointer);
  },
  snapshot(root) {
    return JSON.stringify([...root.querySelectorAll('form')].map((form) => [...new FormData(form)]));
  },
  dirty() {
    return this.edit && [...this.edit.root.querySelectorAll('[data-edit-panel]')].some((panel) => this.snapshot(panel) !== this.edit.saved[panel.dataset.editPanel]);
  },
  openEdit(article) {
    this.ensureDialogs();
    if (this.toasts.parentElement === this.editor) document.body.append(this.toasts);
    const root = article.querySelector('[data-edit-source]').content.firstElementChild.cloneNode(true);
    this.edit = { root, id: article.dataset.id, name: article.dataset.name, tab: 'details', saved: {} };
    root.querySelectorAll('[data-duration]').forEach((el) => { el.textContent = duration(Number(el.dataset.duration) / 60); });
    tierEditor.prepare(root);
    root.querySelectorAll('[data-edit-panel]').forEach((panel) => {
      const name = panel.dataset.editPanel;
      this.edit.saved[name] = this.snapshot(panel);
      panel.id = 'vendo-edit-' + article.dataset.id + '-' + name;
      const tab = root.querySelector('[data-edit-tab=' + name + ']');
      tab.id = panel.id + '-tab'; tab.setAttribute('aria-controls', panel.id); panel.setAttribute('aria-labelledby', tab.id);
    });
    this.tab('details');
    this.editor.toggleAttribute('data-narrow', this.narrow());
    this.editor.replaceChildren(root); this.editor.showModal();
    this.placeToasts();
    (this.narrow() ? root.querySelector('.sheet-edit-titles h3') : root.querySelector('[autofocus]')).focus();
  },
  async closeEdit() {
    if (!this.edit || (this.dirty() && !await this.ask('Discard changes?', 'Your edits to this Vendo have not been saved.', 'Discard', true))) return;
    this.edit = null; this.editor.close();
    const sheet = this.card.querySelector('[data-vendo-sheet]');
    this.detail(); this.placeToasts();
    const scope = this.narrow() ? sheet : this.card;
    scope.querySelector('[data-vendo-edit]')?.focus();
  },
  tab(name) {
    this.edit.tab = name;
    this.edit.root.querySelectorAll('[data-edit-tab]').forEach((b) => {
      const active = b.dataset.editTab === name; b.setAttribute('aria-selected', String(active)); b.tabIndex = active ? 0 : -1;
    });
    this.edit.root.querySelectorAll('[data-edit-panel]').forEach((p) => { p.hidden = p.dataset.editPanel !== name; });
  },
  async save(form) {
    if (!this.edit || this.busy.has(this.edit.id)) return;
    const root = this.edit.root, id = this.edit.id, kind = form.dataset.tiers;
    root.querySelector('[data-save-error]')?.remove();
    if (kind && !tierEditor.check(form)) return;
    this.busy.add(id);
    try {
      const body = kind ? tierEditor.body(form) : new FormData(form);
      const url = form.getAttribute('action');
      const before = this.snapshot(root);
      await this.post(url, body);
      await this.refresh(id);
      if (this.edit?.root === root) {
        // A save only replaces this panel; drafts in the other tabs stay intact.
        const source = [...this.card.querySelectorAll('[data-vendo-source]')].find((t) => t.dataset.vendoSource === id);
        const freshEdit = source?.content.querySelector('[data-edit-source]')?.content;
        const panel = form.closest('[data-edit-panel]');
        if (freshEdit && panel && this.snapshot(root) === before) {
          const freshPanel = freshEdit.querySelector('[data-edit-panel=' + panel.dataset.editPanel + ']').cloneNode(true);
          freshPanel.hidden = panel.hidden;
          freshPanel.id = panel.id; freshPanel.setAttribute('aria-labelledby', panel.getAttribute('aria-labelledby'));
          tierEditor.prepare(freshPanel);
          freshPanel.querySelectorAll('[data-duration]').forEach((el) => { el.textContent = duration(Number(el.dataset.duration) / 60); });
          panel.replaceWith(freshPanel);
          this.edit.saved[freshPanel.dataset.editPanel] = this.snapshot(freshPanel);
        }
      }
      const savedName = [...this.card.querySelectorAll('[data-vendo-source]')].find((t) => t.dataset.vendoSource === id)?.content.querySelector('[data-name]')?.dataset.name || this.edit?.name;
      this.toast(kind === 'rates' ? 'Rates saved for ' + savedName + '; they apply from the next Coin Window' : kind === 'points' ? 'Points table saved for ' + savedName : 'Saved ' + savedName);
    } catch (error) {
      const output = document.createElement('p'); output.dataset.saveError = ''; output.className = 'field-error'; output.setAttribute('role', 'alert'); output.textContent = error.message; form.prepend(output);
    } finally { this.busy.delete(id); }
  },
};

// shopTiers runs a Shop page's tier editor, rendered in the page itself rather
// than in a Vendo's Edit dialog template.
const shopTiers = {
  // prepare builds the editor's rows once per load or boosted swap.
  prepare() {
    document.querySelectorAll('[data-shop-tiers]:not([data-ready])').forEach((el) => { el.dataset.ready = ''; tierEditor.prepare(el); });
  },
  // save posts a Shop page's tier editor; the editor keeps its rows.
  async save(form) {
    if (form.dataset.busy !== undefined) return;
    form.querySelector('[data-save-error]')?.remove();
    if (!tierEditor.check(form)) return;
    form.dataset.busy = '';
    try {
      await vendoUI.post(form.getAttribute('action'), tierEditor.body(form));
      vendoUI.toast(form.dataset.tiers === 'rates' ? 'Shop tiers saved; each Vendo using them prices at them from its next Coin Window' : 'Shop Points table saved');
    } catch (error) {
      const output = document.createElement('p'); output.dataset.saveError = ''; output.className = 'field-error'; output.setAttribute('role', 'alert'); output.textContent = error.message; form.prepend(output);
    } finally { delete form.dataset.busy; }
  }
};
document.addEventListener('DOMContentLoaded', () => { vendoUI.init(); shopTiers.prepare(); });
document.addEventListener('htmx:after:settle', () => { vendoUI.init(); shopTiers.prepare(); });
window.addEventListener('popstate', () => { if (vendoUI.card?.isConnected) vendoUI.select(new URL(location.href).searchParams.get('vendo') || '', false); });
window.addEventListener('beforeunload', (e) => {
  if ([...vendoUI.pending.values()].some((p) => p.kind === 'unpair') || vendoUI.dirty()) { e.preventDefault(); e.returnValue = ''; }
});
document.addEventListener('htmx:before:request', (e) => {
  if (e.target.closest?.('a') && ([...vendoUI.pending.values()].some((p) => p.kind === 'unpair') || vendoUI.dirty())) {
    e.preventDefault(); vendoUI.toast('Finish or Undo pending unpairs and save or discard edits before leaving this page.');
  }
});
document.addEventListener('click', async (e) => {
  if (!(e.target instanceof Element)) return;
  const target = e.target;
  const pill = target.closest('[data-shop-tiers] [data-group]');
  if (pill) {  // a coverage pill expands its Vendo list and collapses the other
    const open = pill.getAttribute('aria-expanded') !== 'true';
    pill.parentElement.querySelectorAll('[data-group]').forEach((p) => { p.setAttribute('aria-expanded', String(open && p === pill)); document.getElementById(p.getAttribute('aria-controls')).hidden = !(open && p === pill); });
    return;
  }
  if (target.closest('[data-select]')) return vendoUI.select(target.closest('[data-select]').dataset.select);
  const filter = target.closest('[data-filter]');
  if (filter) { vendoUI.filter = filter.dataset.filter; vendoUI.update(); return; }
  if (target.closest('[data-vendo-close]')) { const id = vendoUI.selected; vendoUI.select(''); vendoUI.rows().find((r) => r.dataset.select === id)?.focus(); return; }
  if (target.closest('[data-vendo-edit]')) return vendoUI.openEdit(target.closest('.vdetail'));
  if (target.closest('[data-edit-close]')) return vendoUI.closeEdit();
  const tab = target.closest('[data-edit-tab]'); if (tab) return vendoUI.tab(tab.dataset.editTab);
  const add = target.closest('[data-tier-add]');
  if (add) { const form = add.closest('form'); const row = tierEditor.row(form.dataset.tiers); form.querySelector('[data-tier-list]').append(row); tierEditor.refresh(form); row.querySelector('input').focus(); return; }
  const del = target.closest('[data-tier-delete]');
  if (del) { const form = del.closest('form'); if (form.dataset.tiers === 'rates' && form.querySelectorAll('.tier').length === 1) { vendoUI.toast('Keep at least one rate tier.'); return; } del.closest('.tier').remove(); tierEditor.refresh(form); form.querySelector('[data-tier-add]').focus(); }
  const dialog = target.closest('dialog');
  if (dialog && target === dialog && ['vendo-editor', 'sheet'].some((c) => dialog.classList.contains(c))) {
    const rect = dialog.getBoundingClientRect();
    if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) {
      if (vendoUI.edit) vendoUI.closeEdit(); else vendoUI.select('');
    }
  }
});
document.addEventListener('submit', (e) => {
  if (!(e.target instanceof HTMLFormElement)) return;
  const form = e.target;
  if (form.dataset.tiers && form.closest('[data-shop-tiers]')) { e.preventDefault(); shopTiers.save(form); return; }
  if (!form.closest('[data-vendos], .vendo-editor')) return;
  e.preventDefault();
  if (form.dataset.vendoAction && !form.closest('.vendo-edit-body')) vendoUI.action(form);
  else if (form.dataset.vendoAction === 'site') {
    const [title, message] = form.getAttribute('action').endsWith('/points')
      ? ['Use Shop Points table?', "This Vendo's own table is discarded. Redemptions here use the Shop table from now on."]
      : ['Use Shop tiers?', "This Vendo's own tiers are discarded. Its next Coin Window prices at Shop tiers."];
    vendoUI.ask(title, message, title.slice(0, -1)).then((yes) => { if (yes) vendoUI.save(form); });
  } else vendoUI.save(form);
});
for (const name of ['input', 'change']) document.addEventListener(name, (e) => {
  const row = e.target.closest?.('.tier');
  if (row) { e.target.removeAttribute('aria-invalid'); tierEditor.summary(row); }
});
document.addEventListener('cancel', (e) => {
  if (e.target.matches('[data-vendo-sheet], .vendo-editor')) {
    e.preventDefault(); vendoUI.cancelHold();
    if (vendoUI.edit) vendoUI.closeEdit(); else vendoUI.select('');
  }
}, true);
document.addEventListener('pointerdown', (e) => { const b = e.target.closest?.('[data-hold]'); if (b) { e.preventDefault(); b.focus(); vendoUI.startHold(b, e); } });
for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) document.addEventListener(name, (e) => { if (vendoUI.hold && e.pointerId === vendoUI.hold.pointer) vendoUI.cancelHold(); });
document.addEventListener('contextmenu', (e) => { if (e.target.closest?.('[data-hold]')) e.preventDefault(); });
document.addEventListener('keydown', (e) => {
  const hold = e.target.closest?.('[data-hold]');
  if (hold && [' ', 'Enter'].includes(e.key)) { e.preventDefault(); if (!e.repeat) vendoUI.startHold(hold, e); }
  if (e.key === 'Escape') vendoUI.cancelHold();
  const tab = e.target.closest?.('[data-edit-tab]');
  if (tab && ['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(e.key)) {
    e.preventDefault(); const tabs = [...tab.parentElement.querySelectorAll('[data-edit-tab]')];
    const index = e.key === 'Home' ? 0 : e.key === 'End' ? 2 : (tabs.indexOf(tab) + (e.key === 'ArrowRight' ? 1 : 2)) % 3;
    vendoUI.tab(tabs[index].dataset.editTab); tabs[index].focus();
  }
});
document.addEventListener('keyup', (e) => { if ([' ', 'Enter'].includes(e.key) && vendoUI.hold?.pointer === undefined) vendoUI.cancelHold(); });
document.addEventListener('focusout', (e) => { if (e.target === vendoUI.hold?.button) vendoUI.cancelHold(); });
window.addEventListener('blur', () => vendoUI.cancelHold());
document.addEventListener('visibilitychange', () => { if (document.hidden) vendoUI.cancelHold(); });
