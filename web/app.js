// Signal web app (beta): sign-in, quiz, feed, problem pages, saved, clips, areas. Preact + htm, no build step.
// Styled to the signal-design handoff (2026-10-30). The feed never says why an item is shown, and the areas
// list never says how an area got there.
import { html, render, useState, useEffect, useRef, useCallback } from "/static/vendor/preact-htm.js";

// --- API ----------------------------------------------------------------------------------------

class HttpError extends Error {
  constructor(status, detail) { super(detail || `HTTP ${status}`); this.status = status; }
}

async function api(path, { method = "GET", body, keepalive = false } = {}) {
  const r = await fetch(path, {
    method, credentials: "same-origin", keepalive,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401 && path !== "/auth/me") location.reload();     // session ended: back to sign-in
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail; } catch { /* not JSON */ }
    throw new HttpError(r.status, typeof detail === "string" ? detail : "");
  }
  return r.json();
}

const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== undefined && v !== null)).toString();

function sessionId() {
  try {
    let id = sessionStorage.getItem("signal_session_id");
    if (!id) { id = crypto.randomUUID(); sessionStorage.setItem("signal_session_id", id); }
    return id;
  } catch { return (window.__sid ||= crypto.randomUUID()); }
}

// --- Events: batched, idempotent (client_event_id), flushed every few seconds and on leaving ------

const events = { userId: null, queue: [], timer: null };

function track(type, item, extra = {}) {
  if (!events.userId) return;
  events.queue.push({
    client_event_id: crypto.randomUUID(), session_id: sessionId(), type,
    item_type: item.item_type || "post", item_id: String(item.item_id),
    at: new Date().toISOString(),
    ...(item.position !== undefined ? { position: item.position } : {}),
    ...(item.impression_id !== undefined ? { impression_id: item.impression_id } : {}),
    ...extra,
  });
  clearTimeout(events.timer);
  events.timer = setTimeout(flush, 3000);
}

function flush(keepalive = false) {
  if (!events.queue.length || !events.userId) return;
  const batch = events.queue.splice(0, 200);
  api("/events", { method: "POST", keepalive, body: { user_id: events.userId, events: batch } })
    .catch((e) => { if (e.status !== 422) events.queue.unshift(...batch); });  // retry later; ids dedupe
}
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flush(true); });

// --- Helpers ------------------------------------------------------------------------------------

const safeUrl = (u) => (typeof u === "string" && /^https?:\/\//i.test(u) ? u : null);

function ago(iso) {
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 3600) return `${Math.max(1, Math.round(d / 60))}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  if (d < 86400 * 30) return `${Math.round(d / 86400)}d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function sourceName(src) {
  const [family, rest] = String(src || "").split(":");
  const names = { hn: "Hacker News", x: "X", jetstream: "Bluesky", stackexchange: "Stack Exchange",
                  sam_gov: "SAM.gov", ted: "EU tenders", uk_tenders: "UK tenders", federal_register: "Federal Register",
                  regulations_gov: "Regulations.gov", job_boards: "Job post", github: "GitHub", edgar: "SEC EDGAR",
                  courtlistener: "CourtListener", openfda: "FDA", eurlex: "EU law", legiscan: "LegiScan",
                  openstates: "Open States", mastodon: "Mastodon", lemmy: "Lemmy", app_reviews: "WordPress.org",
                  huggingface: "Hugging Face", papers: "arXiv" };
  if (family === "rss" || family === "classic_forum" || family === "brave") return rest || family;
  return names[family] || family;
}

const TYPE_LABEL = { pain: "Problem", paid_demand: "Paid demand", tool_gap: "Tool gap", change: "Change", idea: "Idea",
                     builder: "Builder" };

const CURRENCY = { USD: "$", GBP: "£", EUR: "€" };

function money(m) {
  if (!m || m.amount_min == null || !m.currency) return null;
  const cur = m.currency || "";
  const fmt = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k`
                      : n.toLocaleString());
  const amt = m.amount_max && m.amount_max !== m.amount_min ? `${fmt(m.amount_min)}–${fmt(m.amount_max)}` : fmt(m.amount_min);
  if (!(m.currency in CURRENCY) && !/^[A-Z]{3}$/.test(m.currency)) return null;
  const per = m.period && m.period !== "once" ? `/${m.period}` : "";
  return `${CURRENCY[cur] ?? cur + " "}${amt}${per}`;
}

// X evidence must be shown as X's own embed (Developer Agreement: no reworded X content).
let xScript = null;
function XEmbed({ url }) {
  const ref = useRef();
  useEffect(() => {
    xScript ||= new Promise((ok) => {
      const s = document.createElement("script");
      s.src = "https://platform.twitter.com/widgets.js"; s.async = true; s.onload = ok;
      document.head.appendChild(s);
    });
    xScript.then(() => window.twttr?.widgets?.load(ref.current));
  }, [url]);
  return html`<div class="x-embed" ref=${ref}>
    <blockquote class="twitter-tweet" data-dnt="true"><a href=${safeUrl(url)}>A post on X</a></blockquote></div>`;
}

// --- Shared viewer state: what's saved, the same on every page ----------------------------------

const keyOf = (it) => `${it.item_type || "post"}/${it.item_id}`;
const savedStore = { keys: new Set(), subs: new Set() };
function markSaved(item) {
  if (savedStore.keys.has(keyOf(item))) return;
  track("save", item);
  savedStore.keys.add(keyOf(item));
  savedStore.subs.forEach((f) => f());
}
function useSaved(item) {
  const [, tick] = useState(0);
  useEffect(() => { const f = () => tick((n) => n + 1); savedStore.subs.add(f); return () => savedStore.subs.delete(f); }, []);
  return savedStore.keys.has(keyOf(item));
}

// --- Icons (24px outline in the rail, 20px in action rows) --------------------------------------

const Logo = () => html`<svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true">
  <rect x="1" y="16" width="3.4" height="6" rx="1.2" fill="#141414"/><rect x="6" y="12.5" width="3.4" height="9.5" rx="1.2" fill="#141414"/>
  <rect x="11" y="9" width="3.4" height="13" rx="1.2" fill="#141414"/><rect x="16" y="5.5" width="3.4" height="16.5" rx="1.2" fill="#141414"/>
  <rect x="20.6" y="2" width="3.4" height="20" rx="1.2" fill="#e8590c"/></svg>`;
const NAV_ICON = {
  feed: html`<path d="M3.5 10.5L12 4l8.5 6.5V20h-5.5v-6h-6v6H3.5z"/>`,
  saved: html`<path d="M6 3.5h12v17l-6-4.2-6 4.2z"/>`,
  clips: html`<path d="M8 4.5h8M6 4.5h12v15H6z"/><path d="M9 9.5h6M9 13h6M9 16.5h3.5"/>`,
  areas: html`<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1"/>`,
};
const NavIcon = ({ name, on }) => html`<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width=${on ? 2 : 1.6} aria-hidden="true">${NAV_ICON[name]}</svg>`;
const Bookmark = ({ on }) => html`<svg width="20" height="20" viewBox="0 0 20 20" fill=${on ? "#141414" : "none"}
  stroke="#141414" stroke-width="1.6" aria-hidden="true"><path d="M5 2.8h10v14.4l-5-3.6-5 3.6z"/></svg>`;
const OpenIcon = () => html`<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6"
  aria-hidden="true"><path d="M3 4.5h14v9H9l-4 3v-3H3z"/></svg>`;
const NotIcon = () => html`<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6"
  aria-hidden="true"><circle cx="10" cy="10" r="7.5"/><path d="M5 15L15 5"/></svg>`;
const BackIcon = () => html`<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="1.7"
  aria-hidden="true"><path d="M11 4L6 9l5 5"/></svg>`;
const OutIcon = () => html`<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"
  aria-hidden="true"><path d="M9 3h4v4M13 3L7 9M11 9.5V13H3V5h3.5"/></svg>`;

// --- Signals: glyph and strip --------------------------------------------------------------------
// Pain, paid demand, tool gap and change are post counts in the item's problem. Size is a market-size
// figure Signal doesn't measure yet, so it is never lit.

const SIGNALS = [["pain", "Pain"], ["paid_demand", "Paid"], ["tool_gap", "Tool gap"], ["change", "Change"], ["size", "Size"]];
const litCount = (s) => (s ? SIGNALS.filter(([k]) => s[k] > 0).length : 0);
const strength = (s) => `Found in ${litCount(s)} of 5 signal types`;

function Glyph({ signals, size = 44 }) {
  const step = size === 56 ? [10, 5] : size === 36 ? [6, 3.5] : [8, 4];
  const lit = (k) => !!signals && signals[k] > 0;
  return html`<span class=${`glyph g${size}`} role="img" aria-label=${signals ? strength(signals) : "Inferred problem"}>
    ${SIGNALS.map(([k], i) => html`<i class=${lit(k) ? "on" : ""} style=${`height:${step[0] + i * step[1]}px`}></i>`)}</span>`;
}

function Strip({ signals }) {
  return html`<dl class="strip" aria-label=${strength(signals)}>${SIGNALS.map(([k, label]) => html`<div>
    <span class=${"bar" + (signals[k] > 0 ? " on" : "")} aria-hidden="true"></span>
    <dt>${label}</dt><dd class=${"num" + (signals[k] > 0 ? "" : " none")}>${signals[k] > 0 ? signals[k] : "—"}</dd></div>`)}</dl>`;
}

// --- Helpers ------------------------------------------------------------------------------------

function industryOf(it) {
  const path = it.niche_path || [];
  return { industry: path[0] || "", rest: path.slice(1).join(", ") };
}
// A self-described role, without a leading article or a trailing category code ("Drug Industry - C0022").
const roleText = (r) => (r || "").replace(/^(an?|the)\s+/i, "").replace(/\s+-\s+[A-Z]{0,3}\d+$/, "").trim();
function whoLine(it) {
  const { industry, rest } = industryOf(it);
  const who = roleText(it.who) || rest;
  return html`<b>${industry}</b>${who && html`<span>, ${who}</span>`}`;
}
function findingLine(it) {
  if (it.kind === "inferred") {
    const n = (it.evidence?.length || 0) + (it.evidence_more || 0);
    return `Inferred by Signal from ${n} pieces of evidence${it.confidence ? `, ${it.confidence} confidence` : ""}`;
  }
  const m = money(it.money);
  return [TYPE_LABEL[it.post_type] ? `${TYPE_LABEL[it.post_type]} on ${sourceName(it.source)}` : sourceName(it.source), m]
    .filter(Boolean).join(", ");
}
const shortAgo = (iso) => ago(iso).replace(" ago", "");
const plural = (n, w) => `${n.toLocaleString()} ${w}${n === 1 ? "" : "s"}`;

function Evidence({ items, compact = false }) {
  return html`<ul class=${"evidence" + (compact ? " card-evidence" : "")}>${items.map((e) => html`<li>
    ${e.embed ? html`<${XEmbed} url=${e.embed} />` : html`
      <div>${e.what_it_shows}</div>
      <div class="sub">${safeUrl(e.url) ? html`<a href=${safeUrl(e.url)} target="_blank" rel="noopener noreferrer"
        style="color:var(--muted)">${sourceName(e.source)}</a>` : sourceName(e.source)}</div>`}
  </li>`)}</ul>`;
}

// --- Shell: rail, mobile top bar and menu sheet --------------------------------------------------

const NAV = [["feed", "Home"], ["saved", "Saved"], ["clips", "Clips"], ["areas", "Your areas"]];
const initials = (email) => (email || "?").split("@")[0].split(/[._-]+/).filter(Boolean).slice(0, 2)
  .map((p) => p[0].toUpperCase()).join("") || "?";

function signOut() {
  flush();
  api("/auth/sign-out", { method: "POST" }).catch(() => {}).finally(() => { location.hash = ""; location.reload(); });
}

function MeRow({ me }) {
  return html`<div class="me-row"><span class="avatar">${initials(me.email)}</span>
    <span class="who"><b>${me.email}</b><span>Signed in</span></span></div>`;
}

function Rail({ me, tab }) {
  const [sheet, setSheet] = useState(false);
  const go = (k) => { setSheet(false); location.hash = k; };
  const items = NAV.map(([k, label]) => html`<a class="nav" href=${"#" + k} aria-current=${tab === k ? "page" : undefined}
    onClick=${(e) => { e.preventDefault(); go(k); }}><${NavIcon} name=${k} on=${tab === k} />${label}</a>`);
  return html`<nav class="rail" aria-label="Main">
    <a class="logo" href="#feed" onClick=${(e) => { e.preventDefault(); go("feed"); }}><${Logo} /><span>Signal</span></a>
    <button class="menu-btn act" type="button" aria-label="Open menu" onClick=${() => setSheet(true)}>
      <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="#141414" stroke-width="1.7" aria-hidden="true">
        <path d="M3 6h14M3 10h14M3 14h14"/></svg></button>
    <div class="rail-nav">${items}</div>
    <div class="rail-foot"><${MeRow} me=${me} />
      <button class="nav" type="button" onClick=${signOut} style="font-size:14px;color:var(--muted);height:40px">Sign out</button></div>
    ${sheet && html`<div class="sheet" role="dialog" aria-modal="true" aria-label="Menu">
      <div class="sheet-top"><span class="logo"><${Logo} /><span>Signal</span></span>
        <button class="act icon" type="button" aria-label="Close menu" onClick=${() => setSheet(false)}>
          <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="#141414" stroke-width="1.7" aria-hidden="true">
            <path d="M5 5l10 10M15 5L5 15"/></svg></button></div>
      <div style="display:flex;flex-direction:column;gap:2px">${items}</div>
      <${MeRow} me=${me} />
      <button class="pill" type="button" onClick=${signOut}>Sign out</button>
    </div>`}
  </nav>`;
}

// --- Sign-in ------------------------------------------------------------------------------------

function SoloHead({ right }) {
  return html`<header class="solo-head"><span class="logo" style="padding:0"><${Logo} /><span>Signal</span></span>
    ${right && html`<span class="num note">${right}</span>`}</header>`;
}

function SignIn() {
  const [email, setEmail] = useState("");
  const [state, setState] = useState("idle");   // idle | sending | sent | error
  const [error, setError] = useState("");
  const submit = async (e) => {
    e.preventDefault();
    setState("sending");
    try {
      await api("/auth/start", { method: "POST", body: { email } });
      setState("sent");
    } catch (err) {
      setError(err.status === 422 ? "That doesn't look like an email address." : "Couldn't send the link. Try again.");
      setState("error");
    }
  };
  return html`<${SoloHead} /><main class="solo signin" style="padding-top:12vh">
    <div><h1>Real problems worth building</h1>
      <p class="intro">Signal reads forums, job boards, tenders, reviews and new rules, and shows you the problems people
        run into every week, matched to what you know.</p></div>
    ${state === "sent" ? html`<div class="card" style="display:flex;flex-direction:column;gap:8px;align-items:flex-start">
        <h2 style="font-size:17px;line-height:24px">Check your inbox</h2>
        <p>We sent a sign-in link to <b>${email}</b>. It works once, for 15 minutes.</p>
        <button class="act" style="margin-left:-14px" onClick=${() => setState("idle")}>Use a different email</button></div>`
      : html`<form onSubmit=${submit} style="display:flex;flex-direction:column;gap:12px">
        <label for="email" class="sr">Email</label>
        <div class="field">
          <input id="email" class="input" type="email" required autocomplete="email" placeholder="you@example.com" value=${email}
                 onInput=${(e) => setEmail(e.target.value)} />
          <button class="pill dark big" disabled=${state === "sending"}>${state === "sending" ? "Sending…" : "Email me a link"}</button>
        </div>
        ${state === "error" && html`<p class="error">${error}</p>`}
        <p class="note">No password. We use your email only to send sign-in links.</p>
      </form>`}
  </main>`;
}

// --- Quiz ---------------------------------------------------------------------------------------

function toggle(list, v) { return list.includes(v) ? list.filter((x) => x !== v) : [...list, v]; }

const Tick = ({ on }) => html`<span class="tick" aria-hidden="true">${on && html`<svg width="11" height="11" viewBox="0 0 12 12"
  fill="none" stroke="#141414" stroke-width="2.2"><path d="M2.5 6.2l2.3 2.3 4.7-4.8"/></svg>`}</span>`;

function Quiz({ onDone }) {
  const [opts, setOpts] = useState(null);
  const [industries, setIndustries] = useState([]);   // "Legal" or "Legal > Litigation support"
  const [skills, setSkills] = useState([]);
  const [worksIn, setWorksIn] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { api("/onboarding/options").then(setOpts); }, []);
  if (!opts) return html`<${SoloHead} /><div class="center">Loading…</div>`;

  const chosenRoots = [...new Set(industries.map((i) => i.split(" > ")[0]))];
  const pickRoot = (name) => setIndustries((cur) => (cur.some((i) => i.split(" > ")[0] === name)
    ? cur.filter((i) => i.split(" > ")[0] !== name) : [...cur, name]));
  const pickBranch = (root, b) => setIndustries((cur) => {
    const key = `${root} > ${b}`;
    let next = toggle(cur, key);
    const hasBranch = next.some((i) => i.startsWith(root + " > "));
    next = next.filter((i) => i !== root);                         // a branch replaces the whole industry...
    return hasBranch ? next : [...next, root];                     // ...and none left means the whole industry
  });
  const submit = async () => {
    setBusy(true); setError("");
    try {
      const r = await api("/onboarding", { method: "POST",
        body: { industries: industries.slice(0, 20), skills: skills.slice(0, 20), works_in: worksIn.slice(0, 10) } });
      onDone(r.user_id);
    } catch (e) { setError(e.message); setBusy(false); }
  };
  const n = chosenRoots.length;

  return html`<${SoloHead} right="Your first feed" /><main class="solo">
    <div><h1>Which industries do you know?</h1>
      <p class="intro">Signal reads each industry's forums, job boards, reviews and new rules, and shows you the problems
        people there run into every week. Pick the ones you understand, or want to.</p></div>
    <section aria-label="Industries"><div class="tiles">${opts.industries.map((i) => {
      const on = chosenRoots.includes(i.name);
      return html`<button class="tile" type="button" aria-pressed=${on} onClick=${() => pickRoot(i.name)}>
        <b>${i.name}</b><span class="sub">${i.branches.length ? plural(i.branches.length, "area") : "Whole industry"}</span>
        <${Tick} on=${on} /></button>`;
    })}</div></section>
    ${chosenRoots.map((root) => {
      const branches = opts.industries.find((i) => i.name === root)?.branches || [];
      return branches.length ? html`<div class="branches"><div class="label" style="margin:0">${root}: any part in particular?</div>
        <div class="chips">${branches.map((b) => html`<button class="chip" type="button" aria-pressed=${industries.includes(`${root} > ${b}`)}
          onClick=${() => pickBranch(root, b)}>${b}</button>`)}</div></div>` : null;
    })}
    <div><div class="label">What can you build with?</div><p class="hint">Optional. Skills you'd use to solve a problem.</p>
      <div class="chips">${opts.skills.map((s) => html`<button class="chip" type="button" title=${s.covers}
        aria-pressed=${skills.includes(s.name)} onClick=${() => setSkills(toggle(skills, s.name))}>${s.name}</button>`)}</div></div>
    <div><div class="label">Do you work in any of these?</div>
      <p class="hint">Optional. Now and then we'll ask whether a problem is real where you work.</p>
      <div class="chips">${opts.industries.map((i) => html`<button class="chip" type="button" aria-pressed=${worksIn.includes(i.name)}
        onClick=${() => setWorksIn(toggle(worksIn, i.name))}>${i.name}</button>`)}</div></div>
    ${error && html`<p class="error">${error}</p>`}
    <div class="go-row"><span class="num" aria-live="polite" style=${n ? "" : "color:var(--muted)"}>
      ${n ? `${n} ${n === 1 ? "industry" : "industries"} picked` : "Pick at least one industry"}</span>
      <button class="pill dark big" type="button" disabled=${!n || busy} onClick=${submit}>
        ${busy ? "Building your feed…" : "Show my feed"}</button></div>
  </main>`;
}

// --- Feed ---------------------------------------------------------------------------------------

const NOT_FOR_ME = [
  ["not_my_skills", "Not my skills"], ["industry_not_interesting", "Not interested in this area"],
  ["too_crowded", "Too crowded"], ["too_small", "Too small"], ["already_exists", "Already solved"],
];

// Dwell tracking: a card counts as read or scrolled past when it leaves the screen (the server decides).
function useDwell(item, ref) {
  useEffect(() => {
    const el = ref.current;
    if (!el || !("IntersectionObserver" in window)) return;
    let start = null, total = 0, full = false, done = false;
    const finish = () => {
      if (done || (start === null && !total)) return;
      if (start !== null) { total += Date.now() - start; start = null; }
      done = true;
      if (total < 1500) track("scroll_past", item, { dwell_ms: total, fully_visible: full });
      else track("read", item, { dwell_ms: total });
    };
    const io = new IntersectionObserver(([e]) => {
      if (e.intersectionRatio >= 0.99) full = true;
      if (e.intersectionRatio >= 0.6 && start === null && !done) start = Date.now();
      else if (e.intersectionRatio < 0.6 && start !== null) finish();
    }, { threshold: [0, 0.6, 0.99] });
    io.observe(el);
    return () => { io.disconnect(); finish(); };
  }, [item.impression_id]);
}

// Items opened from a card: the item page acts on the same card (hide, "was this useful") and sends the same event fields.
const opened = new Map();
const openItem = (it) => { location.hash = `item/${keyOf(it)}`; };

function NotForMeMenu({ item, onHide }) {
  const nfm = (reason) => { track("not_for_me", item, reason ? { reason } : {}); onHide(); };
  return html`<div class="menu">
    ${NOT_FOR_ME.map(([r, label]) => html`<button class="chip" type="button" onClick=${() => nfm(r)}>${label}</button>`)}
    ${item.kind === "inferred" && html`<button class="chip" type="button" onClick=${() => nfm("not_true")}>Not true in my experience</button>`}
    <button class="chip" type="button" onClick=${() => nfm(null)}>Just not for me</button>
    ${item.kind !== "inferred" && html`<button class="chip" type="button" onClick=${() => { track("hide_source", item); onHide(); }}>
      Hide ${sourceName(item.source)}</button>`}
  </div>`;
}

function SaveButton({ item, onSave }) {
  const saved = useSaved(item);
  return html`<button class="act icon end" type="button" aria-pressed=${saved} aria-label=${saved ? "Saved" : "Save"}
    onClick=${() => { markSaved(item); onSave?.(); }}><${Bookmark} on=${saved} /></button>`;
}

function Card({ item }) {
  const ref = useRef();
  useDwell(item, ref);
  const [menu, setMenu] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [answered, setAnswered] = useState(null);      // calibration answer
  const [engaged, setEngaged] = useState(false);       // opened or saved: time to ask "Was this useful?"
  const [useful, setUseful] = useState(null);
  const inferred = item.kind === "inferred";
  opened.set(keyOf(item), { item, setHidden, setEngaged });

  if (hidden) return html`<div class="feed-card hidden-card">Hidden from your feed.</div>`;
  const open = () => { if (item.calibration) return; track("expand", item); setEngaged(true); openItem(item); };
  const similar = !inferred && item.similar_posts > 0 ? `${plural(item.similar_posts, "similar post")} in this problem` : "";

  return html`<article class="feed-card" ref=${ref}>
    <div class="card-head"><${Glyph} signals=${item.signals} />
      <div class="lines"><span class="where">${whoLine(item)}</span><span class="sub">${findingLine(item)}</span></div>
      <span class="time num">${shortAgo(item.published_at)}</span></div>
    <div class="card-body">
      <h2 class="card-title">${item.calibration ? item.headline : html`<a href=${"#item/" + keyOf(item)}
        onClick=${(e) => { e.preventDefault(); open(); }}>${item.headline}</a>`}</h2>
      ${item.summary && item.summary !== item.headline && html`<p class="summary">${item.summary}</p>`}
    </div>
    ${inferred ? html`<${Evidence} items=${item.evidence} compact />` : item.signals && html`<${Strip} signals=${item.signals} />`}

    ${item.calibration ? html`<div class="counts" style="box-shadow:none;padding-bottom:12px">
        ${answered ? html`<span>Thanks.</span>` : html`<span style="display:flex;gap:8px">
          <button class="pill dark small" type="button" onClick=${() => { track("interested", item); setAnswered("yes"); }}>Interested</button>
          <button class="pill small" type="button" onClick=${() => { track("not_for_me", item); setAnswered("no"); }}>Not for me</button></span>`}
      </div>`
    : html`<div class="counts"><span>${similar || (inferred ? "" : "One post so far")}</span>
        <a href=${"#item/" + keyOf(item)} onClick=${(e) => { e.preventDefault(); open(); }} style="color:var(--muted)">Details</a></div>
      <div class="actions">
        <button class="act" type="button" onClick=${open}><${OpenIcon} /><span class="act-label">Read more</span></button>
        <button class="act" type="button" aria-expanded=${menu} onClick=${() => setMenu(!menu)}>
          <${NotIcon} /><span class="act-label">Not for me</span></button>
        <${SaveButton} item=${item} onSave=${() => setEngaged(true)} />
      </div>
      ${menu && html`<${NotForMeMenu} item=${item} onHide=${() => setHidden(true)} />`}`}

    ${item.ask_useful && engaged && html`<div class="well">
      ${useful ? html`<span>Thanks, that helps.</span>` : html`
        <span>Was this useful?</span>
        <button class="pill small" type="button" onClick=${() => { track("useful_yes", item); setUseful("yes"); }}>Yes</button>
        <button class="pill small" type="button" onClick=${() => { track("useful_no", item); setUseful("no"); }}>No</button>`}
    </div>`}
  </article>`;
}

function InsiderPrompt({ userId }) {
  const [p, setP] = useState(null);
  const [note, setNote] = useState("");
  const [done, setDone] = useState(false);
  useEffect(() => { api(`/inferred/prompt?${qs({ user_id: userId })}`).then((r) => setP(r.prompt)).catch(() => {}); }, []);
  if (!p) return null;
  if (done) return html`<div class="feed-card hidden-card">Thanks. Insiders like you keep these honest.</div>`;
  const vote = async (v) => {
    await api(`/inferred/${p.inferred_id}/vote`, { method: "POST",
      body: { user_id: userId, vote: v, note: note.trim() || null } }).catch(() => {});
    setDone(true);
  };
  const label = { yes: "Yes", partly: "Partly", no: "No" };
  return html`<section class="card" style="display:flex;flex-direction:column;gap:10px" aria-label="A question for you">
    <span class="sub">${p.question}</span>
    <h2 class="card-title">${p.hypothesis}</h2>
    <${Evidence} items=${p.evidence} />
    <label class="sr" for="insider-note">Anything to add?</label>
    <textarea id="insider-note" class="input" placeholder="Anything to add? (optional)" maxlength="300" value=${note}
              onInput=${(e) => setNote(e.target.value)}></textarea>
    <div style="display:flex;gap:8px">${p.answers.map((a, i) => html`<button class=${"pill" + (i === 0 ? " dark" : "")} type="button"
      onClick=${() => vote(a)}>${label[a] || a}</button>`)}</div>
  </section>`;
}

function Feed({ userId }) {
  const [items, setItems] = useState([]);
  const [page, setPage] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const sentinel = useRef();

  const load = useCallback(async (explore = false) => {
    setLoading(true); setError("");
    try {
      const p = await api(`/feed?${qs({ user_id: userId, session_id: sessionId(), explore: explore || undefined })}`);
      setItems((cur) => [...cur, ...p.items]);
      setPage(p);
    } catch (e) { setError("Couldn't load the feed. Try again."); }
    setLoading(false);
  }, [userId]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {      // load the next page near the end, unless this page is calibration or the end
    const el = sentinel.current;
    if (!el || !page || page.calibration || page.caught_up || !page.items.length) return;
    const io = new IntersectionObserver(([e]) => { if (e.isIntersecting && !loading) load(); }, { rootMargin: "600px" });
    io.observe(el);
    return () => io.disconnect();
  }, [page, loading]);

  const main = html`<h1 class="sr">Home feed</h1>
    <${InsiderPrompt} userId=${userId} />
    ${page?.calibration && html`<p class="note" style="margin:4px 4px 0">To start, tell us which of these interest you.</p>`}
    ${items.map((it) => html`<${Card} key=${it.impression_id} item=${it} />`)}
    ${error && html`<div class="center"><p class="error">${error}</p><button class="pill" onClick=${() => load()}>Retry</button></div>`}
    ${loading && html`<div class="center">Loading…</div>`}
    ${!loading && page && (page.calibration ? html`<div class="center">
        <button class="pill dark big" onClick=${() => load()}>Continue to my feed</button></div>`
      : page.caught_up || !page.items.length ? html`<div class="end-line">
        <span>You're caught up. That's the strongest for you right now.</span>
        <button class="pill small outline" onClick=${() => load(true)}>Explore more</button></div>` : null)}
    <div ref=${sentinel}></div>`;
  return { main };
}

// --- Item (problem) page ------------------------------------------------------------------------

const STYLE_TABS = [["overview", "Overview"], ["plain", "In plain words"], ["technical", "Technical"]];

function Explanations({ data, userId }) {
  const [style, setStyle] = useState("overview");
  const [texts, setTexts] = useState(data.explanations || {});
  const [state, setState] = useState({});         // style -> "loading" | error message
  const want = async (s) => {
    setStyle(s);
    if (texts[s] || state[s] === "loading" || !data.can_explain) return;
    setState((x) => ({ ...x, [s]: "loading" }));
    try {
      const r = await api(`/item/${data.item_type}/${encodeURIComponent(data.item_id)}/explain`,
                          { method: "POST", body: { user_id: userId, style: s } });
      setTexts((x) => ({ ...x, [s]: r.text }));
      setState((x) => ({ ...x, [s]: null }));
    } catch (e) {
      setState((x) => ({ ...x, [s]: e.status === 429 ? "You've read a lot today. More explanations tomorrow."
        : "Couldn't write this one right now." }));
    }
  };
  useEffect(() => { want("overview"); }, [data.item_id]);
  const text = texts[style];
  return html`<section class="card explain" aria-labelledby="exp">
    <div class="card-h"><h2 id="exp">What's going on</h2></div>
    <div class="seg" role="tablist" aria-label="Explanation style">${STYLE_TABS.map(([s, label]) => html`<button role="tab"
      type="button" class="seg-btn" aria-selected=${style === s} onClick=${() => want(s)}>${label}</button>`)}</div>
    <div class="explain-text" aria-live="polite">${text ? text.split(/\n\s*\n/).map((para) => html`<p>${para}</p>`)
      : state[style] === "loading" ? html`<p class="note">Writing an explanation…</p>`
      : state[style] ? html`<p class="note">${state[style]}</p>`
      : !data.can_explain ? html`<p class="note">Explanations aren't available on this server.</p>` : null}</div>
  </section>`;
}

const SIGNAL_DESC = {
  pain: "Posts where people describe the problem in their own words",
  paid_demand: "Gigs, jobs and tenders paying someone to deal with it",
  tool_gap: "Complaints about the tools they use for it now",
  change: "Rules and news that change how it has to be done",
};

function SignalCard({ p }) {
  return html`<section class="card" aria-labelledby="sig">
    <div class="card-h"><h2 id="sig">The signal</h2><span class="sub">${plural(p.total, "post")} in this problem</span></div>
    ${SIGNALS.map(([k, label]) => {
      const n = k === "size" ? 0 : p.signals[k];
      return html`<div class="sig-row">
        <span class="l">${k === "paid_demand" ? "Paid demand" : label}</span>
        <span class=${"v num" + (n ? "" : " none")}>${k === "size" ? "Not measured yet" : n || "None yet"}</span>
        <span class="d">${k === "size" ? "How many people or businesses have it. Signal doesn't estimate this yet." : SIGNAL_DESC[k]}</span>
        ${n > 0 && html`<span class="track" aria-hidden="true"><span style=${`width:${(n / p.total * 100).toFixed(1)}%`}></span></span>`}
      </div>`;
    })}
  </section>`;
}

function weekLabel(i) {
  if (i === 7) return "This week";
  const d = new Date(Date.now() - (7 - i) * 7 * 86400000 - 6 * 86400000);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function MentionsCard({ p }) {
  const w = p.weekly;
  const max = Math.max(...w);
  const top = max <= 4 ? Math.max(max, 1) : Math.ceil(max / 4) * 4;
  const ticks = [...new Set([0, Math.round(top / 2), top])];
  const delta = w[7] - w[6];
  const deltaText = delta === 0 ? "same as last week" : `${delta > 0 ? "+" : ""}${delta} on last week`;
  const bySource = new Map();                       // two raw sources can share a display name
  p.sources.forEach((x) => bySource.set(sourceName(x.source), (bySource.get(sourceName(x.source)) || 0) + x.count));
  const sources = [...bySource].map(([name, count]) => ({ name, count })).sort((a, b) => b.count - a.count);
  const sTop = sources[0]?.count || 1;
  return html`<section class="card" aria-labelledby="wk">
    <div class="card-h"><h2 id="wk">Mentions each week</h2>
      <span class="sub">${max ? `${plural(w[7], "post")} this week, ${deltaText}` : "None in the last 8 weeks"}</span></div>
    ${max > 0 && html`<div class="chart" role="img" aria-label=${`Posts per week for the last 8 weeks: ${w.join(", ")}`}>
      <div class="plot">
        ${ticks.map((t) => html`<span class=${"grid-line" + (t === 0 ? " base" : "")} style=${`bottom:${t / top * 140}px`}></span>
          <span class="tick num" style=${`bottom:${Math.max(0, t / top * 140 - 7)}px`}>${t}</span>`)}
        <div class="bars">${w.map((v, i) => html`<div title=${`${weekLabel(i)}: ${plural(v, "post")}`}>
          <span class=${i === 7 ? "now" : ""} style=${`height:${v / top * 140}px`}></span></div>`)}</div>
      </div>
      <div class="xlabels">${w.map((_, i) => html`<span>${i % 2 === 1 || i === 7 ? weekLabel(i) : ""}</span>`)}</div>
    </div>`}
    <h3 style="margin:24px 0 6px;font-size:15px;line-height:22px;font-weight:600">Where the posts come from</h3>
    ${sources.map((s, i) => html`<div class=${"src-row" + (i === 0 ? " top" : "")}><span class="n">${s.name}</span>
      <span class="b" aria-hidden="true" style=${`width:${(s.count / sTop * 100).toFixed(1)}%`}></span>
      <span class="c num">${s.count}</span></div>`)}
    ${p.sources_more > 0 && html`<p class="sub" style="margin-top:6px">and ${plural(p.sources_more, "more source")}</p>`}
  </section>`;
}

function PostsCard({ p }) {
  if (!p.posts.length) return null;
  return html`<section class="card" aria-labelledby="ev">
    <div class="card-h"><h2 id="ev">Other posts about it</h2>
      <span class="sub">${p.posts_more > 0 ? `Latest ${p.posts.length} of ${p.posts.length + p.posts_more}` : ""}</span></div>
    ${p.posts.map((e) => html`<div class="post-row">
      <div class="top"><span><span class="platform">${sourceName(e.source)}</span>
        <span class="status">${TYPE_LABEL[e.post_type] || "Post"}</span>${money(e.money) && html`<span class="status num">${money(e.money)}</span>`}</span>
        <span class="sub num">${shortAgo(e.published_at)}</span></div>
      <a class="h" href=${"#item/" + keyOf(e)}>${e.headline}</a>
    </div>`)}
    ${p.posts_alike > 0 && html`<p class="sub" style="margin-top:12px">${plural(p.posts_alike, "more post")} saying nearly the same
      thing (often one tender republished by several buyers)</p>`}
  </section>`;
}

function postedBy(d) {
  const who = roleText(d.author_role) ? `a ${roleText(d.author_role)}` : null;
  const where = d.community ? `in ${d.community}` : null;
  return [who && `Posted by ${who}`, where && (who ? where : `Posted ${where}`)].filter(Boolean).join(" ")
    || `Posted on ${sourceName(d.source)}`;
}

function ItemPage({ type, id, userId, me }) {
  const key = `${type}/${id}`;
  const from = opened.get(key);
  const base = from?.item || { item_type: type, item_id: id };
  const [d, setD] = useState(null);
  const [menu, setMenu] = useState(false);
  const [hidden, setHidden] = useState(false);
  const pane = useRef();
  useEffect(() => {
    if (pane.current) pane.current.scrollTop = 0;
    document.documentElement.classList.add("pane-open");
    return () => document.documentElement.classList.remove("pane-open");
  }, [key]);
  useEffect(() => {
    api(`/item/${type}/${encodeURIComponent(id)}?${qs({ user_id: userId })}`).then(setD).catch(() => setD({ error: true }));
  }, [key]);
  const back = () => (from ? history.back() : (location.hash = "feed"));
  const saved = useSaved(base);
  const url = d && safeUrl(d.url);
  const eng = d?.engagement || {};
  const votes = eng.upvotes || eng.score || eng.likes || eng.reactions;
  const stats = [eng.comments && plural(eng.comments, "comment"), votes && plural(votes, "upvote")].filter(Boolean);
  const p = d?.problem;
  const signals = p?.signals || null;
  const inferred = d?.kind === "inferred";

  let main;
  if (!d) main = html`<div class="center">Loading…</div>`;
  else if (d.error) main = html`<div class="center">This item is no longer available.</div>`;
  else {
    const { industry, rest } = industryOf(d);
    const who = roleText(d.author_role) ? roleText(d.author_role) : inferred && d.who ? [].concat(d.who).join(", ") : rest;
    const first = p?.first_seen && new Date(p.first_seen).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
    main = html`
      <article class="card">
        <div class="head-row"><${Glyph} signals=${signals} size=${56} />
          <div class="lines"><span class="where"><b>${industry}</b>${who && html`<span>, ${who}</span>`}</span>
            <span class="sub">${inferred ? `Inferred by Signal from ${plural(d.evidence.length, "piece")} of evidence${d.confidence ? `, ${d.confidence} confidence` : ""}`
              : `${p && p.total > 1 ? `First seen ${first}, ${plural(p.sources.length + p.sources_more, "source")}, ` : ""}${strength(signals).toLowerCase()}`}</span></div></div>
        <h1 class="page-title">${d.headline}</h1>
        ${!inferred && html`<p class="sub">${postedBy(d)}, ${new Date(d.published_at).toLocaleDateString(undefined,
            { month: "long", day: "numeric", year: "numeric" })} (${ago(d.published_at)})${stats.length ? ". " + stats.join(", ") : ""}</p>`}
        ${!inferred && d.title && d.title !== d.headline && html`<p class="sub" style="margin-top:4px">Original title: ${d.title}</p>`}
        ${hidden ? html`<p class="note" style="margin-top:16px">Hidden from your feed.</p>` : html`<div class="head-actions">
          <button class="pill" type="button" aria-pressed=${saved} onClick=${() => { markSaved(base); from?.setEngaged(true); }}>
            <${Bookmark} on=${saved} />${saved ? "Saved" : "Save"}</button>
          <button class="pill" type="button" aria-expanded=${menu} onClick=${() => setMenu(!menu)}>Not for me</button>
          ${url && html`<a class="pill dark" style="margin-left:auto" href=${url} target="_blank" rel="noopener noreferrer"
            onClick=${() => track("open_source", base)}>Open original <${OutIcon} /></a>`}
        </div>
        ${menu && html`<div style="margin:12px -16px -16px"><${NotForMeMenu} item=${{ ...base, kind: d.kind, source: d.source }}
          onHide=${() => { setHidden(true); setMenu(false); from?.setHidden(true); }} /></div>`}`}
      </article>
      <${Explanations} data=${d} userId=${userId} />
      ${inferred && html`<section class="card" aria-labelledby="evi"><div class="card-h"><h2 id="evi">Evidence</h2>
          <span class="sub num">${plural(d.evidence.length, "piece")}</span></div><${Evidence} items=${d.evidence} /></section>
        ${d.counter_evidence?.length > 0 && html`<section class="card" aria-labelledby="cev"><div class="card-h">
          <h2 id="cev">What could argue against it</h2></div><${Evidence} items=${d.counter_evidence} /></section>`}`}
      ${p && html`<${SignalCard} p=${p} /><${MentionsCard} p=${p} /><${PostsCard} p=${p} />`}`;
  }

  return html`<div class="item-page" ref=${pane} role="dialog" aria-modal="true" aria-label="Problem">
    <div class="wrap"><${Rail} me=${me} tab="feed" />
      <main class="main"><button class="act back" type="button" onClick=${back}><${BackIcon} />Back</button>${main}</main>
    </div></div>`;
}

// --- Saved, clips, areas ------------------------------------------------------------------------

function Saved({ userId }) {
  const [items, setItems] = useState(null);
  useEffect(() => { api(`/saved?${qs({ user_id: userId })}`).then((r) => setItems(r.items)); }, []);
  const main = html`<h1 class="page-h1">Saved</h1>
    ${!items ? html`<div class="center">Loading…</div>` : !items.length ? html`<div class="empty">
        <h2>Keep the problems worth a second look</h2>
        <p>Tap the bookmark on any problem and it stays here, with its evidence, until you're ready to work on it.</p>
        <a class="pill small" href="#feed" style="margin-top:8px">Back to your feed</a></div>`
      : html`<div class="list-card">${items.map((it) => html`<div class="lrow"><${Glyph} signals=${it.signals} size=${36} />
        <div class="grow"><a href=${"#item/" + keyOf(it)}>${it.headline || it.title || it.summary}</a>
          <span class="sub">${it.niche_path[0] || ""}${it.kind === "inferred" ? ", inferred" : `, ${sourceName(it.source)}`}. Saved ${ago(it.saved_at)}</span>
        </div></div>`)}</div>`}`;
  return { main };
}

function Clips({ userId }) {
  const [clips, setClips] = useState(null);
  const [error, setError] = useState(false);
  const load = () => api(`/clips?${qs({ user_id: userId })}`).then(setClips).catch(() => setError(true));
  useEffect(() => { load(); }, []);
  const remove = async (id) => {
    await api(`/clips/${encodeURIComponent(id)}?${qs({ user_id: userId })}`, { method: "DELETE" }).catch(() => {});
    load();
  };
  const main = html`<h1 class="page-h1">Clips</h1>
    ${error ? html`<div class="empty"><p>Clips aren't available on this server.</p></div>`
      : !clips ? html`<div class="center">Loading…</div>`
      : !clips.length ? html`<div class="empty"><h2>Nothing clipped yet</h2>
          <p>Select text on any page and use the Clip to Signal button. The link, the text you picked and your note land here.</p></div>`
      : html`<div class="list-card">${clips.map((c) => html`<div class="lrow" style="align-items:flex-start"><div class="grow">
        <a href=${safeUrl(c.url)} target="_blank" rel="noopener noreferrer">${c.title || c.url}</a>
        ${c.text && html`<blockquote class="clip">${c.text.slice(0, 400)}</blockquote>`}
        ${c.note && html`<span class="sub" style="color:var(--ink-2)">Your note: ${c.note}</span>`}
        <span class="sub">${c.site}, ${ago(c.created_at)}</span></div>
        <button class="act" type="button" onClick=${() => remove(c.id)}>Delete</button></div>`)}</div>`}`;
  return { main };
}

function Areas({ userId }) {
  const [map, setMap] = useState(null);
  const [confirmReset, setConfirmReset] = useState(false);
  const load = () => api(`/map?${qs({ user_id: userId })}`).then(setMap);
  useEffect(() => { load(); }, []);
  const act = async (path, body) => { await api(path, { method: "POST", body: { user_id: userId, ...body } }).catch(() => {}); load(); };
  const groups = [["core", "Closest to you"], ["regular", "Also yours"], ["exploring", "Exploring"]];
  const nodes = map ? [...map.nodes].sort((a, b) => b.affinity - a.affinity) : [];
  const main = html`<h1 class="page-h1">Your areas</h1>
    <p class="note" style="margin:0 4px 8px">Your feed comes from these. Ask for more or less of any of them.</p>
    ${!map ? html`<div class="center">Loading…</div>` : groups.map(([ring, title]) => {
      const here = nodes.filter((n) => n.ring === ring);
      return here.length ? html`<section class="card" style="padding:16px 20px 4px"><div class="card-h"><h2>${title}</h2>
          <span class="sub num">${here.length}</span></div>
        ${here.map((n) => html`<div class=${"lrow" + (n.status === "context" ? " context" : "")}><div class="grow">
          <span>${n.label}</span><span class="sub">${n.path.slice(0, -1).join(", ")}</span></div>
          <span class="btns"><button class="act" type="button" aria-label=${`More ${n.label}`}
              onClick=${() => act("/map/nudge", { node_id: n.node_id, direction: "more" })}>More</button>
            <button class="act" type="button" aria-label=${`Less ${n.label}`}
              onClick=${() => act("/map/nudge", { node_id: n.node_id, direction: "less" })}>Less</button>
            <button class="act" type="button" aria-label=${`Remove ${n.label}`}
              onClick=${() => act("/map/remove", { node_id: n.node_id })}>Remove</button></span>
        </div>`)}</section>` : null;
    })}
    ${map && !nodes.length && html`<div class="empty"><p>Your areas appear here as you use the feed.</p></div>`}
    ${map && html`<div class="end-line">${confirmReset ? html`<span>This forgets what the feed has learned about you. Sure?</span>
        <button class="pill small dark" onClick=${() => { setConfirmReset(false); act("/map/reset", {}); }}>Yes, start over</button>
        <button class="pill small" onClick=${() => setConfirmReset(false)}>Cancel</button>`
      : html`<button class="act" style="margin-left:-14px" onClick=${() => setConfirmReset(true)}>Start over from my quiz answers</button>`}</div>`}`;
  return { main };
}

// --- App ----------------------------------------------------------------------------------------

// Views return { main } for the shell to place next to the rail; they're called as hooks-using functions
// inside a component keyed by tab, so each tab keeps its own state while it's open.
function View({ view, userId, me, tab }) {
  const { main } = view({ userId });
  return html`<div class="wrap"><${Rail} me=${me} tab=${tab} /><main class="main">${main}</main></div>`;
}

function App() {
  const [me, setMe] = useState(undefined);       // undefined: loading; null: signed out
  const [hash, setHash] = useState(() => location.hash.slice(1));
  const [tab, setTab] = useState(() => (location.hash.slice(1).startsWith("item/") ? "feed" : location.hash.slice(1) || "feed"));
  useEffect(() => { api("/auth/me").then(setMe).catch(() => setMe(null)); }, []);
  useEffect(() => {
    const on = () => {
      const h = location.hash.slice(1);
      setHash(h);
      if (!h.startsWith("item/")) setTab(h || "feed");     // the item page opens over the tab, which keeps its place
    };
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  useEffect(() => {                                  // what's already saved, so every bookmark shows it
    if (!me?.user_id) return;
    api(`/saved?${qs({ user_id: me.user_id })}`).then((r) => {
      r.items.forEach((it) => savedStore.keys.add(keyOf(it)));
      savedStore.subs.forEach((f) => f());
    }).catch(() => {});
  }, [me?.user_id]);
  const itemRoute = hash.startsWith("item/") ? hash.split("/") : null;   // ["item", type, id]
  events.userId = me?.user_id || null;

  if (me === undefined) return html`<div class="center">Loading…</div>`;
  if (me === null) return html`<${SignIn} />`;
  if (!me.user_id) return html`<${Quiz} onDone=${(uid) => setMe({ ...me, user_id: uid })} />`;

  const views = { feed: Feed, saved: Saved, clips: Clips, areas: Areas };
  const current = views[tab] ? tab : "feed";
  return html`
    <${View} key=${current} view=${views[current]} userId=${me.user_id} me=${me} tab=${current} />
    ${itemRoute && itemRoute.length >= 3 && html`<${ItemPage} key=${hash} type=${itemRoute[1]}
      id=${decodeURIComponent(itemRoute.slice(2).join("/"))} userId=${me.user_id} me=${me} />`}`;
}

render(html`<${App} />`, document.getElementById("app"));
