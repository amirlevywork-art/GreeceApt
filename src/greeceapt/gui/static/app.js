"use strict";
// GreeceApt GUI — vanilla JS, no external libraries. Data comes from the local server only;
// photos load from the Spitogatos image server, as when browsing the site.

const TOKEN = document.querySelector('meta[name="greeceapt-token"]').content;
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ── icons (feather-style strokes) ─────────────────────────────────────────────
const ICONS = {
  home: '<path d="M3 10.5 12 3l9 7.5V21h-6v-6H9v6H3z"/>',
  award: '<circle cx="12" cy="8" r="6"/><path d="M8.2 13.2 7 22l5-3 5 3-1.2-8.8"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  star: '<path d="m12 2 3.1 6.3 6.9 1-5 4.9 1.2 6.8L12 17.8 5.8 21l1.2-6.8-5-4.9 6.9-1z"/>',
  alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01"/>',
  ban: '<circle cx="12" cy="12" r="10"/><path d="m4.9 4.9 14.2 14.2"/>',
  pin: '<path d="M21 10c0 7-9 13-9 13S3 17 3 10a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  play: '<path d="m6 3 14 9-14 9z"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  sliders: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  external: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14 21 3"/>',
  left: '<path d="m15 18-6-6 6-6"/>', right: '<path d="m9 18 6-6-6-6"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  refresh: '<path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0 0 20.5 15"/>',
  download: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/>',
  area: '<path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3"/>',
  layers: '<path d="m12 2 10 5-10 5L2 7z"/><path d="m2 17 10 5 10-5M2 12l10 5 10-5"/>',
  sparkle: '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8"/>',
  calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
  bed: '<path d="M2 4v16M2 8h18a2 2 0 0 1 2 2v10M2 17h20M6 8v9"/>',
  bolt: '<path d="M13 2 3 14h9l-1 8 10-12h-9z"/>',
  history: '<path d="M3 3v5h5"/><path d="M3.05 13A9 9 0 1 0 6 5.3L3 8"/><path d="M12 7v5l4 2"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  photo: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/>',
  cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/>',
  globe: '<circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15 15 0 0 1 4 10 15 15 0 0 1-4 10 15 15 0 0 1-4-10 15 15 0 0 1 4-10z"/>',
};
const icon = (name, cls = "") => `<svg class="i ${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;

// ── formatting ────────────────────────────────────────────────────────────────
const isNum = (v) => typeof v === "number" && isFinite(v);
const eur = (v) => (isNum(v) ? "€" + Math.round(v).toLocaleString("en") : "–");
const pct = (v, d = 2) => (isNum(v) ? (v * 100).toFixed(d) + "%" : "–");
const num = (v, d = 0) => (isNum(v) ? v.toLocaleString("en", { maximumFractionDigits: d, minimumFractionDigits: d }) : "–");
const words = (v) => String(v ?? "").replaceAll("_", " ");
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
function duration(s) {
  if (!isNum(s)) return "–";
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + " s";
  const m = Math.round(s / 60);
  if (m < 60) return m + " min";
  const h = Math.floor(m / 60), r = m % 60;
  if (h < 48) return h + " h" + (r ? " " + r + " min" : "");
  return Math.round(h / 24) + " days";
}
function when(iso) {
  if (!iso) return "–";
  return new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
function ago(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 90) return "just now";
  const n = (v, unit) => `${v} ${unit}${v === 1 ? "" : "s"} ago`;
  if (s < 3600) return n(Math.round(s / 60), "minute");
  if (s < 86400) return n(Math.round(s / 3600), "hour");
  const d = Math.round(s / 86400);
  return d === 1 ? "yesterday" : d + " days ago";
}
const listedDate = (iso) => new Date(iso + "T00:00:00").toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
const FLOORS = { basement: "Basement", semi_basement: "Semi-basement", ground: "Ground floor", elevated_ground: "Raised ground floor", upper: "Upper floor" };
// Short names for table cells (the flat page uses the full ones)
const FLOORS_SHORT = { basement: "Basement", semi_basement: "Semi-basement", ground: "Ground", elevated_ground: "Raised ground" };
const floorText = (f) => (f.floor_class === "upper" && isNum(f.floor) ? `Floor ${f.floor}` : FLOORS[f.floor_class] || "Floor unknown");
const belowGround = (f) => f.floor_class === "basement" || f.floor_class === "semi_basement";
// Net yield after every cost and tax (Nov 2026: median 2.8 %, best 4.7 %).
const yclass = (v) => (!isNum(v) ? "y-lo" : v >= 0.035 ? "y-hi" : v >= 0.03 ? "y-ok" : v >= 0.025 ? "y-mid" : "y-lo");
const ypill = (v) => `<span class="ypill ${yclass(v)}">${pct(v)}</span>`;

// Plain-English names for the pipeline's codes: [label, tone, explanation]
const FLAGS = {
  basement: ["Basement", "bad", "Below street level. Check the permit allows it as a home (κύρια χρήση)."],
  semi_basement: ["Semi-basement", "bad", "Partly below street level. Check the permit allows it as a home."],
  renovation_full: ["Full renovation", "warn", "Condition is poor; the cost includes a full renovation."],
  kitchen_work: ["Kitchen work", "warn", "The photos show a kitchen in poor condition: its job (about €4,000 at a basic standard) is in the cost, even though the rest of the flat looks fine."],
  bathroom_work: ["Bathroom work", "warn", "The photos show a bathroom in poor condition: its job (about €4,500 at a basic standard) is in the cost, even though the rest of the flat looks fine."],
  area_includes_extra_space: ["Area includes extra space", "warn", "The ad's area adds an attic, terrace or storage; rent, renovation and tax use the living area only."],
  let_with_tenant: ["Let, tenant stays", "good", "The ad says it is let now for at least the model's rent: the cost assumes you keep the tenant and do not renovate."],
  renovation_medium: ["Renovation", "warn", "Kitchen and bathroom work is included in the cost."],
  renovation_paint: ["Repaint", "", "A repaint is included in the cost."],
  needs_renovation: ["Ad says: needs renovation", "warn", "The advertisement itself says the flat needs renovation."],
  sparse_description: ["Short description", "", "The ad says little. Ask the agent for details."],
  source_price_conflict: ["Prices differ between ads", "warn", "Agencies list this flat at different prices; the cheapest live one is used."],
  source_price_spread: ["Prices differ between ads", "warn", "Agencies list this flat at different prices."],
  thin_visual_evidence: ["Few usable photos", "warn", "The condition score rests on few photos."],
  floor_conflict_adjacent: ["Floor unclear", "warn", "The ads disagree: basement or semi-basement."],
  floor_conflict: ["Floor unclear", "warn", "The ads disagree on the floor."],
  duplicate_review_pending: ["Possible duplicate", "warn", "Waiting for your decision under Duplicates."],
};
const CHECKS = {
  location_mismatch: "The description names another neighborhood",
  no_living_space_seen: "No kitchen, living room or bedroom in any photo",
  price_outlier: "Price per m² is less than half the neighborhood's — find out why",
  not_habitable: "Unfinished, or without kitchen or bathroom",
};
const REASONS = {
  on_market_too_long: "On the market over 6 months",
  xe_only_untracked: "Only on XE (not collected now)",
  no_longer_listed: "No longer listed",
  condition_unaccepted: "Photos not good enough to judge",
  duplicate_review_pending: "Possible duplicate — your decision",
  area_below_rent_data: "Under 20 m²",
  floor_conflict: "Ads disagree on the floor",
  too_few_usable_photos: "Fewer than 3 usable photos",
  commercial_premises: "Shop or store room",
  floor_unknown: "Floor not stated",
  no_living_space_seen: "No kitchen, living room or bedroom in the photos",
};
const REASON_EXPLAIN = {
  on_market_too_long: "It was first advertised more than 6 months ago (counted from its oldest ad). A flat that long on the market usually has a reason; ask the agent why it has not sold.",
  xe_only_untracked: "It is only advertised on XE, which is not collected now, so its price and availability cannot be kept up to date.",
  no_longer_listed: "Its ads are no longer on Spitogatos: probably sold or withdrawn.",
  condition_unaccepted: "The image model could not judge its condition from the photos, so the renovation cost and the rent cannot be estimated.",
  duplicate_review_pending: "It looks like another ad of the same flat. Decide under Duplicates and re-score.",
  area_below_rent_data: "Under 20 m²: almost no rentals that small exist to compare its rent with.",
  floor_conflict: "Its ads disagree on the floor, and the floor changes the rent a lot.",
  too_few_usable_photos: "Fewer than 3 usable photos: too little to judge its condition.",
  commercial_premises: "The ad describes a shop or store room, not a home.",
  floor_unknown: "The floor is not stated, and the floor changes the rent a lot.",
  no_living_space_seen: "No photo shows a kitchen, living room or bedroom: probably a store room or shop, not a home.",
};
const reasonText = (r) => REASONS[r] || cap(words(r || "other"));
const checkText = (r) => { const [code, ...rest] = String(r).split(":"); return (CHECKS[code] || cap(words(code))) + (rest.length ? ` (${rest.join(":").trim()})` : ""); };
const TIPS = {
  net: "What you keep each year after everything — half a month empty, a management company (1 month's rent), maintenance (5%), insurance, ENFIA, an accountant and 15% income tax — divided by everything you pay: price + 8% purchase costs + any renovation. Uses the conservative signed rent (asking rents less 10%).",
  before_tax: "The same yield before the 15% Greek rental income tax.",
  gross: "A year of the same conservative rent divided by the total cost, before any running cost or tax.",
  condition: "0–10, judged by the local image model from the photos. Below 8 the cost includes a renovation.",
  rent: "Conservative – central monthly rent a tenant signs for: the model's asking rent less 10% (ads ask more than flats let for). The conservative figure is the one 90% of similar flats reach.",
};
const tip = (key) => `<span class="tip" data-tip="${esc(TIPS[key] || key)}">?</span>`;

// ── API ───────────────────────────────────────────────────────────────────────
async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: ${r.status} ${await r.text()}`);
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}
async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "content-type": "application/json", "x-greeceapt-token": TOKEN }, body: JSON.stringify(body || {}) });
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch { data = { detail: text }; }
  if (!r.ok) throw new Error(data.detail || text);
  return data;
}
function toast(msg, ms = 3500) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.add("hidden"), ms);
}

// ── state ─────────────────────────────────────────────────────────────────────
const store = {
  get: (k, d) => { try { const v = JSON.parse(localStorage.getItem(k)); return v ?? d; } catch { return d; } },
  set: (k, v) => localStorage.setItem(k, JSON.stringify(v)),
};
const DEFAULT_FILTERS = { q: "", hood: "", priceMin: "", priceMax: "", areaMin: "", areaMax: "", aboveOnly: false, hideCheck: false };
const S = {
  view: "all", flats: [], hoods: {}, meta: {}, actions: [], jobs: [], review: null, status: null, history: null,
  filters: { ...DEFAULT_FILTERS }, moreFilters: false,
  sort: {}, unrankedReason: "",
};


function applyFilters(rows) {
  const F = S.filters, q = F.q.trim().toLowerCase();
  const n = (v) => (v === "" || v == null ? null : Number(v));
  const [pmin, pmax, amin, amax] = [n(F.priceMin), n(F.priceMax), n(F.areaMin), n(F.areaMax)];
  return rows.filter((f) =>
    (!q || [f.title, f.neighborhood, f.url, floorText(f), f.exclusion_reason].join(" ").toLowerCase().includes(q)) &&
    (!F.hood || f.neighborhood === F.hood) &&
    (pmin === null || f.price_eur >= pmin) && (pmax === null || f.price_eur <= pmax) &&
    (amin === null || f.area_sqm >= amin) && (amax === null || f.area_sqm <= amax) &&
    (!F.aboveOnly || !belowGround(f)) && (!F.hideCheck || !f.needs_check));
}

// ── shared pieces ─────────────────────────────────────────────────────────────
function flagTags(f, max = 99) {
  const seen = new Set(), tags = [];
  const add = (code) => {
    const [label, tone, explain] = FLAGS[code] || [cap(words(code)), "", ""];
    if (seen.has(label)) return;
    seen.add(label);
    tags.push(`<span class="tag ${tone}" ${explain ? `data-tip="${esc(explain)}"` : ""}>${esc(label)}</span>`);
  };
  if (f.needs_check) tags.push(`<span class="tag warn" data-tip="A verification check flagged it; see the flat page.">Needs a check</span>`);
  if (f.floor_class === "basement") add("basement");
  if (f.floor_class === "semi_basement") add("semi_basement");
  (f.risk_flags || []).filter((r) => !["basement", "semi_basement"].includes(r)).forEach(add);
  (f.evidence_warnings || []).forEach(add);
  return tags.slice(0, max).join("") + (tags.length > max ? `<span class="tag">+${tags.length - max}</span>` : "");
}
function pageTitle(title, sub, actions = "") {
  return `<div class="page-title"><div><h1>${title}</h1>${sub ? `<p>${sub}</p>` : ""}</div>${actions ? `<div class="actions">${actions}</div>` : ""}</div>`;
}
const empty = (text, ic = "search") => `<div class="empty">${icon(ic)}${text}</div>`;

// ── filters ───────────────────────────────────────────────────────────────────
function hoodOptions() {
  const names = [...new Set(S.flats.filter((f) => f.ranked).map((f) => f.neighborhood).filter(Boolean))].sort();
  return `<option value="">All neighborhoods</option>` + names.map((n) => `<option ${S.filters.hood === n ? "selected" : ""}>${esc(n)}</option>`).join("");
}
function filterBar({ extra = "" } = {}) {
  const F = S.filters;
  const chips = [];
  if (F.hood) chips.push(["hood", F.hood]);
  if (F.priceMin || F.priceMax) chips.push(["price", `€${F.priceMin || 0} – ${F.priceMax ? "€" + F.priceMax : "any"}`]);
  if (F.areaMin || F.areaMax) chips.push(["area", `${F.areaMin || 0} – ${F.areaMax || "any"} m²`]);
  if (F.aboveOnly) chips.push(["aboveOnly", "Above ground only"]);
  if (F.hideCheck) chips.push(["hideCheck", "Without “needs a check”"]);
  return `<div class="filters">
      <div class="search">${icon("search")}<input id="f-q" placeholder="Search flats, neighborhoods…" value="${esc(F.q)}"></div>
      <select id="f-hood">${hoodOptions()}</select>
      <label class="chk"><input id="f-above" type="checkbox" ${F.aboveOnly ? "checked" : ""}> Above ground only</label>
      <button class="btn secondary small" id="f-more">${icon("sliders")} ${S.moreFilters ? "Fewer" : "More"} filters</button>
      <span class="spacer"></span>
      ${extra}
    </div>
    <div class="more-filters ${S.moreFilters ? "" : "hidden"}">
      <label>Price € <input id="f-pmin" type="number" placeholder="from" value="${esc(F.priceMin)}"> – <input id="f-pmax" type="number" placeholder="to" value="${esc(F.priceMax)}"></label>
      <label>Size m² <input id="f-amin" type="number" placeholder="from" value="${esc(F.areaMin)}" style="width:76px"> – <input id="f-amax" type="number" placeholder="to" value="${esc(F.areaMax)}" style="width:76px"></label>
      <label class="chk"><input id="f-check" type="checkbox" ${F.hideCheck ? "checked" : ""}> Hide flats needing a check</label>
    </div>
    ${chips.length ? `<div class="active-chips">${chips.map(([k, l]) => `<span class="fchip">${esc(l)}<button data-clear="${k}" title="Remove">×</button></span>`).join("")}
      <button class="link-btn" id="f-clear">Clear all</button></div>` : ""}`;
}
function bindFilters(root) {
  const F = S.filters;
  const on = (id, ev, fn) => { const el = $(id, root); if (el) el.addEventListener(ev, fn); };
  const keep = (fn) => () => { fn(); render({ keepFocus: document.activeElement?.id }); };
  on("#f-q", "input", keep(() => (F.q = $("#f-q", root).value)));
  on("#f-hood", "change", keep(() => (F.hood = $("#f-hood", root).value)));
  on("#f-pmin", "input", keep(() => (F.priceMin = $("#f-pmin", root).value)));
  on("#f-pmax", "input", keep(() => (F.priceMax = $("#f-pmax", root).value)));
  on("#f-amin", "input", keep(() => (F.areaMin = $("#f-amin", root).value)));
  on("#f-amax", "input", keep(() => (F.areaMax = $("#f-amax", root).value)));
  on("#f-above", "change", keep(() => (F.aboveOnly = $("#f-above", root).checked)));
  on("#f-check", "change", keep(() => (F.hideCheck = $("#f-check", root).checked)));
  on("#f-more", "click", () => { S.moreFilters = !S.moreFilters; render(); });
  on("#f-clear", "click", () => { S.filters = { ...DEFAULT_FILTERS }; render(); });
  $$("[data-clear]", root).forEach((b) => b.addEventListener("click", () => {
    const k = b.dataset.clear;
    if (k === "price") { F.priceMin = ""; F.priceMax = ""; } else if (k === "area") { F.areaMin = ""; F.areaMax = ""; } else F[k] = DEFAULT_FILTERS[k];
    render();
  }));
}

// ── columns & table ───────────────────────────────────────────────────────────
const COLS = {
  rank: { label: "Rank", num: true, get: (f) => f._rank, html: (f) => (f._rank ? `<span class="rank">${f._rank}</span>` : "–") },
  photo: { label: "Photo", get: () => null, html: (f) => (f.photo ? `<img class="th" src="${esc(f.photo)}" loading="lazy" alt="">` : ""), nosort: true },
  neighborhood: { label: "Neighborhood", get: (f) => f.neighborhood, wrap: true, cls: "c-hood",
    html: (f) => `${esc(f.neighborhood || "–")}${f.needs_check ? ` <span class="tag warn" title="${esc((f.check_reasons || []).map(checkText).join("; "))}">Check</span>` : ""}` },
  price: { label: "Price", num: true, get: (f) => f.price_eur, fmt: eur },
  area: { label: "m²", num: true, get: (f) => f.area_sqm, fmt: (v) => num(v) },
  psqm: { label: "€/m²", num: true, cls: "c-opt", get: (f) => f.price_psqm, fmt: eur },
  floor: { label: "Floor", get: (f) => floorText(f), cls: "c-floor",
    html: (f) => `<span title="${esc(floorText(f))}">${esc(FLOORS_SHORT[f.floor_class] && !(f.floor_class === "upper" && isNum(f.floor)) ? FLOORS_SHORT[f.floor_class] : floorText(f))}</span>` },
  condition: { label: "Condition", num: true, get: (f) => f.visual_score, fmt: (v) => num(v, 1) },
  renovation: { label: "Renovation", num: true, get: (f) => f.renovation_eur, html: (f) => (f.renovation_level && f.renovation_level !== "none" ? `<span title="${esc(cap(f.renovation_level))} renovation">${eur(f.renovation_eur)}</span>` : `<span class="muted">–</span>`) },
  cost: { label: "Total cost", num: true, get: (f) => f.all_in_cost, fmt: eur },
  rent_lo: { label: "Rent", num: true, get: (f) => f.rent_lo_eur,
    html: (f) => eur(f.rent_lo_eur) + (isNum((f.costs || {}).rent_stated) ? ` <span class="tag" title="The ad says it is let now for ${eur(f.costs.rent_stated)}">let</span>` : "") },
  rent_hat: { label: "Rent (central)", num: true, get: (f) => f.rent_hat_eur, fmt: eur },
  net: { label: "Net yield", num: true, get: (f) => f.net_yield_lo, html: (f) => ypill(f.net_yield_lo) },
  before_tax: { label: "Before tax", num: true, get: (f) => f.net_yield_lo_before_tax, fmt: (v) => pct(v) },
  gross: { label: "Gross", num: true, cls: "c-opt", get: (f) => f.gross_yield, fmt: (v) => pct(v) },
  dom: { label: "Listed since", num: true, get: (f) => f.first_published || null,
    html: (f) => (f.first_published ? `<span title="${esc(isNum(f.days_on_market) ? f.days_on_market + " days ago — the flat's earliest ad" : "")}">${esc(listedDate(f.first_published))}</span>` : "–") },
  discount: { label: "vs area", num: true, cls: "c-opt", get: (f) => f.market_discount_pct, fmt: (v) => (isNum(v) ? num(v, 0) + "%" : "–") },
  confidence: { label: "Confidence", get: (f) => f.confidence },
  flags: { label: "Notes", get: (f) => (f.risk_flags || []).join(" "), html: (f) => `<div class="tags">${flagTags(f, 2)}</div>` },
  reasons: { label: "What to check", get: (f) => (f.check_reasons || []).join(", "), html: (f) => (f.check_reasons || []).map((r) => `<div class="reason-line">${icon("alert")}<span>${esc(checkText(r))}</span></div>`).join(""), wrap: true },
  reason: { label: "Why not ranked", get: (f) => f.exclusion_reason, html: (f) => `<span class="tag">${esc(reasonText(f.exclusion_reason || f.rent_unavailable_reason))}</span>` },
};
const DEALS_COLS = ["rank", "photo", "neighborhood", "net", "before_tax", "gross", "price", "psqm", "area", "floor",
  "condition", "renovation", "rent_lo", "dom", "discount"];
function renderTable(host, rows, cols, view, { defaultSort, rowClass } = {}) {
  const sort = S.sort[view] || defaultSort || {};
  const sorted = [...rows];
  if (sort.key && COLS[sort.key]) {
    const g = COLS[sort.key].get;
    sorted.sort((a, b) => {
      const x = g(a), y = g(b);
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * (sort.dir || 1);
    });
  }
  host.innerHTML = `<div class="table-card"><div class="table-wrap"><table class="grid"><thead><tr>${cols.map((k) =>
    `<th data-k="${k}" class="${COLS[k].num ? "num" : ""} ${COLS[k].cls || ""} ${sort.key === k ? "sorted" : ""}">${esc(COLS[k].label)}${sort.key === k ? (sort.dir > 0 ? " ↑" : " ↓") : ""}</th>`).join("")}</tr></thead>
    <tbody>${sorted.map((f) => `<tr data-oid="${esc(f.opportunity_id)}" class="${rowClass ? rowClass(f) : ""}">${cols.map((k) => {
      const c = COLS[k], v = c.get(f);
      return `<td class="${c.num ? "num" : ""} ${c.wrap ? "wrap" : ""} ${c.cls || ""}">${c.html ? c.html(f) : esc(c.fmt ? c.fmt(v) : v ?? "–")}</td>`;
    }).join("")}</tr>`).join("")}</tbody></table></div>
    <div class="table-foot">${sorted.length.toLocaleString("en")} flats · click a row to open it · click a column title to sort</div></div>`;
  $$("th", host).forEach((th) => th.addEventListener("click", () => {
    const k = th.dataset.k; if (COLS[k].nosort) return;
    const cur = S.sort[view] || defaultSort || {};
    S.sort[view] = { key: k, dir: cur.key === k ? -(cur.dir || 1) : (COLS[k].num ? -1 : 1) };
    render();
  }));
  $$("tbody tr", host).forEach((tr) => tr.addEventListener("click", () => openFlat(tr.dataset.oid)));
  host._export = () => exportCsv(sorted, cols, view);
}
function exportCsv(rows, cols, view) {
  const keys = cols.filter((k) => k !== "photo");
  const cell = (v) => { const s = String(v ?? ""); return /[",\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s; };
  const lines = [[...keys.map((k) => COLS[k].label), "url"].map(cell).join(",")];
  rows.forEach((f) => lines.push([...keys.map((k) => COLS[k].get(f)), f.url].map(cell).join(",")));
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob(["﻿" + lines.join("\n")], { type: "text/csv" }));
  a.download = `greeceapt-${view}-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
}

// ── views ─────────────────────────────────────────────────────────────────────
const VIEWS = {};

VIEWS.all = (main) => {
  const F = S.filters, q = F.q.trim().toLowerCase();
  const rows = S.flats.filter((f) => f.ranked)
    .map((f) => ({ ...f, _rank: f.rank_in_neighborhood }))
    .filter((f) => (!F.hood || f.neighborhood === F.hood)
      && (!q || [f.title, f.neighborhood, f.url, floorText(f)].join(" ").toLowerCase().includes(q)));
  main.innerHTML = pageTitle("Deals", `Every ranked flat. Click any column title to sort by it — net yield ${tip("net")} after every cost and tax, price, size, condition, rent and the rest. “Rank” is the flat's place in its own neighborhood.`,
      `<button class="btn secondary" id="export">${icon("download")} Export to Excel</button>`)
    + `<div class="filters"><div class="search">${icon("search")}<input id="f-q" placeholder="Search flats, neighborhoods…" value="${esc(F.q)}"></div>
       <select id="f-hood">${hoodOptions()}</select>${F.q || F.hood ? `<button class="link-btn" id="f-clear">Clear</button>` : ""}</div>
    <div id="tbl"></div>`;
  bindFilters(main);
  const host = $("#tbl", main);
  renderTable(host, rows, DEALS_COLS, "all", { defaultSort: { key: "net", dir: -1 }, rowClass: (f) => (f.needs_check ? "flag" : "") });
  $("#export", main).addEventListener("click", () => host._export());
};

VIEWS.check = (main) => {
  const rows = applyFilters(S.flats.filter((f) => f.needs_check).map((f) => ({ ...f, _rank: f.rank_in_neighborhood })));
  main.innerHTML = pageTitle("Needs a check", "Ranked flats that a safety check flagged: the ad places the flat far from where its map pin is, the price per m² is under half the neighborhood's, or it looks unfinished. They keep their rank; look at what to check before relying on it.")
    + filterBar() + `<div id="tbl"></div>`;
  bindFilters(main);
  renderTable($("#tbl", main), rows, ["rank", "photo", "neighborhood", "price", "area", "net", "reasons"], "check", { defaultSort: { key: "net", dir: -1 } });
};

VIEWS.unranked = (main) => {
  const all = S.flats.filter((f) => !f.ranked);
  const counts = {};
  all.forEach((f) => { const r = f.exclusion_reason || "other"; counts[r] = (counts[r] || 0) + 1; });
  const rows = applyFilters(all.filter((f) => !S.unrankedReason || (f.exclusion_reason || "other") === S.unrankedReason));
  main.innerHTML = pageTitle("Not ranked", "Flats that are kept but cannot be ranked, and why.")
    + `<div class="chips"><span class="chip ${!S.unrankedReason ? "on" : ""}" data-r="">All<b>${all.length}</b></span>${Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([r, n]) =>
      `<span class="chip ${S.unrankedReason === r ? "on" : ""}" data-r="${esc(r)}">${esc(reasonText(r))}<b>${n}</b></span>`).join("")}</div>`
    + filterBar() + `<div id="tbl"></div>`;
  $$(".chip", main).forEach((c) => c.addEventListener("click", () => { S.unrankedReason = c.dataset.r; render(); }));
  bindFilters(main);
  renderTable($("#tbl", main), rows, ["photo", "reason", "dom", "neighborhood", "price", "area", "floor"], "unranked", { defaultSort: { key: "price", dir: 1 } });
};

VIEWS.hoods = (main) => {
  const meta = new Map((S.meta.neighborhoods || []).map((n) => [n.name, n]));
  const names = [...new Set([...meta.keys(), ...Object.keys(S.hoods)])];
  const rows = names.map((n) => ({ name: n, ...(meta.get(n) || {}), ...(S.hoods[n] || {}) }));
  const sort = S.sort.hoods || { key: "best", dir: -1 };
  rows.sort((a, b) => { const x = a[sort.key], y = b[sort.key];
    if (x == null) return 1; if (y == null) return -1;
    return (typeof x === "number" ? x - y : String(x).localeCompare(String(y))) * sort.dir; });
  const H = [["name", "Neighborhood"], ["n", "Ranked flats"], ["listing_count", "All listings"], ["best", "Best net yield"], ["median", "Typical net yield"], ["sale_baseline_psqm", "Sale price €/m²"]];
  main.innerHTML = pageTitle("Neighborhoods", "Click a neighborhood to see its flats in Deals.")
    + `<div class="table-card"><div class="table-wrap"><table class="grid"><thead><tr>${H.map(([k, l]) => `<th data-k="${k}" class="${k === "name" ? "" : "num"} ${sort.key === k ? "sorted" : ""}">${l}${sort.key === k ? (sort.dir > 0 ? " ↑" : " ↓") : ""}</th>`).join("")}</tr></thead><tbody>
    ${rows.map((r) => `<tr data-h="${esc(r.name)}"><td><b>${esc(r.name)}</b></td><td class="num">${r.n ?? "–"}</td><td class="num">${r.listing_count ?? "–"}</td>
      <td class="num">${isNum(r.best) ? ypill(r.best) : "–"}</td><td class="num">${pct(r.median)}</td><td class="num">${eur(r.sale_baseline_psqm)}</td></tr>`).join("")}
    </tbody></table></div><div class="table-foot">${rows.length} neighborhoods</div></div>`;
  $$("th", main).forEach((th) => th.addEventListener("click", () => { const k = th.dataset.k; S.sort.hoods = { key: k, dir: sort.key === k ? -sort.dir : (k === "name" ? 1 : -1) }; render(); }));
  $$("tbody tr", main).forEach((tr) => tr.addEventListener("click", () => { S.filters.hood = tr.dataset.h; go("all"); }));
};

VIEWS.review = async (main) => {
  if (!S.review) { main.innerHTML = pageTitle("Possible duplicates") + `<div class="skeleton" style="height:240px"></div>`; S.review = (await api("/api/review")).pairs; renderNav(); }
  const holding = S.review.filter((p) => p.holds_ranking).length;
  const pairs = S.reviewAll ? S.review : S.review.filter((p) => p.holds_ranking);
  main.innerHTML = pageTitle("Possible duplicates", `Ads that look like the same flat but disagree on price, size or floor. ${holding ? `<b>${holding}</b> keep a flat out of the ranking until you decide.` : "None keeps a flat out of the ranking right now."} ${S.reviewAll ? "Showing all pairs; the others change nothing in the ranking today (the flats are not ranked for another reason, or the ads are gone)." : `The other ${S.review.length - holding} change nothing in the ranking today.`} Your decision applies at the next re-score.`,
      `<button class="btn secondary" id="rv-all">${S.reviewAll ? "Show only the ones that matter" : `Show all ${S.review.length}`}</button>`
      + (S.reviewDecided ? `<button class="btn" id="rv-rescore">${icon("refresh")} Re-score now (${S.reviewDecided} decided)</button>` : ""))
    + (pairs.length ? pairs.map((p, i) => `<div class="pair-card" data-k="${esc(p.pair_key)}">
      <div class="pair-head"><b>Pair ${i + 1} · ${esc(cap(words(p.reason)))}</b>${p.holds_ranking ? `<span class="tag warn">Blocks a flat</span>` : ""}
        <span class="tag info">${p.photo_matches ?? 0} matching photos</span>${isNum(p.description_similarity) ? `<span class="tag">Text ${pct(p.description_similarity, 0)} alike</span>` : ""}</div>
      <div class="pair">${p.sides.map((s) => `<div class="side">
        <div class="facts">${eur(s.price_eur)} · ${num(s.area_sqm)} m² · ${esc(FLOORS[s.floor_class] || "floor ?")} · ${esc(s.neighborhood || "")}</div>
        <a href="${esc(s.url)}" data-ext="1">${icon("external")} Open ad ${esc(s.url.split("/").pop())}</a>
        <div class="photos">${(s.photos || []).map((u) => `<img src="${esc(u)}" loading="lazy" alt="">`).join("")}</div>
        <div class="d">${esc((s.description || "").slice(0, 600))}</div></div>`).join("")}</div>
      <div class="pair-actions"><button class="btn" data-d="same">${icon("check")} Same flat</button><button class="btn secondary" data-d="different">Different flats</button><button class="btn ghost" data-d="skip">Decide later</button></div></div>`).join("")
    : empty(S.reviewAll ? "No pairs waiting. Nice!" : "No pair keeps a flat out of the ranking. Nice!", "check"));
  $("#rv-rescore", main)?.addEventListener("click", () => startAction("score_only"));
  $("#rv-all", main).addEventListener("click", () => { S.reviewAll = !S.reviewAll; render(); });
  bindExternal(main);
  $$(".pair-card", main).forEach((c) => $$("button[data-d]", c).forEach((b) => b.addEventListener("click", async () => {
    const key = c.dataset.k;
    if (b.dataset.d !== "skip") {
      try { await post("/api/review", { pair_key: key, decision: b.dataset.d }); S.reviewDecided = (S.reviewDecided || 0) + 1; toast(b.dataset.d === "same" ? "Saved: the same flat" : "Saved: different flats"); }
      catch (e) { toast("Not saved: " + e.message, 6000); return; }
    }
    S.review = S.review.filter((p) => p.pair_key !== key);
    renderNav(); render();
  })));
};

const ACTION_ICONS = { update_check: "search", update: "refresh", score_only: "award", rent_train: "cpu", rent_pairs: "copy", rent_vision: "photo", rent_refresh: "globe", delisting: "file" };
VIEWS.actions = (main) => {
  const groups = {};
  S.actions.forEach((a) => (groups[a.group] = groups[a.group] || []).push(a));
  const last = (key) => (S.history?.jobs || []).find((j) => j.action === key && j.ok);
  main.innerHTML = pageTitle("Tasks", "Everything runs on this Mac. Tasks that contact Spitogatos ask first and stop at once if the site refuses. Progress shows bottom-right, and a task keeps going if you close the window.")
    + `<div class="action-groups">${Object.entries(groups).map(([g, list]) => `<div class="agroup"><h3>${esc(g)}</h3>
      ${list.map((a) => { const busy = busyFor(a), l = last(a.key);
        return `<div class="action"><div class="ic">${icon(ACTION_ICONS[a.key] || "play")}</div><div class="txt"><b>${esc(a.label)}</b><span>${esc(a.description)}</span>
          <div class="meta">${a.network ? `<span class="net">${icon("globe")} contacts Spitogatos</span>` : `<span>${icon("check")} stays offline</span>`}${l ? `<span>${icon("clock")} last run ${esc(ago(l.at))}</span>` : ""}</div></div>
          <button class="btn ${busy ? "secondary" : ""} small" data-a="${esc(a.key)}" ${busy ? "disabled" : ""}>${busy ? "Running…" : `${icon("play")} Run`}</button></div>`; }).join("")}</div>`).join("")}</div>`;
  $$("button[data-a]", main).forEach((b) => b.addEventListener("click", () => startAction(b.dataset.a)));
  if (!S.history && !S.historyLoading) {
    S.historyLoading = true;
    api("/api/history").then((h) => { S.history = h; if (S.view === "actions") render(); }).catch(() => {}).finally(() => (S.historyLoading = false));
  }
};
const busyFor = (action) => S.jobs.some((j) => (j.status === "running" || j.status === "queued") && j.action === action.key);

VIEWS.history = async (main) => {
  main.innerHTML = pageTitle("History") + `<div class="skeleton" style="height:300px"></div>`;
  const H = (S.history = await api("/api/history"));
  const pairs = H.pairs;
  const mh = H.rent_model_history || [];
  main.innerHTML = pageTitle("History", "Past updates, the rent model, and how well it predicts real rents.")
    + `<h2 class="section">${icon("refresh")} Updates</h2>
    <div class="table-card"><div class="table-wrap" style="max-height:300px"><table class="grid"><thead><tr><th>When</th><th>Took</th><th>Result</th><th class="num">Ranked</th><th class="num">New flats</th><th class="num">Gone</th><th>Note</th></tr></thead><tbody>
    ${(H.updates || []).map((u) => { const c = u.counts || {}; const took = u.finished_at ? (new Date(u.finished_at) - new Date(u.started_at)) / 1000 : null;
      return `<tr><td>${when(u.started_at)}</td><td>${duration(took)}</td><td><span class="status ${u.ok ? "ok" : "failed"}">${esc(u.status)}</span></td><td class="num">${c.ranked_after ?? "–"}</td><td class="num">${c.new_ranked ?? "–"}</td><td class="num">${c.no_longer_listed ?? "–"}</td><td class="wrap muted">${esc(u.error || "")}</td></tr>`; }).join("") || `<tr><td colspan="8" class="muted">No updates yet.</td></tr>`}
    </tbody></table></div></div>
    <h2 class="section">${icon("file")} What the last update changed</h2><div class="md">${H.update_report ? markdown(H.update_report) : "<p class='muted'>No report yet.</p>"}</div>
    <h2 class="section">${icon("cpu")} Rent model ${mh.length ? `<span class="muted">${mh.map((r) => `${when(r.trained_at)}: typical error ${pct(r.random_cv_mape, 1)}`).join(" · ")}</span>` : ""}</h2>
    <div class="md">${H.rent_model_report ? markdown(H.rent_model_report) : "<p class='muted'>Not trained yet.</p>"}</div>
    <h2 class="section">${icon("copy")} Checked against real flats <span class="muted">flats advertised for sale and for rent at the same time</span></h2>
    ${pairs && pairs.n ? `<p>${pairs.n} flats: the model was off by ${pct(Math.abs(pairs.median_pct_error), 0)} ${pairs.median_pct_error < 0 ? "on the low side" : "on the high side"} (median), and the asked rent was at least the conservative rent for ${pct(pairs.lower_bound_coverage, 0)} of them.</p>
      <div class="table-card"><table class="grid"><thead><tr><th>Sale ad</th><th class="num">m²</th><th class="num">Price</th><th class="num">Asked rent</th><th class="num">Predicted</th><th class="num">Conservative</th><th class="num">Difference</th></tr></thead><tbody>
      ${pairs.flats.map((f) => `<tr><td><a href="${esc(f.sale_urls[0])}" data-ext="1">${esc(f.sale_urls[0].split("/").pop())}</a>${f.sale_urls.length > 1 ? ` <span class="muted">+${f.sale_urls.length - 1}</span>` : ""}</td><td class="num">${num(f.area_sqm)}</td><td class="num">${eur(f.price_eur)}</td><td class="num">${eur(f.rent_eur)}</td><td class="num">${eur(f.predicted_rent)}</td><td class="num">${eur(f.predicted_rent_lo)}</td><td class="num">${pct(f.pct_error, 0)}</td></tr>`).join("")}
      </tbody></table></div>` : `<p class="muted">Not run yet (Tasks → Validate on sale/rent pairs).</p>`}
    ${H.rank_history_report ? `<h2 class="section">${icon("ban")} Flats that left the market</h2><div class="md">${markdown(H.rank_history_report)}</div>` : ""}
    <h2 class="section">${icon("clock")} Tasks run from this window</h2>
    <div class="table-card"><div class="table-wrap" style="max-height:260px"><table class="grid"><thead><tr><th>When</th><th>Task</th><th>Result</th><th>Steps</th></tr></thead><tbody>
    ${(H.jobs || []).map((j) => `<tr><td>${when(j.at)}</td><td>${esc((S.actions.find((a) => a.key === j.action) || {}).label || j.action)}</td><td><span class="status ${j.ok ? "ok" : "failed"}">${j.ok ? "done" : "exit " + esc(j.exit_code)}</span></td>
      <td class="wrap muted">${Object.entries(j.stages || {}).map(([k, v]) => `${esc(k)} ${duration(v.duration_s)}`).join(" · ")}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">None yet.</td></tr>`}
    </tbody></table></div></div>`;
  $$(".grid tbody tr", main).forEach((tr) => (tr.style.cursor = "default"));
  bindExternal(main);
};

// Minimal markdown: headings, lists, tables, bold, code, paragraphs.
function markdown(src) {
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = [], lines = src.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (/^\s*\|/.test(l)) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
      i--;
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const body = rows.filter((r) => !/^\s*\|[\s:|-]+\|\s*$/.test(r));
      out.push(`<table>${body.map((r, k) => `<tr>${cells(r).map((c) => (k === 0 ? `<th>${inline(c)}</th>` : `<td>${inline(c)}</td>`)).join("")}</tr>`).join("")}</table>`);
    } else if (/^#{1,4} /.test(l)) {
      const n = l.match(/^#+/)[0].length; out.push(`<h${n + 1}>${inline(l.slice(n + 1))}</h${n + 1}>`);
    } else if (/^\s*[-*] /.test(l)) {
      const items = [];
      while (i < lines.length && /^\s*[-*] /.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*] /, ""));
      i--; out.push(`<ul>${items.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`);
    } else if (l.trim()) out.push(`<p>${inline(l)}</p>`);
  }
  return out.join("");
}

// ── flat page ─────────────────────────────────────────────────────────────────
const openExternal = (u) => window.open(u, "_blank", "noopener,width=1300,height=900");
function bindExternal(root) {
  $$("a[data-ext]", root).forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); openExternal(a.href); }));
}
async function openFlat(oid) {
  const page = $("#flat-page");
  page.classList.remove("hidden");
  document.body.classList.add("modal-open");
  page.scrollTop = 0;
  page.innerHTML = `<div class="fp"><div class="fp-head"><h2>Loading…</h2><span class="spacer"></span><button class="btn ghost" id="fp-close">${icon("x")}</button></div>
    <div class="fp-grid"><div class="skeleton" style="aspect-ratio:4/3"></div><div class="skeleton" style="height:360px"></div></div></div>`;
  $("#fp-close").onclick = closeFlat;
  let d;
  try { d = await api("/api/flat/" + encodeURIComponent(oid)); }
  catch (e) { page.innerHTML = `<div class="fp"><div class="fp-head"><h2>Not found</h2><span class="spacer"></span><button class="btn ghost" id="fp-close">${icon("x")}</button></div><div style="padding:24px">${esc(e.message)}</div></div>`; $("#fp-close").onclick = closeFlat; return; }
  const f = d.deal, L = d.listing, photos = d.photos || [];
  const sources = [...new Set([L.url, f.url, ...(f.source_urls || [])].filter(Boolean))];
  const mapUrl = isNum(L.latitude) && isNum(L.longitude) ? `https://www.google.com/maps?q=${L.latitude},${L.longitude}` : null;
  const reno = f.renovation_level && f.renovation_level !== "none";
  const C = f.costs || {};
  const keepYear = isNum(C.net) ? C.net : null;
  const done = new Set(store.get("checks_" + oid, []));
  page.innerHTML = `<div class="fp">
    <div class="fp-head"><div><div class="crumb">${esc(f.neighborhood)}${f.neighborhood_note ? ` <span class="tag info" data-tip="${esc(f.neighborhood_note)}">location inferred</span>` : ""}${f.rank_in_neighborhood ? ` · #${f.rank_in_neighborhood} of ${(S.hoods[f.neighborhood] || {}).n || "?"} in the neighborhood` : ` · ${esc(reasonText(f.exclusion_reason))}`}</div>
      <h2>${num(f.area_sqm)} m² · ${esc(floorText(f))} · ${esc(f.neighborhood)}</h2></div>
      <span class="spacer"></span>
      <span class="head-price">${eur(f.price_eur)}</span>${f.ranked ? `<span class="head-yield ${yclass(f.net_yield_lo)}">${pct(f.net_yield_lo)} net</span>` : `<span class="tag">Not ranked</span>`}
      <button class="btn" id="fp-open2">${icon("external")} Spitogatos</button>
      <button class="btn ghost" id="fp-close" title="Close (Esc)">${icon("x")}</button></div>
    <div class="fp-grid">
      <div>
        ${photos.length ? `<div class="hero-img"><img id="g-main" src="${esc(photos[0].url)}" alt="">
            <button class="nav-btn prev" id="g-prev" title="Previous (←)">${icon("left")}</button><button class="nav-btn next" id="g-next" title="Next (→)">${icon("right")}</button>
            <span class="room" id="g-room"></span><span class="counter" id="g-count"></span></div>
          <div class="strip">${photos.map((p, i) => `<div class="th"><img data-i="${i}" src="${esc(p.url)}" loading="lazy" class="${i === 0 ? "on" : ""}" alt="">${p.status && p.status !== "accepted" ? `<span class="rej">not used</span>` : ""}</div>`).join("")}</div>`
          : empty("No photos stored.", "photo")}
        <h3>About the flat</h3>
        <div class="kfacts">
          <div class="kfact">${icon("area")}<div><div class="k">Size</div><div class="v">${num(f.area_sqm)} m²${isNum(f.area_listed) ? `<small class="muted" title="The ad lists ${num(f.area_listed)} m², including an attic, terrace or storage"> of ${num(f.area_listed)} listed</small>` : ""}</div></div></div>
          <div class="kfact">${icon("layers")}<div><div class="k">Floor</div><div class="v">${esc(floorText(f))}</div></div></div>
          <div class="kfact">${icon("calendar")}<div><div class="k">Built</div><div class="v">${esc(L.year_built ?? "–")}${L.renovation_year ? ` · ren. ${esc(L.renovation_year)}` : ""}</div></div></div>
          <div class="kfact">${icon("bed")}<div><div class="k">Bedrooms</div><div class="v">${esc(L.bedrooms ?? "–")}</div></div></div>
          <div class="kfact">${icon("bolt")}<div><div class="k">Energy class</div><div class="v">${esc(L.energy_class ?? "–")}</div></div></div>
          <div class="kfact">${icon("sparkle")}<div><div class="k">Condition ${tip("condition")}</div><div class="v">${num(f.visual_score, 1)} / 10</div></div></div>
        </div>
        <h3>Description</h3><div class="desc">${esc(L.description || "No description.")}</div>
        ${f.ranked ? `<h3>The money, step by step</h3>
        <table class="money">
          <tr><td>Asking price</td><td>${eur(f.price_eur)}</td></tr>
          <tr><td>Purchase costs<small>transfer tax, notary, lawyer, agent — about 8%</small></td><td>${eur(f.cost_before_renovation - f.price_eur)}</td></tr>
          <tr><td>Renovation<small>${reno ? (((() => { const r = ["kitchen", "bathroom"].filter((k) => (f.risk_flags || []).includes(k + "_work")); return r.length ? `${cap(r.join(" and "))} job (the photos rate ${r.length > 1 ? "them" : "it"} poor), plus what the rest needs` : ""; })()) || `${esc(cap(f.renovation_level))}: condition ${num(f.visual_score, 1)} → ${num(f.visual_score_after, 1)}`) : "none needed"}</small></td><td>${eur(f.renovation_eur || 0)}</td></tr>
          <tr class="total"><td>You pay in total</td><td>${eur(f.all_in_cost)}</td></tr>
          ${isNum(C.rent_stated) && C.rent_stated >= (C.rent_model_lo || 0)
            ? `<tr><td>Rent a month ${tip("rent")}<small>the current lease (the model alone: ${eur(C.rent_model_lo)} – ${eur((C.rent_asked_hat || 0) * 0.9)})</small></td><td>${eur(f.rent_lo_eur)}</td></tr>`
            : `<tr><td>Rent a month${f.rent_basis === "post_renovation" ? " (after renovation)" : ""} ${tip("rent")}<small>signed (asking ${eur(C.rent_asked_lo)} – ${eur(C.rent_asked_hat)} less 10%), conservative – central</small></td><td>${eur(f.rent_lo_eur)} – ${eur(f.rent_hat_eur)}</td></tr>`}
          ${isNum(C.rent_stated) ? `<tr><td>Let now, says the ad<small>${C.rent_stated >= (C.rent_model_lo || 0)
            ? `a tenant pays ${eur(C.rent_stated)}, so the conservative rent is at least that (the model alone: ${eur(C.rent_model_lo)})`
            : `the lease is below the model's ${eur(C.rent_model_lo)} — ask until when; a buyer may keep the tenant at this rent`}</small></td><td>${eur(C.rent_stated)}</td></tr>` : ""}
          <tr><td>A year's rent<small>12 × ${eur(f.rent_lo_eur)} (conservative)</small></td><td>${eur(C.rent)}</td></tr>
          <tr><td>Empty months<small>half a month a year, between tenants</small></td><td>− ${eur(C.vacancy)}</td></tr>
          <tr><td>Management company<small>one month's rent a year</small></td><td>− ${eur(C.management)}</td></tr>
          <tr><td>Maintenance and repairs<small>5% of the rent</small></td><td>− ${eur(C.maintenance)}</td></tr>
          <tr><td>Insurance</td><td>− ${eur(C.insurance)}</td></tr>
          <tr><td>ENFIA property tax<small>about €3.5 per m² — the bill depends on the address</small></td><td>− ${eur(C.enfia)}</td></tr>
          <tr><td>Accountant<small>yearly E1/E2 tax return</small></td><td>− ${eur(C.accountant)}</td></tr>
          <tr><td>Income tax<small>15% on 95% of the rent collected</small></td><td>− ${eur(C.income_tax)}</td></tr>
          <tr class="total"><td>You keep a year ${tip("net")}</td><td>${eur(keepYear)} · ${pct(f.net_yield_lo)}</td></tr>
          <tr><td>Before income tax ${tip("before_tax")}</td><td>${eur(C.before_tax)} · ${pct(f.net_yield_lo_before_tax)}</td></tr>
        </table>` : ""}
        ${C.if_renovated ? (() => { const R = C.if_renovated, d = R.net_yield_lo - f.net_yield_lo; return `
        <h3>If you renovate instead</h3>
        <p class="muted">The ranking keeps the tenant (no renovation). If the tenant leaves and you renovate, then let the flat again:</p>
        <table class="money">
          <tr><td>Renovation<small>${esc(cap(R.renovation_level))}: condition ${num(f.visual_score, 1)} → ${num(R.condition_after, 1)}</small></td><td>${eur(R.renovation_eur)}</td></tr>
          <tr class="total"><td>You pay in total</td><td>${eur(R.all_in_cost)}</td></tr>
          <tr><td>Rent a month after renovation<small>the model, conservative – central, no tenant in place</small></td><td>${eur(R.rent_lo)} – ${eur(R.rent_hat)}</td></tr>
          <tr class="total"><td>You keep a year</td><td>${eur(R.net)} · ${pct(R.net_yield_lo)}</td></tr>
          <tr><td>Compared with keeping the tenant</td><td class="${d >= 0 ? "pos" : "neg"}">${d >= 0 ? "+" : "−"}${num(Math.abs(d) * 100, 2)} points</td></tr>
        </table>`; })() : ""}
        <h3>All ads of this flat</h3><div class="ads">${sources.map((u) => `<a href="${esc(u)}" data-ext="1">${icon("external")} ${esc(u)}</a>`).join("")}</div>
      </div>
      <div class="summary">
        <div class="sum-card">
          <div class="price">${eur(f.price_eur)}</div>
          <div class="sub">${eur(f.price_psqm)}/m²${isNum(f.market_discount_pct) ? ` · ${num(Math.abs(f.market_discount_pct), 0)}% ${f.market_discount_pct >= 0 ? "below" : "above"} the area's price` : ""}${f.first_published ? ` · listed since ${esc(listedDate(f.first_published))}${isNum(f.days_on_market) ? ` (${f.days_on_market} days)` : ""}` : ""}</div>
          ${f.ranked ? `<div class="big-yield"><span class="v">${pct(f.net_yield_lo)}</span><span class="l">net a year, after all costs and tax ${tip("net")}</span></div>
          <p class="plain">Pay about <b>${eur(f.all_in_cost)}</b> in total, let it for at least <b>${eur(f.rent_lo_eur)}</b> a month, and keep about <b>${eur(keepYear)}</b> a year after the management company, every cost and the tax.</p>
          <div class="ygrid"><div><div class="k">Before tax</div><div class="v">${pct(f.net_yield_lo_before_tax)}</div></div>
            <div><div class="k">Rent a month</div><div class="v">${eur(f.rent_lo_eur)}</div></div>
            <div><div class="k">Gross, before costs</div><div class="v">${pct(f.gross_yield)}</div></div></div>`
          : `<div class="alert-box">${icon("ban")}<div><b>Not ranked: ${esc(reasonText(f.exclusion_reason || f.rent_unavailable_reason))}</b><br>${esc(REASON_EXPLAIN[f.exclusion_reason] || "There is not enough to estimate its rent and yield.")}</div></div>`}
        </div>
        <div class="sum-actions">
          <button class="btn" id="fp-open">${icon("external")} Open on Spitogatos</button>
          ${mapUrl ? `<button class="btn secondary" id="fp-map">${icon("pin")} Map</button>` : ""}
        </div>
        <div class="tags">${flagTags(f)}${f.confidence && f.confidence !== "unranked" ? `<span class="tag ${f.confidence === "high" ? "good" : f.confidence === "low" ? "warn" : ""}" data-tip="How solid the data behind this flat is.">${esc(cap(f.confidence))} confidence</span>` : ""}</div>
        ${(f.check_reasons || []).length ? `<div class="alert-box">${icon("alert")}<div><b>Check before trusting this one</b><br>${f.check_reasons.map((r) => esc(checkText(r))).join("<br>")}</div></div>` : ""}
        ${(d.checklist || []).length ? `<div class="sum-card"><b>Before you buy</b> <span class="muted">— filled in from the ad, the photos and the rentals nearby; tick when you have confirmed it</span>
          <div class="prefill">${d.checklist.map((it) => `<div class="pf ${it.status}"><label><input type="checkbox" data-c="${esc(it.key)}" ${done.has(it.key) ? "checked" : ""}>
            <span class="pf-ic">${it.status === "found" ? icon("check") : it.status === "warn" ? icon("alert") : "?"}</span><b class="${done.has(it.key) ? "done" : ""}">${esc(it.label)}</b></label>
            <div class="pf-note">${esc(it.note)}</div>
            ${(it.comparables || []).length ? `<div class="comps">${it.comparables.map((c) => `<a href="${esc(c.url)}" data-ext="1">${eur(c.rent_eur)}/mo · ${num(c.area_sqm)} m² · ${esc(FLOORS[c.floor_class] || "")} · ${num(c.distance_m)} m${c.furnished ? " · furnished" : ""}</a>`).join("")}</div>` : ""}</div>`).join("")}</div></div>` : ""}
      </div>
    </div></div>`;
  $("#fp-close").onclick = closeFlat;
  $("#fp-open").onclick = () => openExternal(L.url || f.url);
  $("#fp-open2").onclick = () => openExternal(L.url || f.url);
  if (mapUrl) $("#fp-map").onclick = () => openExternal(mapUrl);
  bindExternal(page);
  $$(".prefill input", page).forEach((cb) => cb.addEventListener("change", () => {
    const key = cb.dataset.c;
    cb.checked ? done.add(key) : done.delete(key);
    store.set("checks_" + oid, [...done]);
    cb.parentElement.querySelector("b").classList.toggle("done", cb.checked);
  }));
  let cur = 0;
  const show = (i) => {
    if (!photos.length) return;
    cur = (i + photos.length) % photos.length;
    $("#g-main").src = photos[cur].url;
    $$(".strip img", page).forEach((im) => im.classList.toggle("on", Number(im.dataset.i) === cur));
    const p = photos[cur];
    $("#g-count").textContent = `${cur + 1} / ${photos.length}`;
    $("#g-room").textContent = p.status && p.status !== "accepted" ? "Not used by the image model" : p.room ? `${cap(p.room)} · condition ${num(p.score, 1)}` : "";
    $("#g-room").classList.toggle("hidden", !$("#g-room").textContent);
    $$(".strip img", page)[cur]?.scrollIntoView({ block: "nearest", inline: "nearest" });
  };
  $$(".strip img", page).forEach((im) => im.addEventListener("click", () => show(Number(im.dataset.i))));
  if (photos.length) { $("#g-prev").onclick = () => show(cur - 1); $("#g-next").onclick = () => show(cur + 1); show(0); }
  document.onkeydown = (e) => {
    if ($("#flat-page").classList.contains("hidden")) return;
    if (e.key === "Escape") closeFlat();
    if (e.key === "ArrowRight") show(cur + 1);
    if (e.key === "ArrowLeft") show(cur - 1);
  };
}
function closeFlat() { $("#flat-page").classList.add("hidden"); $("#flat-page").innerHTML = ""; document.body.classList.remove("modal-open"); }
$("#flat-page").addEventListener("click", (e) => { if (e.target.id === "flat-page") closeFlat(); });

// ── tasks ─────────────────────────────────────────────────────────────────────
function dialog(html) {
  const d = $("#dialog"); d.innerHTML = `<div class="dialog">${html}</div>`; d.classList.remove("hidden");
  return new Promise((resolve) => {
    $$("[data-r]", d).forEach((b) => b.addEventListener("click", () => {
      const values = {}; $$("input[name]", d).forEach((i) => (values[i.name] = i.value));
      d.classList.add("hidden"); resolve(b.dataset.r === "ok" ? values : null);
    }));
  });
}
async function startAction(key) {
  const a = S.actions.find((x) => x.key === key);
  if (!a) return;
  let params = {};
  if (a.network || (a.params || []).length) {
    const values = await dialog(`<h3>${esc(a.label)}</h3><p>${esc(a.description)}</p>
      ${(a.params || []).map((p) => `<label>${esc(p.label)} <input name="${esc(p.name)}" type="number" value="${esc(p.default ?? "")}"></label>`).join("")}
      ${a.network ? `<div class="net">${icon("globe")}<div><b>This contacts Spitogatos.</b> ${esc(a.network)}</div></div>` : ""}
      <div class="row"><button class="btn ghost" data-r="cancel">Cancel</button><button class="btn" data-r="ok">${icon("play")} Start</button></div>`);
    if (!values) return;
    params = values;
  }
  try {
    await post("/api/jobs", { action: key, params, confirmed: Boolean(a.network) });
    toast(`${a.label} started — follow it bottom-right`);
    S.tasksOpen = true;
    pollJobs(true);
  } catch (e) { toast(`${a.label}: ${e.message}`, 6000); }
}

let jobsTimer = null;
const lastStatuses = {}, dismissed = new Set(store.get("dismissed_jobs", []));
async function pollJobs(soon = false) {
  clearTimeout(jobsTimer);
  try { S.jobs = await api("/api/jobs"); } catch { /* server gone */ }
  for (const j of S.jobs) {
    const was = lastStatuses[j.id];
    if (was && was !== j.status && ["ok", "failed", "stopped", "blocked"].includes(j.status)) {
      toast(`${j.label}: ${j.status === "ok" ? "done" : j.status}${j.message ? " — " + j.message : ""}`, 6000);
      S.history = null;
      if (["score_only", "update", "rent_train", "rent_pairs", "delisting", "rent_refresh"].includes(j.action)) refreshData();
    }
    lastStatuses[j.id] = j.status;
  }
  renderTasks();
  const active = S.jobs.some((j) => j.status === "running" || j.status === "queued");
  jobsTimer = setTimeout(pollJobs, active || soon ? 1500 : 6000);
  if (S.view === "actions") VIEWS.actions($("#main"));
}
function jobLine(j) {
  const p = j.progress || {};
  const counter = isNum(p.done) && isNum(p.total) && p.total ? ` ${p.done.toLocaleString("en")} of ${p.total.toLocaleString("en")}` : "";
  return { p, frac: j.status === "ok" ? 1 : p.fraction || 0, step: `${p.stage || "Starting"}${counter}`, left: p.eta_s >= 1 ? `about ${duration(p.eta_s)} left` : "finishing…" };
}
function renderTasks() {
  const box = $("#tasks"), pill = $("#tasks-pill"), panel = $("#tasks-panel");
  const recent = S.jobs.filter((j) => !dismissed.has(j.id) && (j.status === "running" || j.status === "queued" || (j.finished_at && Date.now() - new Date(j.finished_at) < 15 * 60 * 1000)));
  box.classList.toggle("hidden", !recent.length);
  if (!recent.length) return;
  const running = recent.filter((j) => j.status === "running");
  if (running.length) {
    const { frac, step, left } = jobLine(running[0]);
    pill.innerHTML = `<span class="spin"></span><span>${esc(running[0].label)} · ${esc(step)} · ${Math.round(frac * 100)}% · ${esc(left)}</span><span class="mini"><div style="width:${Math.round(frac * 100)}%"></div></span>${running.length > 1 ? `<span>+${running.length - 1}</span>` : ""}`;
  } else {
    const last = recent[0];
    pill.innerHTML = `${last.status === "ok" ? `<span class="ok">${icon("check")}</span>` : icon("alert")}<span>${esc(last.label)} ${last.status === "ok" ? "finished" : esc(last.status)}${recent.length > 1 ? ` · ${recent.length} tasks` : ""}</span>`;
  }
  panel.classList.toggle("hidden", !S.tasksOpen);
  const openLogs = new Set($$(".job details[open]", panel).map((d) => d.dataset.id));
  panel.innerHTML = `<div class="ph"><span>Tasks</span><button class="btn ghost small" id="tasks-hide">Hide</button></div>` + recent.map((j) => {
    const { p, frac, step, left } = jobLine(j);
    const meta = j.status === "running" ? `<span>${esc(step)}${p.stages > 1 ? ` · step ${p.stage_index + 1} of ${p.stages}` : ""}</span><span><b>${esc(left)}</b></span>`
      : j.status === "queued" ? `<span>${esc(j.message || "Waiting for another task to finish")}</span>` : `<span>${esc(j.message || (j.status === "ok" ? "Finished" : j.status))}</span><span>took ${duration(p.elapsed_s)}</span>`;
    return `<div class="job ${j.status}"><div class="job-top"><b>${esc(j.label)}</b><span class="status ${j.status}">${j.status === "ok" ? "done" : esc(j.status)}</span>
        ${j.status === "running" || j.status === "queued" ? `<button class="btn danger small" data-stop="${j.id}">Stop</button>` : `<button class="btn ghost small" data-dismiss="${j.id}" title="Remove">${icon("x")}</button>`}</div>
      <div class="bar"><div style="width:${Math.round(frac * 100)}%"></div></div>
      <div class="job-meta">${meta}</div>
      <details data-id="${j.id}" ${openLogs.has(j.id) ? "open" : ""}><summary>Show log</summary><pre>${esc((j.tail || []).join("\n"))}</pre>
        <a href="/api/jobs/${j.id}/log" target="_blank" style="font-size:12px">Full log</a></details></div>`;
  }).join("");
  $("#tasks-hide", panel).onclick = () => { S.tasksOpen = false; renderTasks(); };
  $$("[data-stop]", panel).forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Stop this task? If an update is stopped half-way, the previous ranking stays.")) return;
    try { await post(`/api/jobs/${b.dataset.stop}/stop`); toast("Stopping…"); } catch (e) { toast(e.message); }
  }));
  $$("[data-dismiss]", panel).forEach((b) => b.addEventListener("click", () => { dismissed.add(b.dataset.dismiss); store.set("dismissed_jobs", [...dismissed].slice(-50)); renderTasks(); }));
  $$("pre", panel).forEach((pre) => (pre.scrollTop = pre.scrollHeight));
}
$("#tasks-pill").addEventListener("click", () => { S.tasksOpen = !S.tasksOpen; renderTasks(); });

// ── navigation & status ───────────────────────────────────────────────────────
const NAV = [
  ["Flats", [["all", "list", "Deals"]]],
  ["To review", [["check", "alert", "Needs a check"], ["review", "copy", "Duplicates"], ["unranked", "ban", "Not ranked"]]],
  ["More", [["hoods", "pin", "Neighborhoods"], ["actions", "play", "Tasks"], ["history", "history", "History"]]],
];
function renderNav() {
  const counts = {
    check: S.flats.filter((f) => f.needs_check).length || "",
    review: (S.review || []).filter((p) => p.holds_ranking).length || "",
    unranked: S.flats.filter((f) => !f.ranked).length || "",
    all: S.flats.filter((f) => f.ranked).length || "",
  };
  $("#nav").innerHTML = NAV.map(([group, items]) => (group ? `<div class="group">${group}</div>` : "") + items.map(([v, ic, label]) =>
    `<a data-view="${v}" title="${esc(label)}" class="${S.view === v ? "active" : ""}">${icon(ic)}<span>${label}</span>${counts[v] ? `<span class="count ${v === "review" ? "alert" : ""}">${counts[v]}</span>` : ""}</a>`).join("")).join("");
  $$("#nav a").forEach((a) => a.addEventListener("click", () => go(a.dataset.view)));
}
function renderSideStatus() {
  const s = S.status || {};
  const up = (s.ollama || {}).up;
  const lastLine = (s.last_update || "").split("\n")[0].replace(/^Last update:\s*/, "").split(" — ")[0];
  $("#side-status").innerHTML = `
    <div class="status-line" title="${esc((s.ollama || {}).models?.join(", ") || "")}"><span class="dot ${up ? "on" : "off"}"></span>${up ? "Image model ready" : "Image model off — start Ollama"}</div>
    <div class="status-line" title="${esc(s.last_update || "")}"><span class="dot"></span>Data from ${esc(lastLine || "never")}</div>
    <div class="status-line"><span class="dot"></span>Ranking built ${esc(ago(S.meta.built_at))}</div>
    ${(s.outside_pipeline || []).length ? `<div class="status-line warn-line" title="${esc(s.outside_pipeline.join("\n"))}"><span class="dot busy"></span>An update is running (started outside this window)</div>` : ""}
    ${sideNotices().map((n, i) => `<a class="side-notice" data-n="${i}" title="${esc(n.text)}">${icon(n.icon)}<span>${esc(n.label)}</span></a>`).join("")}`;
  $$(".side-notice").forEach((el) => el.addEventListener("click", () => {
    const n = sideNotices()[Number(el.dataset.n)];
    n.go ? go(n.go) : startAction(n.action);
  }));
}
// What used to be the Overview's to-do list, as short links in the side bar.
function sideNotices() {
  const s = S.status || {}, out = [];
  (s.notices || []).forEach((n) => {
    if (n.action === "rent_train") out.push({ icon: "cpu", label: "Retrain the rent model", text: n.text, action: "rent_train" });
    if (n.action === "score_only") out.push({ icon: "refresh", label: "Re-score the ranking", text: n.text, action: "score_only" });
  });
  const holding = (S.review || []).filter((p) => p.holds_ranking).length;
  if (holding) out.push({ icon: "copy", label: `${holding} duplicate pair${holding === 1 ? "" : "s"} to decide`, text: "They keep a flat out of the ranking until you decide.", go: "review" });
  if (s.ollama && !s.ollama.up) out.push({ icon: "alert", label: "Start Ollama for updates", text: "The image model is not running.", go: "actions" });
  return out;
}
async function refreshStatus() {
  const wasBusy = (S.status?.outside_pipeline || []).length > 0;
  try { S.status = await api("/api/status"); } catch { S.status = { last_update: "Server not reachable." }; }
  const busy = (S.status.outside_pipeline || []).length > 0;
  clearTimeout(refreshStatus.timer);
  refreshStatus.timer = setTimeout(refreshStatus, busy ? 15000 : 30000);
  if (wasBusy && !busy) { toast("The update finished — loading the new ranking", 5000); return refreshData(); }
  // A ranking published from the terminal (./update, a re-score) while this window was open
  if (S.meta?.built_at && S.status.ranking_built_at && S.status.ranking_built_at !== S.meta.built_at) {
    toast("A new ranking was built — loading it", 5000);
    return refreshData();
  }
  renderSideStatus();
}
async function refreshData() {
  const [data, meta, actions] = await Promise.all([api("/api/flats"), api("/api/meta"), api("/api/actions")]);
  S.flats = data.flats; S.hoods = data.neighborhoods; S.meta = meta; S.actions = actions;
  S.review = null;
  renderNav();
  render();
  await refreshStatus();
  api("/api/review").then((r) => { S.review = r.pairs; renderNav(); renderSideStatus(); }).catch(() => {});
}
function go(view) {
  S.view = view;
  renderNav();
  $("#main").scrollTop = 0;
  render();
}
function render({ keepFocus } = {}) {
  const main = $("#main");
  const sel = keepFocus ? $("#" + keepFocus) : null;
  const caret = sel && "selectionStart" in sel ? sel.selectionStart : null;
  const scroll = main.scrollTop;
  Promise.resolve(VIEWS[S.view](main)).then(() => {
    if (keepFocus) {
      const el = $("#" + keepFocus);
      if (el) { el.focus(); if (caret !== null && el.setSelectionRange) try { el.setSelectionRange(caret, caret); } catch {} }
      main.scrollTop = scroll;
    }
  }).catch((e) => { main.innerHTML = empty(`Something went wrong: ${esc(e.message)}`, "alert"); });
}
$("#quit").addEventListener("click", async () => {
  const running = S.jobs.filter((j) => j.status === "running");
  if (!confirm(running.length ? `Close GreeceApt? ${running.map((j) => j.label).join(", ")} will keep running in the background.` : "Close GreeceApt?")) return;
  try { await post("/api/quit"); } catch {}
  document.body.innerHTML = `<div style="padding:60px;font-size:16px;grid-column:1/-1">GreeceApt is closed. You can close this window; run <code>./gui</code> to open it again.</div>`;
});

renderNav();
refreshData().catch((e) => { $("#main").innerHTML = empty(`Could not load data: ${esc(e.message)}`, "alert"); });
pollJobs();
