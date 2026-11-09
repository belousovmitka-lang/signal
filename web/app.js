// Signal web app: Home, Problems, Problem, Workspace (Outreach, Interviews, Assumptions, Brief, Pilot) and
// Cofounders, built to the design handoff of 2026-11-08 (README in signal-handoff.zip). Preact + htm, no build step.
// Problems are job clusters (signal_feed/builder.py): every number on them comes from the posts. Where a value
// is missing, the page shows a placeholder like [YOUR PRICE], never an invented one.
import { html, render, useState, useEffect, useRef, useMemo } from "/static/vendor/preact-htm.js";

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
  if ((r.status === 401 || r.status === 403) && !path.startsWith("/auth/")) {
    let d = "";
    try { d = (await r.clone().json()).detail; } catch { /* not JSON */ }
    if (r.status === 401 || d === "account suspended") location.reload();
  }
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail; } catch { /* not JSON */ }
    throw new HttpError(r.status, typeof detail === "string" ? detail : "");
  }
  return r.json();
}

const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== undefined && v !== null && v !== "")).toString();
const enc = encodeURIComponent;

// --- Helpers ------------------------------------------------------------------------------------

const safeUrl = (u) => (typeof u === "string" && /^https?:\/\//i.test(u) ? u : null);
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Math.random())).slice(0, 8);
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const low = (s) => (s ? s[0].toLowerCase() + s.slice(1) : s);
const plural = (n, w, ws = w + "s") => `${n} ${n === 1 ? w : ws}`;
const reduceMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

function ago(iso) {
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 3600) return `${Math.max(1, Math.round(d / 60))} min ago`;
  if (d < 86400) return plural(Math.round(d / 3600), "hour") + " ago";
  if (d < 86400 * 14) return plural(Math.round(d / 86400), "day") + " ago";
  if (d < 86400 * 60) return plural(Math.round(d / 86400 / 7), "week") + " ago";
  return plural(Math.round(d / 86400 / 30), "month") + " ago";
}
const dayLabel = (iso) => new Date(iso.length === 10 ? iso + "T00:00" : iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
function whenLabel(iso) {           // "Mon 10:00" within a week, else "Nov 19"
  const d = asDate(iso);
  const days = Math.abs(d - Date.now()) / 86400000;
  const hasTime = iso.length > 10 && !/T00:00(:00(\.0+)?)?Z?$/.test(iso);
  const time = hasTime ? " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false }) : "";
  return (days < 6.5 ? d.toLocaleDateString(undefined, { weekday: "short" }) : dayLabel(iso)) + time;
}
const todayISO = () => new Date().toLocaleDateString("en-CA");      // local YYYY-MM-DD
const asDate = (iso) => new Date(iso.length === 10 ? iso + "T00:00" : iso);
const isUpcoming = (i) => (i.date.length > 10 ? asDate(i.date) > Date.now() : i.date > todayISO());

function sourceName(src) {
  const [family, rest] = String(src || "").split(":");
  const names = { hackernews: "Hacker News", hn: "Hacker News", x: "X", jetstream: "Bluesky", bluesky: "Bluesky",
                  stackexchange: "Stack Exchange", sam_gov: "SAM.gov", ted: "EU tenders", uk_tenders: "UK tenders",
                  federal_register: "Federal Register", regulations_gov: "Regulations.gov", job_boards: "Job post",
                  github: "GitHub", edgar: "SEC EDGAR", courtlistener: "CourtListener", openfda: "FDA", eurlex: "EU law",
                  legiscan: "LegiScan", openstates: "Open States", mastodon: "Mastodon", lemmy: "Lemmy",
                  app_reviews: "WordPress.org", huggingface: "Hugging Face", papers: "arXiv", peopleperhour: "PeoplePerHour",
                  cfpb: "CFPB", nhtsa: "NHTSA", producthunt: "Product Hunt" };
  if (family === "rss" || family === "classic_forum" || family === "brave") return rest || family;
  return names[family] || family;
}

async function copy(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { return false; }
}

// A one-line status message for actions with no visible result (copied, saved).
const toast = { msg: "", subs: new Set(), timer: null };
function say(msg) {
  toast.msg = msg; toast.subs.forEach((f) => f());
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { toast.msg = ""; toast.subs.forEach((f) => f()); }, 2600);
}
function Toast() {
  const [, force] = useState(0);
  useEffect(() => { const f = () => force((x) => x + 1); toast.subs.add(f); return () => toast.subs.delete(f); }, []);
  return html`<div role="status" aria-live="polite" class="toast" style=${toast.msg ? "" : "display:none"}>${toast.msg}</div>`;
}

// --- Icons --------------------------------------------------------------------------------------

const Logo = ({ color = "#4D8DFF" }) => html`<svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true">
  <rect x="2" y="13" width="4" height="7" rx="1.2" fill=${color}/><rect x="9" y="8.5" width="4" height="11.5" rx="1.2" fill=${color}/>
  <rect x="16" y="3" width="4" height="17" rx="1.2" fill=${color}/></svg>`;
const ICONS = {
  home: "M3.5 9.2 10 4l6.5 5.2V16a1 1 0 0 1-1 1h-3.3v-4.4H7.8V17H4.5a1 1 0 0 1-1-1z",
  problems: "M9 15a6 6 0 1 0 0-12 6 6 0 0 0 0 12zM13.5 13.5 17 17",
  workspace: "M2.8 5.5a1 1 0 0 1 1-1h4l1.7 2h6.7a1 1 0 0 1 1 1V15a1 1 0 0 1-1 1H3.8a1 1 0 0 1-1-1z",
  cofounders: "M7 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM13 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
};
const Icon = ({ name, size = 20 }) => html`<svg width=${size} height=${size} viewBox="0 0 20 20" fill="none"
  stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" aria-hidden="true"><path d=${ICONS[name]}/></svg>`;
const Plus = () => html`<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7"
  aria-hidden="true"><path d="M8 3v10M3 8h10"/></svg>`;
const Back = () => html`<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7"
  stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 8H3M7 4 3 8l4 4"/></svg>`;
const BookmarkIcon = () => html`<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"
  stroke-linejoin="round" aria-hidden="true"><path d="M4 2.5h8v11L8 10.5l-4 3z"/></svg>`;

// --- Routing and the page change animation ------------------------------------------------------

function parseRoute(hash) {
  const p = hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (p[0] === "problems") return p[1] ? { name: "problem", id: p[1] } : { name: "problems" };
  if (p[0] === "workspace") return { name: "workspace", id: p[1] || null, tab: p[2] || "outreach" };
  if (p[0] === "cofounders") return { name: "cofounders" };
  return { name: "home" };
}
const go = (path) => { location.hash = "#/" + path; };
const NAV = [["home", "Home", ""], ["problems", "Problems", "problems"], ["workspace", "Workspace", "workspace"],
             ["cofounders", "Cofounders", "cofounders"]];

// Old content fades out (140 ms), then the new content mounts and its columns fade in one after another (CSS).
function useLeaving(key, value) {
  const [shown, setShown] = useState(value);
  const [leaving, setLeaving] = useState(false);
  const last = useRef(key);
  useEffect(() => {
    if (key === last.current || reduceMotion()) { last.current = key; setShown(value); return undefined; }
    last.current = key;
    setLeaving(true);
    const t = setTimeout(() => { setShown(value); setLeaving(false); scrollTo(0, 0); }, 140);
    return () => clearTimeout(t);
  }, [key, value]);
  return [shown, leaving];
}

// --- Saved problems and their workspaces (one shared store; writes are debounced) -----------------

const store = { userId: null, items: null, cofounders: null, subs: new Set(), timers: {}, loading: false };
const notify = () => store.subs.forEach((f) => f());

function useStore() {
  const [, force] = useState(0);
  useEffect(() => { const f = () => force((x) => x + 1); store.subs.add(f); return () => store.subs.delete(f); }, []);
  return store;
}

function loadWorkspaces() {
  if (store.loading) return;
  store.loading = true;
  api(`/workspaces?${qs({ user_id: store.userId })}`).then((r) => {
    store.items = r.items;
    r.items.forEach((w) => { if (!w.data || !w.data.v) { w.data = seed(w); persist(w); } });
    notify();
  }).catch(() => { store.items = []; notify(); }).finally(() => { store.loading = false; });
}

function loadCofounders() {
  return api(`/cofounders?${qs({ user_id: store.userId })}`).then((r) => { store.cofounders = r; notify(); return r; });
}

function persist(w, keepalive = false) {
  clearTimeout(store.timers[w.problem_id]);
  const send = () => {
    delete store.timers[w.problem_id];
    return api(`/workspaces/${enc(w.problem_id)}`, { method: "PUT", keepalive, body: { user_id: store.userId, data: w.data } })
      .catch(() => say("Couldn't save. Your last change will be retried."));
  };
  if (keepalive) return send();
  store.timers[w.problem_id] = setTimeout(send, 500);
  return null;
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "hidden" || !store.items) return;
  store.items.filter((w) => store.timers[w.problem_id]).forEach((w) => persist(w, true));
});

function edit(w, fn) {
  const d = structuredClone(w.data);
  fn(d);
  w.data = d;
  notify();
  persist(w);
}

async function saveProblem(problemId, note) {
  const w = await api(`/workspaces/${enc(problemId)}`, { method: "POST", body: { user_id: store.userId, note: note || null } });
  const items = store.items || [];
  const i = items.findIndex((x) => x.problem_id === w.problem_id);
  if (i >= 0) { items[i] = { ...items[i], note: w.note }; } else {
    if (!w.data || !w.data.v) { w.data = seed(w); persist(w); }
    items.push(w);
  }
  store.items = [...items];
  notify();
  return w;
}

const SECTIONS = [["problem", "The problem"], ["who", "Who has it"], ["cost", "What it costs them"],
                  ["today", "What they use today"], ["why", "Why now"]];

// A new workspace starts from what the evidence already shows: the people who posted, beliefs to test, the
// open questions, a brief drafted from the problem. Nothing is made up: unknowns stay empty or [PLACEHOLDER].
function seed(w) {
  const p = w.problem || {};
  const ev = (p.evidence || []).filter((e) => safeUrl(e.url));
  const contacts = ev.slice(0, 6).map((e) => ({
    id: uid(), name: (cap(e.who) || "Someone").slice(0, 48), org: sourceName(e.source), how: "from a post", email: "",
    source: e.summary, sourceUrl: e.url, sourceAt: e.published_at, status: "not", channel: "forum",
    subject: "", body: "", sentAt: null, replied: false,
  }));
  const tools = p.tools || [];
  const assumptions = [
    { id: uid(), text: p.title || "People have this problem", status: "untested", note: "", test: "Ask in your first interviews", riskiest: false },
    { id: uid(), text: "It happens every week or more", status: "untested", note: "", test: "Ask how often, and how long it takes", riskiest: false },
    { id: uid(), text: `${p.audience?.payer || p.buyer || "They"} would pay to fix it`, status: "untested", note: "",
      test: "Put a price in the pilot offer", riskiest: true },
    { id: uid(), text: tools.length ? `${tools.slice(0, 3).join(", ")} don't solve it well` : "No existing tool solves it well",
      status: "untested", note: "", test: "Ask what they tried before", riskiest: false },
  ];
  const posts = p.posts || 0;
  const brief = {
    problem: p.summary || p.statement || "",
    who: [p.audience?.role, p.segment].filter(Boolean).join(". "),
    cost: "", today: (p.workarounds || []).map((x) => x.what).join("\n"), why: "",
  };
  const briefSrc = {};
  SECTIONS.forEach(([k]) => { if (brief[k]) briefSrc[k] = plural(posts, "post"); });
  return {
    v: 1, contacts, learnings: [], interviews: [], assumptions, brief, briefSrc, briefBy: {}, briefAt: null,
    questions: (p.open_questions || []).map((q) => q.split("?")[0] + "?"),
    offer: { price: "", weeks: "", success: "", goal: "3" }, pilots: {}, notes: w.note || "", plan: {},
  };
}

const pastInterviews = (d) => d.interviews.filter((i) => !isUpcoming(i)).sort((a, b) => b.date.localeCompare(a.date));
const upcoming = (d) => d.interviews.filter(isUpcoming).sort((a, b) => a.date.localeCompare(b.date));
const riskiest = (d) => d.assumptions.find((a) => a.riskiest && a.status === "untested")
  || d.assumptions.find((a) => a.status === "untested");
const goalOf = (d) => Math.max(1, parseInt(d.offer.goal, 10) || 3);

function planSteps(d) {
  const sent = d.contacts.filter((c) => c.sentAt).length;
  const past = pastInterviews(d).length;
  const risk = d.assumptions.find((a) => a.riskiest);
  const filled = SECTIONS.filter(([k]) => (d.brief[k] || "").trim()).length;
  const committed = d.contacts.filter((c) => c.status === "committed").length;
  const goal = goalOf(d);
  return [
    { phase: "Validation", key: "reach", title: "Message 5 people who have it", meta: `${Math.min(sent, 5)} of 5 sent`,
      done: sent >= 5, action: ["Find people to interview", "outreach"] },
    { phase: "Validation", key: "interview", title: "Run 5 interviews", meta: `${Math.min(past, 5)} of 5 done`,
      done: past >= 5, action: ["Log an interview", "interviews"] },
    { phase: "Validation", key: "risk", title: risk ? `Test: ${low(risk.text)}` : "Test your riskiest assumption",
      meta: "Your riskiest assumption", done: !!risk && risk.status !== "untested", action: ["Open assumptions", "assumptions"] },
    { phase: "Validation", key: "workarounds", title: "List the workarounds people use today", meta: "From your interview notes",
      done: !!d.briefBy.today, action: ["Open the brief", "brief"] },
    { phase: "Validation", key: "brief", title: "Write the one-page problem brief", meta: `${filled} of 5 sections`,
      done: filled === 5 && Object.keys(d.briefBy).length > 0, action: ["Open the brief", "brief"] },
    { phase: "Pilot", key: "offer", title: "Write your pilot offer", meta: d.offer.price.trim() ? "Price set" : "Price not set",
      done: !!(d.offer.price.trim() && d.offer.success.trim()), action: ["Write the offer", "pilot"] },
    { phase: "Pilot", key: "commit", title: `Get ${goal} pilot commitments`, meta: `${committed} of ${goal}`,
      done: committed >= goal, action: ["Send the offer", "pilot"] },
  ].map((s) => ({ ...s, auto: s.done, done: s.done || !!d.plan[s.key] }));
}

// --- Shell --------------------------------------------------------------------------------------

const initials = (s) => (s || "?").split(/[@\s._-]+/).filter(Boolean).slice(0, 2).map((x) => x[0].toUpperCase()).join("") || "?";
const displayName = (me) => store.cofounders?.me?.name || cap((me.email || "").split("@")[0].split(/[._-]/)[0]) || "You";

function signOut() {
  api("/auth/sign-out", { method: "POST" }).catch(() => {}).finally(() => { location.hash = ""; location.reload(); });
}

function Sidebar({ me, active }) {
  const s = useStore();
  const [open, setOpen] = useState(false);
  const n = s.items ? s.items.length : null;
  return html`<aside class="side">
    <a class="logo" href="#/"><${Logo} /><span>Signal</span></a>
    <nav aria-label="Main">${NAV.map(([k, label, path]) => html`<a class="nav" href=${"#/" + path}
      aria-current=${active === k ? "page" : undefined}><${Icon} name=${k} />${label}</a>`)}</nav>
    <div class="account">
      ${open && html`<div class="menu" role="menu"><div class="email">${me.email}</div>
        <button type="button" role="menuitem" onClick=${() => { setOpen(false); go("cofounders"); }}>Edit my profile</button>
        <button type="button" role="menuitem" onClick=${signOut}>Sign out</button></div>`}
      <button type="button" aria-expanded=${open} aria-label="Account" onClick=${() => setOpen(!open)}>
        <span class="avatar">${initials(displayName(me))}</span>
        <span><b>${displayName(me)}</b><small class="num">${n === null ? "" : plural(n, "saved problem")}</small></span>
      </button>
    </div>
  </aside>`;
}

// --- Home ---------------------------------------------------------------------------------------

function Home({ me }) {
  const s = useStore();
  const items = s.items;
  const [sel, setSel] = useState(() => { try { return localStorage.getItem("signal_home_problem"); } catch { return null; } });
  const [full, setFull] = useState(false);
  useEffect(() => { if (!s.cofounders) loadCofounders().catch(() => {}); }, []);
  const date = new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
  if (!items) return html`<div class="page"><p class="center">Loading…</p></div>`;
  const w = items.find((x) => x.problem_id === sel) || items[0];
  const pick = (id) => { setSel(id); setFull(false); try { localStorage.setItem("signal_home_problem", id); } catch { /* private mode */ } };
  const header = html`<div><p class="date">${date}</p><h1 class="title">Today</h1></div>`;
  if (!w) {
    return html`<div class="page">${header}
      <div class="card" style="max-width:640px"><h2 class="sec">Save a problem to start</h2>
        <p class="muted">Each saved problem gets a workspace and a short plan here.</p>
        <a class="btn primary" href="#/problems">Browse problems</a></div></div>`;
  }
  const d = w.data;
  const steps = planSteps(d);
  const open = steps.filter((x) => !x.done);
  const phase = open[0]?.phase;
  const shown = open.filter((x) => x.phase === phase).slice(0, 3);
  const toggleStep = (k) => edit(w, (dd) => { dd.plan[k] = !dd.plan[k]; });
  return html`<div class="page">
    ${header}
    <div class="ptabs" role="group" aria-label="Saved problems">
      ${items.map((x) => html`<button type="button" class="ptab" aria-pressed=${x === w} onClick=${() => pick(x.problem_id)}>
        <span>${shortTitle(x.problem)}</span>${x.problem.score != null && html`<span class="score">${x.problem.score}</span>`}</button>`)}
      <a class="addtab" href="#/problems" aria-label="Find another problem"><${Plus} /></a>
    </div>
    <div class="cols cols-home stagger" key=${w.problem_id}>
      <section class="card" aria-labelledby="plan-h">
        <h2 class="sec" id="plan-h">${phase || "Plan"}</h2>
        ${!shown.length && html`<p class="muted">Every step is done.</p>`}
        ${(full ? steps : shown).map((st, i) => {
          const first = !full && i === 0;
          return html`<div class=${"step" + (first ? " first" : "") + (st.done ? " done" : "")}>
            <input type="checkbox" class="check" checked=${st.done} disabled=${st.auto} id=${"st-" + st.key}
              aria-label=${st.title} onChange=${() => toggleStep(st.key)} />
            <div><label for=${"st-" + st.key}><b>${st.title}</b></label><span class="meta">${st.meta}</span>
              ${first && html`<div><a class="btn primary" href=${`#/workspace/${enc(w.problem_id)}/${st.action[1]}`}>${st.action[0]}</a></div>`}
              ${!first && !st.done && html`<div><a class="textbtn" style="min-height:32px" href=${`#/workspace/${enc(w.problem_id)}/${st.action[1]}`}>${st.action[0]}</a></div>`}
            </div></div>`;
        })}
        <button type="button" class="textbtn" onClick=${() => setFull(!full)}>
          ${full ? "Today's steps" : `Full plan (${steps.filter((x) => x.done).length}/${steps.length})`}</button>
      </section>
      <${WaitingOn} w=${w} cofounders=${s.cofounders} />
    </div>
  </div>`;
}

function shortTitle(p) {
  const t = p.title || "";
  return t.length > 42 ? t.slice(0, 40).replace(/\s+\S*$/, "") + "…" : t;
}

function WaitingOn({ w, cofounders }) {
  const d = w.data;
  const rows = [];
  upcoming(d).slice(0, 2).forEach((i) => rows.push({ when: whenLabel(i.date), title: "Interview booked",
    sub: [i.role, i.org].filter(Boolean).join(", ") }));
  d.contacts.filter((c) => c.sentAt && !c.replied && c.status !== "committed").forEach((c) => {
    const days = Math.floor((Date.now() - new Date(c.sentAt)) / 86400000);
    rows.push(days >= 3
      ? { when: plural(days, "day"), quiet: true, title: `No reply from ${c.name}`, link: ["Send a follow-up", `workspace/${enc(w.problem_id)}/outreach`, c.id] }
      : { when: new Date(c.sentAt).toLocaleDateString(undefined, { weekday: "short" }), quiet: true, title: `Waiting on ${c.name}`, sub: `Message sent · ${c.org}` });
  });
  (cofounders?.matches || []).filter((m) => m.asked && !m.asked_me).slice(0, 1).forEach((m) =>
    rows.push({ when: "", quiet: true, title: `Intro request to ${m.name}`, sub: "Cofounder match" }));
  (cofounders?.matches || []).filter((m) => m.asked_me && !m.asked).slice(0, 1).forEach((m) =>
    rows.push({ when: "New", title: `${m.name} asked for an intro`, link: ["Answer", "cofounders"] }));
  return html`<section class="card" aria-labelledby="wait-h"><h2 class="sec" id="wait-h">Waiting on</h2>
    ${rows.length ? html`<div>${rows.slice(0, 4).map((r) => html`<div class="wait">
      <span class=${"when" + (r.quiet ? " quiet" : "")}>${r.when}</span>
      <div><div>${r.title}</div>${r.sub && html`<div class="meta">${r.sub}</div>`}
        ${r.link && html`<a href=${"#/" + r.link[1]} onClick=${() => { if (r.link[2]) sessionStorage.setItem("signal_contact", r.link[2]); }}
          style="font-size:14px;text-decoration:underline;text-underline-offset:3px">${r.link[0]}</a>`}</div>
    </div>`)}</div>` : html`<p class="muted">Nothing yet. Booked interviews and unanswered messages show up here.</p>`}
  </section>`;
}

// --- Problems -----------------------------------------------------------------------------------

function MiniBars({ p }) {
  const vals = [["Pain", p.pain?.value], ["Frequency", p.frequency?.value], ["Will pay", p.will_pay?.value]];
  return html`<div class="bars" aria-label=${vals.map(([k, v]) => `${k} ${v ?? "unknown"} of 10`).join(", ")}>
    ${vals.map(([, v]) => html`<span class="bar"><i style=${`width:${(v ?? 0) * 10}%`}></i></span>`)}</div>`;
}

function evidenceLine(p) {
  const parts = [plural(p.posts, "post"), plural(p.buyers, "buyer")];
  if (p.price?.count) parts.push(`$${Math.round(p.price.median).toLocaleString()} median budget`);
  else if (p.tools?.length) parts.push(plural(p.tools.length, "tool") + " named");
  return parts.join(" · ");
}

const ScoreTile = ({ score }) => html`<div class=${"stile" + (score >= 85 ? " hi" : "")} aria-label=${`Score ${score}`}>
  <span>${score}</span><small>score</small></div>`;

function Problems() {
  const s = useStore();
  const [data, setData] = useState(null);
  const [q, setQ] = useState("");
  const [ind, setInd] = useState("");
  const [sel, setSel] = useState(null);
  const [adding, setAdding] = useState(false);
  const [err, setErr] = useState("");
  useEffect(() => {
    let on = true;
    const t = setTimeout(() => api(`/problems?${qs({ user_id: store.userId, q, industry: ind })}`)
      .then((r) => { if (on) { setData(r); setErr(""); } }).catch(() => on && setErr("Couldn't load problems.")), q ? 250 : 0);
    return () => { on = false; clearTimeout(t); };
  }, [q, ind]);
  const saved = new Set((s.items || []).map((w) => w.problem_id));
  const items = data?.items || [];
  const current = items.find((p) => p.id === sel) || items[0];
  const chips = (data?.industries || []).slice(0, 5);
  return html`<div class="page">
    <div class="head"><h1 class="title">Problems</h1>
      <button type="button" class="btn primary" onClick=${() => setAdding(!adding)} aria-expanded=${adding}>Add problem</button></div>
    ${adding && html`<${AddProblem} onDone=${() => setAdding(false)} />`}
    <div class="row" style="align-items:stretch">
      <label class="search"><span class="sr">Search problems</span><${Icon} name="problems" size="18" />
        <input class="input" type="search" placeholder="Search problems, industries, or roles" value=${q}
          onInput=${(e) => setQ(e.target.value)} /></label>
      <div class="chips" role="group" aria-label="Industry">
        <button type="button" class="chip" aria-pressed=${!ind} onClick=${() => setInd("")}>All</button>
        ${chips.map((c) => html`<button type="button" class="chip" aria-pressed=${ind === c.name}
          onClick=${() => setInd(ind === c.name ? "" : c.name)}>${c.name}</button>`)}
        ${ind && !chips.some((c) => c.name === ind) && html`<button type="button" class="chip" aria-pressed="true"
          onClick=${() => setInd("")}>${ind}</button>`}
      </div>
    </div>
    ${err && html`<p class="error">${err}</p>`}
    <div class="cols cols-main">
      <div class="stack" style="gap:12px">
        <div class="listhead"><span class="num">${data ? `${plural(data.total, "problem")} · sorted by score` : "Loading…"}</span>
          <span>Pain · Frequency · Will pay</span></div>
        ${data && !items.length && html`<div class="card"><p class="muted">No problems match.</p></div>`}
        ${items.map((p) => html`<article class="pcard" aria-current=${current?.id === p.id ? "true" : undefined}
            onClick=${(e) => { if (e.target.closest("a")) return; if (matchMedia("(max-width: 1080px)").matches) go(`problems/${enc(p.id)}`); else setSel(p.id); }}>
          <${ScoreTile} score=${p.score} />
          <div style="min-width:0">
            <h3><a href=${`#/problems/${enc(p.id)}`}>${p.title}</a></h3>
            ${saved.has(p.id) && html`<div style="margin-top:6px"><span class="tag info">Saved</span></div>`}
            <p class="muted" style="margin-top:6px">${[p.segment || p.buyer, p.industry].filter(Boolean).join(" · ")}</p>
            <${MiniBars} p=${p} />
            <p class="meta">${evidenceLine(p)}</p>
          </div></article>`)}
      </div>
      ${current && html`<${Preview} p=${current} saved=${saved.has(current.id)} key=${current.id} />`}
    </div>
  </div>`;
}

function Preview({ p, saved }) {
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try { await saveProblem(p.id); say("Saved. It has a workspace now."); } catch { say("Couldn't save. Try again."); }
    setBusy(false);
  };
  return html`<aside class="card preview" aria-label="Selected problem">
    <h2>${p.title}</h2>
    ${p.statement && html`<p class="muted">${p.statement}</p>`}
    <${Stats} p=${p} />
    ${p.evidence?.length > 0 && html`<div><h3 class="ctitle">Evidence</h3>
      <div class="stack" style="gap:10px;margin-top:10px">${p.evidence.map((e) => html`<${EvidenceItem} e=${e} />`)}</div></div>`}
    ${(p.workarounds?.length > 0 || p.tools?.length > 0) && html`<div><h3 class="ctitle">Alternatives</h3>
      <div class="chips" style="margin-top:10px">${(p.tools?.length ? p.tools.slice(0, 4) : p.workarounds.map((w) => w.what))
        .map((t) => html`<span class="tag soft" style="white-space:normal">${t}</span>`)}</div></div>`}
    <div class="row" style="flex-wrap:nowrap">
      <a class="btn primary" style="flex:1" href=${`#/problems/${enc(p.id)}`}>Open full problem</a>
      ${saved ? html`<a class="btn" href=${`#/workspace/${enc(p.id)}`}>Workspace</a>`
        : html`<button type="button" class="btn" disabled=${busy} onClick=${save}>Save</button>`}
    </div>
  </aside>`;
}

function Stats({ p }) {
  const f = p.frequency || {};
  return html`<div class="stats">
    <div class="stat"><div class="k">Pain</div><div class="v">${p.pain?.value != null ? `${p.pain.value}/10` : "–"}</div></div>
    <div class="stat"><div class="k">Frequency</div><div class="v">${f.label || "–"}</div></div>
    <div class="stat"><div class="k">Will pay</div><div class="v">${p.will_pay?.value != null ? `${p.will_pay.value}/10` : "–"}</div></div>
  </div>`;
}

function EvidenceItem({ e }) {
  const url = safeUrl(e.url);
  return html`<div class="quote"><p>${e.summary}</p>
    <p class="meta">${[sourceName(e.source), cap(e.who), e.published_at && ago(e.published_at)].filter(Boolean).join(" · ")}
      ${url && html` · <a href=${url} target="_blank" rel="noopener noreferrer">Original</a>`}</p></div>`;
}

function AddProblem({ onDone }) {
  const [title, setTitle] = useState("");
  const [who, setWho] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      const w = await api("/workspaces", { method: "POST", body: { user_id: store.userId, title, who: who || null } });
      w.data = seed(w); persist(w);
      store.items = [...(store.items || []), w]; notify();
      onDone(); go(`workspace/${enc(w.problem_id)}`);
    } catch { say("Couldn't add it. Try again."); setBusy(false); }
  };
  return html`<form class="card form" onSubmit=${submit} style="max-width:720px">
    <div><label class="lbl" for="np-t">Problem</label>
      <input id="np-t" class="input" required minlength="3" maxlength="200" value=${title} onInput=${(e) => setTitle(e.target.value)}
        placeholder="e.g. Small clinics re-type paper intake forms" /></div>
    <div><label class="lbl" for="np-w">Who has it <span>(optional)</span></label>
      <input id="np-w" class="input" maxlength="200" value=${who} onInput=${(e) => setWho(e.target.value)} /></div>
    <div class="row"><button class="btn primary" disabled=${busy}>Add</button>
      <button type="button" class="btn" onClick=${onDone}>Cancel</button></div>
  </form>`;
}

// --- Problem detail -----------------------------------------------------------------------------

function ProblemPage({ id }) {
  const s = useStore();
  const [p, setP] = useState(null);
  const [err, setErr] = useState("");
  const [filter, setFilter] = useState("All");
  const [all, setAll] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api(`/problems/${enc(id)}?${qs({ user_id: store.userId })}`).then(setP)
      .catch((e) => setErr(e.status === 404 ? "This problem is no longer in the bank." : "Couldn't load it."));
  }, [id]);
  if (err) return html`<div class="page"><a class="backlink" href="#/problems"><${Back} />All problems</a><p class="muted">${err}</p></div>`;
  if (!p) return html`<div class="page"><p class="center">Loading…</p></div>`;
  const ws = (s.items || []).find((w) => w.problem_id === p.id);
  const save = async () => {
    setBusy(true);
    try { const w = await saveProblem(p.id, note); go(`workspace/${enc(w.problem_id)}`); }
    catch { say("Couldn't save. Try again."); setBusy(false); }
  };
  const groups = ["All", ...new Set(p.evidence.map((e) => e.group))];
  const ev = p.evidence.filter((e) => filter === "All" || e.group === filter);
  const f = p.frequency || {};
  const tiles = [
    ["Pain", p.pain?.value != null ? `${p.pain.value}/10` : "–", p.pain?.reason],
    ["Frequency", f.label || "–", f.reason],
    ["Will pay", p.will_pay?.value != null ? `${p.will_pay.value}/10` : "–", p.will_pay?.reason],
    ["Competition", p.competition?.label || "–", p.competition?.reason],
  ];
  const share = async () => { if (await copy(location.href)) say("Link copied."); };
  return html`<div class="page">
    <div><a class="backlink" href="#/problems"><${Back} />All problems</a></div>
    <div class="stack" style="gap:12px">
      <div class="tags"><span class="tag accent">Score ${p.score}</span>${p.industry && html`<span class="tag">${p.industry}</span>`}
        ${p.buyer && html`<span class="tag">${p.buyer}</span>`}</div>
      <h1 class="detail-title">${p.title}</h1>
      <div class="between" style="align-items:flex-end">
        <div class="stack" style="gap:6px;max-width:760px">${p.statement && html`<p class="lede">${p.statement}</p>`}
          ${p.summary && html`<p class="muted">${p.summary}</p>`}</div>
        <div class="row"><button type="button" class="btn" onClick=${share}>Share</button>
          ${ws ? html`<a class="btn primary" href=${`#/workspace/${enc(p.id)}`}>Open workspace</a>`
            : html`<button type="button" class="btn primary" disabled=${busy} onClick=${save}><${BookmarkIcon} />Save problem</button>`}</div>
      </div>
    </div>
    <div class="cols cols-detail stagger">
      <div class="stack">
        <section class="card" aria-labelledby="sc-h"><h2 class="sec" id="sc-h">Score</h2>
          <div class="stats stats4">${tiles.map(([k, v, why]) => html`<div class="stat"><div class="k">${k}</div>
            <div class="v">${v}</div>${why && html`<div class="why">${why}</div>`}</div>`)}</div></section>
        <section class="card" aria-labelledby="ev-h">
          <div class="between"><h2 class="sec" id="ev-h">Evidence</h2>
            <div class="chips" role="group" aria-label="Filter by source">${groups.map((g) => html`<button type="button" class="chip"
              aria-pressed=${filter === g} onClick=${() => setFilter(g)}>${g === "All" ? `All · ${p.evidence.length}` : g}</button>`)}</div></div>
          <div class="stack" style="gap:12px">${(all ? ev : ev.slice(0, 3)).map((e) => html`<${EvidenceItem} e=${e} />`)}</div>
          ${ev.length > 3 && !all && html`<button type="button" class="textbtn" onClick=${() => setAll(true)}>Show all ${ev.length}</button>`}
        </section>
        ${(p.workarounds.length > 0 || p.tools.length > 0) && html`<section class="card" aria-labelledby="alt-h">
          <h2 class="sec" id="alt-h">Alternatives</h2>
          <div>${p.workarounds.map((w) => html`<div class="alt"><span>${w.what}</span><span class="meta">${sourceName(w.source)}</span></div>`)}</div>
          ${p.tools.length > 0 && html`<div><p class="meta" style="margin-bottom:8px">Tools the posts name</p>
            <div class="chips">${p.tools.map((t) => html`<span class="tag soft">${t}</span>`)}</div></div>`}
        </section>`}
        <${LiveCard} id=${p.id} />
      </div>
      <div class="stack">
        ${ws ? html`<section class="card dark"><h2 class="sec">Saved</h2><p class="muted">It has a workspace and a plan on Home.</p>
            <a class="btn accent wide" href=${`#/workspace/${enc(p.id)}`}>Open workspace</a></section>`
          : html`<section class="card dark" aria-labelledby="sv-h"><h2 class="sec" id="sv-h">Save</h2>
            <div><label class="lbl" for="sv-note">Note <span>(optional)</span></label>
              <textarea id="sv-note" class="input" maxlength="2000" placeholder="e.g. I worked in this job for two years" value=${note}
                onInput=${(e) => setNote(e.target.value)}></textarea></div>
            <button type="button" class="btn accent wide" disabled=${busy} onClick=${save}>Save problem</button></section>`}
        <section class="card tight" aria-labelledby="who-h"><h2 class="ctitle" id="who-h">Who has it</h2>
          <div>${[["Role", p.audience.role], ["Segment", p.audience.segment], ["Who pays", p.audience.payer],
                  ["Where they talk", p.audience.channels.map(sourceName).join(", ")]].filter(([, v]) => v)
            .map(([k, v]) => html`<div class="kv"><span>${k}</span><span>${v}</span></div>`)}</div></section>
        ${p.open_questions.length > 0 && html`<section class="card tight" aria-labelledby="oq-h"><h2 class="ctitle" id="oq-h">Open questions</h2>
          <ul class="bullets">${p.open_questions.map((q) => html`<li>${q}</li>`)}</ul></section>`}
      </div>
    </div>
  </div>`;
}

// Search results from platforms Signal can't store (Reddit, YouTube), fetched now and never saved.
function LiveCard({ id }) {
  const [live, setLive] = useState(null);
  useEffect(() => {
    let on = true;
    api(`/problems/${enc(id)}/live?${qs({ user_id: store.userId })}`).then((r) => on && setLive(r.sections || []))
      .catch(() => on && setLive([]));
    return () => { on = false; };
  }, [id]);
  if (live && !live.length) return null;
  return html`<section class="card live" aria-labelledby="live-h" aria-busy=${!live}>
    <div class="between"><h2 class="sec" id="live-h">Elsewhere right now</h2><span class="meta">${live ? "Live results, not saved" : "Searching…"}</span></div>
    ${(live || []).map((sec) => html`<div><h3 class="ctitle">${sec.label}</h3>
      ${sec.items.map((i) => html`<div class="alt" style="grid-template-columns:minmax(0,1fr)">
        <a href=${safeUrl(i.url)} target="_blank" rel="noopener noreferrer">${i.title}</a>
        ${i.snippet && html`<span class="meta">${i.snippet}</span>`}</div>`)}</div>`)}
  </section>`;
}

// --- Workspace ----------------------------------------------------------------------------------

const TABS = [["outreach", "Outreach"], ["interviews", "Interviews"], ["assumptions", "Assumptions"], ["brief", "Brief"], ["pilot", "Pilot"]];

function Workspace({ id, tab, me }) {
  const s = useStore();
  const items = s.items;
  const [shownTab, leaving] = useLeaving(tab, tab);
  if (!items) return html`<div class="page"><p class="center">Loading…</p></div>`;
  const w = items.find((x) => x.problem_id === id) || items[0];
  if (!w) {
    return html`<div class="page"><div><p class="date">Workspace</p><h1 class="ws-title">No saved problems</h1></div>
      <div class="card" style="max-width:640px"><p class="muted">Save a problem and its workspace opens here.</p>
        <a class="btn primary" href="#/problems">Browse problems</a></div></div>`;
  }
  const d = w.data;
  const n = pastInterviews(d).length;
  const pid = enc(w.problem_id);
  const views = { outreach: Outreach, interviews: Interviews, assumptions: Assumptions, brief: Brief, pilot: Pilot };
  const View = views[shownTab] || Outreach;
  return html`<div class="page">
    <div class="head no-print">
      <div style="min-width:0"><p class="date">Workspace</p><h1 class="ws-title" style="max-width:820px">${w.problem.title}</h1></div>
      <div class="switch"><label for="ws-switch">Switch problem</label>
        <select id="ws-switch" class="input" value=${w.problem_id} onChange=${(e) => go(`workspace/${enc(e.target.value)}/${tab}`)}>
          ${items.map((x) => html`<option value=${x.problem_id}>${shortTitle(x.problem)}</option>`)}</select></div>
    </div>
    ${!w.in_bank && !w.problem.own && html`<p class="meta">This problem left the bank in the last rebuild. Your work on it is kept.</p>`}
    <nav class="tabs no-print" aria-label="Workspace">${TABS.map(([k, label]) => html`<a href=${`#/workspace/${pid}/${k}`}
      aria-current=${tab === k ? "page" : undefined}>${k === "interviews" && n ? `${label} · ${n}` : label}</a>`)}</nav>
    <div class=${leaving ? "leaving" : ""} key=${w.problem_id + shownTab}>
      <${View} w=${w} d=${d} me=${me} />
    </div>
  </div>`;
}

function QuickNote({ w }) {
  return html`<section class="card tight no-print"><label class="ctitle" for="qn" style="display:block;margin-bottom:12px">Quick note</label>
    <textarea id="qn" class="input" placeholder="Anything worth remembering…" value=${w.data.notes}
      onInput=${(e) => edit(w, (d) => { d.notes = e.target.value; })}></textarea></section>`;
}

// Outreach

function draft(c, w, me) {
  const first = /^[A-Z][a-z]+$/.test((c.name || "").split(" ")[0]) && c.how !== "from a post" ? c.name.split(" ")[0] : "there";
  const topic = low(w.problem.title || "this problem");
  const seen = c.sourceUrl ? `I saw your post on ${c.org}. I'm researching a problem it touches on: ${topic}.`
    : `I'm researching a problem: ${topic}.`;
  return `Hi ${first},\n\n${seen} I'm talking with people who deal with it.\n\n`
    + "I'm not selling anything. Would you have 20 minutes this week to tell me how you handle it today?\n\n"
    + `Thanks,\n${displayName(me)}`;
}

function shorten(text) {
  const parts = text.split(/\n\s*\n/);
  if (parts.length <= 3) return text.split(/(?<=[.?!])\s+/).slice(0, 4).join(" ");
  return [parts[0], parts[parts.length - 2], parts[parts.length - 1]].join("\n\n");
}

function Outreach({ w, d, me }) {
  const [selId, setSelId] = useState(() => sessionStorage.getItem("signal_contact") || null);
  const [adding, setAdding] = useState(false);
  const [learning, setLearning] = useState("");
  useEffect(() => { sessionStorage.removeItem("signal_contact"); }, []);
  const notC = d.contacts.filter((c) => c.status !== "committed");
  const comm = d.contacts.filter((c) => c.status === "committed");
  const c = d.contacts.find((x) => x.id === selId) || notC[0] || comm[0];
  const goal = goalOf(d);
  const person = (x) => html`<button type="button" class="person" aria-current=${x === c ? "true" : undefined} onClick=${() => setSelId(x.id)}>
    <span style="min-width:0"><b>${x.name}</b><small>${[x.role, x.org, x.how].filter(Boolean).join(" · ")}</small></span>
    ${x.status === "committed" ? html`<span class="tag accent">${d.pilots[x.id]?.status ? "Pilot" : "In"}</span>`
      : x.sentAt ? html`<span class="meta">${x.replied ? "Replied" : "Sent"}</span>` : null}</button>`;
  return html`<div class="cols cols-out stagger">
    <section class="card tight" aria-labelledby="pp-h">
      <div class="between"><h2 class="ctitle" id="pp-h">People</h2>
        <button type="button" class="btn small" onClick=${() => setAdding(!adding)} aria-expanded=${adding}><${Plus} />Add</button></div>
      ${adding && html`<${AddContact} w=${w} onDone=${(nid) => { setAdding(false); if (nid) setSelId(nid); }} />`}
      <div><p class="group">Not contacted · ${notC.filter((x) => !x.sentAt).length}</p>
        ${notC.map(person)}
        ${!notC.length && html`<p class="meta">Add people who have this problem.</p>`}
        <p class="group">Committed · ${comm.length} of ${goal}</p>
        ${comm.map(person)}</div>
    </section>
    ${c ? html`<${Composer} w=${w} c=${c} me=${me} key=${c.id} />`
      : html`<section class="card"><h2 class="sec">Message</h2><p class="muted">Add someone to write to.</p></section>`}
    <div class="stack">
      <section class="card tight" aria-labelledby="ln-h"><h2 class="ctitle" id="ln-h">Learnings</h2>
        ${d.learnings.length > 0 && html`<ul class="bullets">${d.learnings.map((l, i) => html`<li>${l}
          <button type="button" class="textbtn" style="min-height:24px;font-size:13px" aria-label=${"Remove learning: " + l}
            onClick=${() => edit(w, (dd) => { dd.learnings.splice(i, 1); })}>Remove</button></li>`)}</ul>`}
        <form class="row" style="flex-wrap:nowrap" onSubmit=${(e) => { e.preventDefault(); if (!learning.trim()) return;
          edit(w, (dd) => { dd.learnings.push(learning.trim()); }); setLearning(""); }}>
          <label class="sr" for="ln-in">Add a learning</label>
          <input id="ln-in" class="input" placeholder="Add a learning" value=${learning} onInput=${(e) => setLearning(e.target.value)} />
          <button class="btn small" style="min-height:44px">Add</button></form>
      </section>
      <${QuickNote} w=${w} />
    </div>
  </div>`;
}

function AddContact({ w, onDone }) {
  const [f, setF] = useState({ name: "", role: "", org: "", email: "" });
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const submit = (e) => {
    e.preventDefault();
    const id = uid();
    edit(w, (d) => { d.contacts.push({ id, ...f, how: "", source: "", sourceUrl: "", status: "not", channel: f.email ? "email" : "linkedin",
      subject: "", body: "", sentAt: null, replied: false }); });
    onDone(id);
  };
  return html`<form class="form" onSubmit=${submit} style="margin-top:12px">
    <div><label class="lbl" for="ac-n">Name</label><input id="ac-n" class="input" required maxlength="80" value=${f.name} onInput=${set("name")} /></div>
    <div><label class="lbl" for="ac-r">Role</label><input id="ac-r" class="input" maxlength="80" value=${f.role} onInput=${set("role")} /></div>
    <div><label class="lbl" for="ac-o">Organization</label><input id="ac-o" class="input" maxlength="80" value=${f.org} onInput=${set("org")} /></div>
    <div><label class="lbl" for="ac-e">Email <span>(optional)</span></label><input id="ac-e" class="input" type="email" maxlength="200" value=${f.email} onInput=${set("email")} /></div>
    <div class="row"><button class="btn primary small">Add</button><button type="button" class="btn small" onClick=${() => onDone(null)}>Cancel</button></div>
  </form>`;
}

function Composer({ w, c, me }) {
  const upd = (fn) => edit(w, (d) => { fn(d.contacts.find((x) => x.id === c.id), d); });
  const body = c.body || draft(c, w, me);
  const subject = c.subject || (c.sourceUrl ? `Your post on ${c.org}` : `Quick question about ${low(shortTitle(w.problem))}`);
  const channels = [["email", "Email"], ["linkedin", "LinkedIn"], ["forum", c.sourceUrl ? `Reply on ${c.org}` : "Forum message"]];
  const send = async () => {
    const ok = await copy(body);
    if (c.channel === "email") {
      if (!c.email) { say("Add their email first."); return; }
      location.href = `mailto:${enc(c.email)}?subject=${enc(subject)}&body=${enc(body)}`;
    } else if (c.channel === "forum" && safeUrl(c.sourceUrl)) {
      open(c.sourceUrl, "_blank", "noopener");
      say(ok ? "Message copied. Paste it as a reply." : "Copy the message, then paste it as a reply.");
    } else {
      say(ok ? "Message copied. Paste it in your message." : "Copy the message first.");
    }
    upd((x) => { x.sentAt = new Date().toISOString(); });
  };
  return html`<section class="card" aria-labelledby="msg-h">
    <div class="between"><h2 class="sec" id="msg-h">Message to ${c.name}</h2>
      ${c.status === "committed"
        ? html`<button type="button" class="textbtn" onClick=${() => upd((x) => { x.status = "not"; })}>Move back</button>`
        : html`<button type="button" class="btn small" onClick=${() => upd((x) => { x.status = "committed"; })}>Committed</button>`}</div>
    ${c.sourceUrl ? html`<p class="srcbox">Source: their post on ${c.org}${c.sourceAt ? `, ${ago(c.sourceAt)}` : ""}. ${c.source}
        ${" "}<a href=${safeUrl(c.sourceUrl)} target="_blank" rel="noopener noreferrer">Open post</a></p>`
      : c.how || c.role ? html`<p class="srcbox">Source: ${[c.role, c.org, c.how].filter(Boolean).join(" · ")}</p>` : null}
    ${c.sentAt && html`<p class="meta">Sent ${dayLabel(c.sentAt)}${c.replied ? " · replied" : ""}
      ${!c.replied && html` · <button type="button" class="textbtn" style="min-height:24px" onClick=${() => upd((x) => { x.replied = true; })}>They replied</button>`}</p>`}
    <div class="form2">
      <div><label class="lbl" for="sv">Send via</label>
        <select id="sv" class="input" value=${c.channel} onChange=${(e) => upd((x) => { x.channel = e.target.value; })}>
          ${channels.map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select></div>
      <div><label class="lbl" for="sj">Subject</label>
        <input id="sj" class="input" value=${subject} onInput=${(e) => upd((x) => { x.subject = e.target.value; })} /></div>
    </div>
    ${c.channel === "email" && html`<div><label class="lbl" for="em">Their email</label>
      <input id="em" class="input" type="email" value=${c.email} onInput=${(e) => upd((x) => { x.email = e.target.value; })} /></div>`}
    <div><label class="lbl" for="mb">Message</label>
      <textarea id="mb" class="input" style="min-height:220px" value=${body} onInput=${(e) => upd((x) => { x.body = e.target.value; })}></textarea></div>
    <div class="between"><div class="row">
      <button type="button" class="btn" onClick=${() => upd((x) => { x.body = shorten(body); })}>Shorten</button>
      <button type="button" class="btn" onClick=${async () => say((await copy(body)) ? "Copied." : "Couldn't copy.")}>Copy</button></div>
      <button type="button" class="btn primary" onClick=${send}>Send</button></div>
  </section>`;
}

// Interviews

function Interviews({ w, d }) {
  const [theme, setTheme] = useState(null);
  const [logging, setLogging] = useState(false);
  const [older, setOlder] = useState(false);
  const past = pastInterviews(d);
  const counts = {};
  past.forEach((i) => (i.themes || []).forEach((t) => { counts[t] = (counts[t] || 0) + 1; }));
  const themes = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const total = past.length;
  const list = theme ? past.filter((i) => (i.themes || []).includes(theme)) : past;
  const next = upcoming(d)[0];
  return html`<div class="cols cols-ws stagger">
    <div class="stack">
      ${themes.length > 0 && html`<section class="card" aria-labelledby="th-h"><h2 class="sec" id="th-h">Themes</h2>
        <div>${themes.map(([t, n]) => {
          const on = Math.round((n / Math.max(total, 1)) * 5);
          return html`<button type="button" class="theme" aria-pressed=${theme === t} onClick=${() => setTheme(theme === t ? null : t)}>
            <span>${t}</span><span class="segs" aria-hidden="true">${[0, 1, 2, 3, 4].map((k) => html`<i class=${k < on ? "on" : ""}></i>`)}</span>
            <span class="meta">${n} of ${total}</span></button>`;
        })}</div></section>`}
      <div class="between"><h2 class="sec">Interviews${theme ? ` · ${theme}` : ""}</h2>
        <button type="button" class="btn primary" onClick=${() => setLogging(!logging)} aria-expanded=${logging}>Log interview</button></div>
      ${logging && html`<${LogInterview} w=${w} onDone=${() => setLogging(false)} />`}
      ${!list.length && !logging && html`<div class="card"><p class="muted">No interviews yet. Log one after each call; tag what you heard
        and the themes add up here.</p></div>`}
      ${(older ? list : list.slice(0, 3)).map((i) => html`<article class="card tight iv">
        <div class="between" style="align-items:flex-start"><h3 class="ctitle">${[i.org, i.role].filter(Boolean).join(" · ") || "Interview"}</h3>
          <span class="meta">${dayLabel(i.date)}${i.minutes ? ` · ${i.minutes} min` : ""}</span></div>
        ${i.summary && html`<p>${i.summary}</p>`}
        ${i.quote && html`<p class="q">“${i.quote}”${(i.themes || []).map((t) => html` <span class="hash">#${t.replace(/\s+/g, "")}</span>`)}</p>`}
        <button type="button" class="textbtn" style="min-height:28px;font-size:13px" onClick=${() => edit(w, (dd) => {
          dd.interviews = dd.interviews.filter((x) => x.id !== i.id); })}>Delete</button>
      </article>`)}
      ${list.length > 3 && !older && html`<button type="button" class="textbtn" onClick=${() => setOlder(true)}>
        Show ${plural(list.length - 3, "older interview")}</button>`}
    </div>
    <div class="stack">
      <section class="card dark" aria-labelledby="nx-h">
        ${next ? html`<p class="meta" style="color:#9DBEFF">${whenLabel(next.date)}</p>
            <h2 class="ctitle" id="nx-h" style="font-size:18px">${next.org || next.role || "Next interview"}</h2>`
          : html`<h2 class="ctitle" id="nx-h" style="font-size:18px">Next interview</h2><p class="muted">Not booked yet.</p>`}
        ${d.questions.length > 0 && html`<ul class="bullets">${d.questions.map((q, k) => html`<li>${q}
          <button type="button" class="textbtn" style="min-height:24px;font-size:13px" aria-label=${"Remove question: " + q}
            onClick=${() => edit(w, (dd) => { dd.questions.splice(k, 1); })}>Remove</button></li>`)}</ul>`}
        ${!next && html`<button type="button" class="btn ghost-dark wide" onClick=${() => setLogging(true)}>Book one</button>`}
      </section>
      <${QuickNote} w=${w} />
    </div>
  </div>`;
}

function LogInterview({ w, onDone }) {
  const d = w.data;
  const [f, setF] = useState({ contact: "", org: "", role: "", date: todayISO(), time: "", minutes: "", summary: "", quote: "", themes: "" });
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const known = [...new Set(d.interviews.flatMap((i) => i.themes || []))];
  const submit = (e) => {
    e.preventDefault();
    const c = d.contacts.find((x) => x.id === f.contact);
    const it = { id: uid(), contactId: c?.id || null, org: f.org || c?.org || "", role: f.role || c?.role || c?.name || "",
      date: f.time ? `${f.date}T${f.time}` : f.date, minutes: parseInt(f.minutes, 10) || null, summary: f.summary.trim(),
      quote: f.quote.trim(), themes: f.themes.split(",").map((t) => cap(t.trim())).filter(Boolean) };
    edit(w, (dd) => { dd.interviews.push(it); });
    onDone();
  };
  return html`<form class="card form" onSubmit=${submit}>
    ${d.contacts.length > 0 && html`<div><label class="lbl" for="li-c">Person <span>(optional)</span></label>
      <select id="li-c" class="input" value=${f.contact} onChange=${set("contact")}><option value="">Someone else</option>
        ${d.contacts.map((c) => html`<option value=${c.id}>${c.name}${c.org ? ` · ${c.org}` : ""}</option>`)}</select></div>`}
    <div class="form2"><div><label class="lbl" for="li-o">Organization</label><input id="li-o" class="input" value=${f.org} onInput=${set("org")} /></div>
      <div><label class="lbl" for="li-r">Role</label><input id="li-r" class="input" value=${f.role} onInput=${set("role")} /></div></div>
    <div class="form2"><div><label class="lbl" for="li-d">Date <span>(a future date books it)</span></label>
        <input id="li-d" class="input" type="date" required value=${f.date} onInput=${set("date")} /></div>
      <div class="form2"><div><label class="lbl" for="li-t">Time</label><input id="li-t" class="input" type="time" value=${f.time} onInput=${set("time")} /></div>
        <div><label class="lbl" for="li-m">Minutes</label><input id="li-m" class="input" type="number" min="1" max="600" value=${f.minutes} onInput=${set("minutes")} /></div></div></div>
    <div><label class="lbl" for="li-s">Summary</label><textarea id="li-s" class="input" maxlength="600" value=${f.summary} onInput=${set("summary")}></textarea></div>
    <div><label class="lbl" for="li-q">Quote <span>(optional)</span></label><input id="li-q" class="input" maxlength="300" value=${f.quote} onInput=${set("quote")} /></div>
    <div><label class="lbl" for="li-th">Themes <span>(comma-separated${known.length ? `: ${known.slice(0, 4).join(", ")}` : ""})</span></label>
      <input id="li-th" class="input" value=${f.themes} onInput=${set("themes")} /></div>
    <div class="row"><button class="btn primary">Save</button><button type="button" class="btn" onClick=${onDone}>Cancel</button></div>
  </form>`;
}

// Assumptions

const STATUS = { confirmed: ["Confirmed", "accent"], wrong: ["Wrong", "warn"], untested: ["Untested", "dashed"] };
const NEXT_STATUS = { untested: "confirmed", confirmed: "wrong", wrong: "untested" };

function Assumptions({ w, d }) {
  const [editing, setEditing] = useState(null);
  const [text, setText] = useState("");
  const count = (k) => d.assumptions.filter((a) => a.status === k).length;
  const risk = riskiest(d);
  const add = (e) => {
    e.preventDefault();
    if (!text.trim()) return;
    edit(w, (dd) => { dd.assumptions.push({ id: uid(), text: text.trim(), status: "untested", note: "", test: "", riskiest: false }); });
    setText("");
  };
  const upd = (id, fn) => edit(w, (dd) => { fn(dd.assumptions.find((a) => a.id === id), dd); });
  return html`<div class="cols cols-ws stagger">
    <section class="card" aria-labelledby="as-h">
      <div><h2 class="sec" id="as-h">Assumptions</h2>
        <p class="meta">${count("confirmed")} confirmed · ${count("wrong")} wrong · ${count("untested")} untested</p></div>
      <form class="row" style="flex-wrap:nowrap" onSubmit=${add}><label class="sr" for="as-new">New assumption</label>
        <input id="as-new" class="input" placeholder="Something you believe but haven't checked" value=${text} onInput=${(e) => setText(e.target.value)} />
        <button class="btn primary">Add assumption</button></form>
      <div>${d.assumptions.map((a) => {
        const [label, cls] = STATUS[a.status];
        const isRisk = risk && a.id === risk.id && a.status === "untested";
        return html`<div class="asm">
          <button type="button" class=${"tag " + cls} title="Change status" aria-label=${`Status ${label}. Change it`}
            onClick=${() => upd(a.id, (x) => { x.status = NEXT_STATUS[x.status]; })}>${label}</button>
          <div style="min-width:0">${a.status === "wrong" ? html`<s>${a.text}</s>` : html`<span>${a.text}</span>`}
            <p class="meta">${a.status === "untested" ? (a.test ? `Test: ${a.test}` : "") : a.note}</p></div>
          <div class="row" style="gap:6px">${isRisk && html`<span class="tag dark">Riskiest</span>`}
            <button type="button" class="textbtn" style="min-height:32px" onClick=${() => setEditing(editing === a.id ? null : a.id)}>Edit</button></div>
          ${editing === a.id && html`<div class="edit form">
            <div><label class="lbl" for=${"at-" + a.id}>Assumption</label><input id=${"at-" + a.id} class="input" value=${a.text}
              onInput=${(e) => upd(a.id, (x) => { x.text = e.target.value; })} /></div>
            <div><label class="lbl" for=${"ap-" + a.id}>${a.status === "untested" ? "How you'll test it" : "What you learned"}</label>
              <input id=${"ap-" + a.id} class="input" value=${a.status === "untested" ? a.test : a.note}
                onInput=${(e) => upd(a.id, (x) => { if (x.status === "untested") x.test = e.target.value; else x.note = e.target.value; })} /></div>
            <div class="row"><label class="row" style="gap:8px;min-height:44px"><input type="checkbox" class="check" checked=${a.riskiest}
                onChange=${(e) => upd(a.id, (x, dd) => { dd.assumptions.forEach((y) => { y.riskiest = false; }); x.riskiest = e.target.checked; })} />Riskiest</label>
              <button type="button" class="btn small" onClick=${() => setEditing(null)}>Done</button>
              <button type="button" class="textbtn" onClick=${() => { edit(w, (dd) => { dd.assumptions = dd.assumptions.filter((x) => x.id !== a.id); }); setEditing(null); }}>Delete</button></div>
          </div>`}
        </div>`;
      })}</div>
    </section>
    <div class="stack">
      ${risk && html`<section class="card dark" aria-labelledby="rk-h"><p class="meta" style="color:#9DBEFF">Riskiest</p>
        <h2 class="ctitle" id="rk-h" style="font-size:19px">${risk.text}</h2>
        ${d.questions.includes(risk.text) ? html`<p class="muted">On the list for your next interview.</p>`
          : html`<button type="button" class="btn accent wide" onClick=${() => edit(w, (dd) => { dd.questions.push(risk.text); })}>Add to next interview</button>`}
      </section>`}
      <${QuickNote} w=${w} />
    </div>
  </div>`;
}

// Brief

function Brief({ w, d }) {
  const [editing, setEditing] = useState(null);
  const [val, setVal] = useState("");
  const done = SECTIONS.filter(([k]) => (d.brief[k] || "").trim()).length;
  const nInterviews = pastInterviews(d).length;
  const start = (k) => { setEditing(k); setVal(d.brief[k] || ""); };
  const save = (k) => {
    edit(w, (dd) => {
      dd.brief[k] = val.trim(); dd.briefBy[k] = true; dd.briefAt = new Date().toISOString();
      dd.briefSrc[k] = nInterviews ? plural(nInterviews, "interview") : (dd.briefSrc[k] || "");
    });
    setEditing(null);
  };
  const asText = () => [w.problem.title, "", ...SECTIONS.flatMap(([k, l]) => [l, d.brief[k] || "[EMPTY]", ""])].join("\n");
  return html`<div class="cols cols-ws stagger">
    <article class="card doc" aria-labelledby="br-h">
      <p class="meta">${d.briefAt ? `Updated ${dayLabel(d.briefAt)}` : "Draft from the evidence"}</p>
      <h2 id="br-h">${w.problem.title}</h2>
      ${SECTIONS.map(([k, label]) => {
        const text = (d.brief[k] || "").trim();
        if (editing === k) {
          return html`<div class="docsec form"><label class="ctitle" for=${"bf-" + k}>${label}</label>
            <textarea id=${"bf-" + k} class="input" style="min-height:140px" value=${val} onInput=${(e) => setVal(e.target.value)}></textarea>
            <div class="row no-print"><button type="button" class="btn primary small" onClick=${() => save(k)}>Save</button>
              <button type="button" class="btn small" onClick=${() => setEditing(null)}>Cancel</button></div></div>`;
        }
        if (!text) {
          return html`<div class="docsec"><div class="empty"><h3 class="ctitle">${label}</h3><p class="muted" style="margin:4px 0 10px">Empty</p>
            <button type="button" class="btn small no-print" onClick=${() => start(k)}>Write</button></div></div>`;
        }
        return html`<div class="docsec"><div class="between"><h3>${label}</h3><span class="meta">${d.briefSrc[k] || ""}
            <button type="button" class="textbtn no-print" style="min-height:28px;margin-left:8px" onClick=${() => start(k)}>Edit</button></span></div>
          <p>${text}</p></div>`;
      })}
    </article>
    <div class="stack no-print">
      <section class="card tight"><h2 class="ctitle">${done} of 5 sections done</h2>
        <span class="segs wide" aria-hidden="true">${[0, 1, 2, 3, 4].map((k) => html`<i class=${k < done ? "on" : ""}></i>`)}</span>
        <div class="row" style="flex-wrap:nowrap"><button type="button" class="btn" style="flex:1" onClick=${() => print()}>Export PDF</button>
          <button type="button" class="btn primary" style="flex:1" onClick=${async () => say((await copy(asText())) ? "Brief copied as text." : "Couldn't copy.")}>Share</button></div>
      </section>
      <${QuickNote} w=${w} />
    </div>
  </div>`;
}

// Pilot

const OFFER = [["price", "Price", "[YOUR PRICE]/mo"], ["weeks", "Length", "[LENGTH] weeks"], ["success", "Success means", "[WHAT CHANGES]"],
               ["goal", "Goal", "[NUMBER] pilots"]];
const CHECKLIST = ["Agreed on terms", "Measure the problem before you start", "Book kickoff call"];

function offerText(d, w) {
  const o = d.offer;
  return `Pilot offer: ${w.problem.title}\n\nPrice: ${o.price ? `${o.price}/mo` : "[YOUR PRICE]/mo"}\nLength: ${o.weeks ? `${o.weeks} weeks` : "[LENGTH]"}`
    + `\nSuccess means: ${o.success || "[WHAT CHANGES]"}`;
}

function Pilot({ w, d }) {
  const [editing, setEditing] = useState(false);
  const comm = d.contacts.filter((c) => c.status === "committed");
  const goal = goalOf(d);
  const left = Math.max(0, goal - comm.length);
  const suggest = d.contacts.find((c) => c.status !== "committed" && c.replied) || d.contacts.find((c) => c.status !== "committed");
  const updPilot = (cid, fn) => edit(w, (dd) => {
    dd.pilots[cid] = dd.pilots[cid] || { status: "", checklist: CHECKLIST.map((t) => ({ text: t, done: false })) };
    fn(dd.pilots[cid]);
  });
  const sendOffer = () => {
    if (!suggest) { go(`workspace/${enc(w.problem_id)}/outreach`); return; }
    edit(w, (dd) => {
      const c = dd.contacts.find((x) => x.id === suggest.id);
      c.subject = `A pilot for ${low(shortTitle(w.problem))}`;
      c.body = `Hi ${c.name.split(" ")[0]},\n\nThanks for talking with me. I'm starting a small pilot and would like you in it.\n\n${offerText(dd, w)}\n\nWould you like to join?\n\nThanks`;
    });
    sessionStorage.setItem("signal_contact", suggest.id);
    go(`workspace/${enc(w.problem_id)}/outreach`);
  };
  return html`<div class="stack stagger">
    <section class="card" aria-labelledby="of-h">
      <div class="between"><h2 class="sec" id="of-h">Offer</h2>
        <div class="row"><button type="button" class="btn" onClick=${() => setEditing(!editing)}>${editing ? "Done" : "Edit"}</button>
          <button type="button" class="btn" onClick=${async () => say((await copy(offerText(d, w))) ? "Offer copied." : "Couldn't copy.")}>Copy</button></div></div>
      <div class="offer">${OFFER.map(([k, label, ph]) => html`<div class="stat"><div class="k">${label}</div>
        ${editing ? html`<label class="sr" for=${"of-" + k}>${label}</label><input id=${"of-" + k} class="input" value=${d.offer[k]}
            placeholder=${ph} onInput=${(e) => edit(w, (dd) => { dd.offer[k] = e.target.value; })} />`
          : html`<div class=${k === "success" ? "why" : "v"} style=${k === "success" ? "font-size:15px;font-weight:500;color:var(--ink)" : ""}>
            ${d.offer[k] ? (k === "price" ? `${d.offer[k]}/mo` : k === "weeks" ? `${d.offer[k]} weeks` : k === "goal" ? plural(goalOf(d), "pilot") : d.offer[k]) : ph}</div>`}
      </div>`)}</div>
    </section>
    <div class="pilots">
      ${comm.map((c) => {
        const p = d.pilots[c.id] || { status: "", checklist: CHECKLIST.map((t) => ({ text: t, done: false })) };
        return html`<section class="card tight" aria-label=${c.name}>
          <div class="between" style="align-items:flex-start"><div><h3 class="ctitle">${c.name}</h3>
            <p class="meta">${[c.role, c.org].filter(Boolean).join(" · ")}</p></div>
            <span class=${"tag " + (p.status ? "info" : "accent")}>${p.status || "Committed"}</span></div>
          <div><label class="lbl" for=${"ps-" + c.id}>Status</label>
            <input id=${"ps-" + c.id} class="input" placeholder="e.g. Starts Jan 12, Letter of intent" value=${p.status}
              onInput=${(e) => updPilot(c.id, (x) => { x.status = e.target.value; })} /></div>
          <div>${p.checklist.map((it, k) => html`<label class="checkrow"><input type="checkbox" class="check" checked=${it.done}
            onChange=${() => updPilot(c.id, (x) => { x.checklist[k].done = !x.checklist[k].done; })} />${it.text}</label>`)}</div>
        </section>`;
      })}
      ${left > 0 && html`<section class="card dashed tight" aria-label="Open pilot slot" style="display:flex;flex-direction:column;justify-content:center">
        <h3 class="ctitle">${plural(left, "more pilot")} needed</h3>
        ${suggest && html`<p class="muted">Suggested: ${suggest.name}${suggest.org ? ` · ${suggest.org}` : ""}</p>`}
        <div><button type="button" class="btn primary" onClick=${sendOffer}>${suggest ? "Send them the offer" : "Find people"}</button></div>
      </section>`}
    </div>
  </div>`;
}

// --- Cofounders ---------------------------------------------------------------------------------

const TINTS = [["#E8EDFF", "#16288A"], ["#FFE3CC", "#7A3300"], ["#E3F5D8", "#1F4A0E"], ["#F3E3FF", "#4B1675"], ["#E0F2F1", "#0D4A45"]];
const tint = (s) => TINTS[[...(s || "")].reduce((a, ch) => a + ch.charCodeAt(0), 0) % TINTS.length];
const COMMIT = { full_time: "Full-time", part_time: "Part-time", exploring: "Exploring" };
const FILTERS = [["best", "Best matches"], ["space", "Same problem space"], ["tech", "Technical"], ["near", "Nearby"], ["full", "Full-time now"]];

function Cofounders({ me }) {
  const s = useStore();
  const [editing, setEditing] = useState(false);
  const [filter, setFilter] = useState("best");
  const [err, setErr] = useState("");
  useEffect(() => { loadCofounders().catch(() => setErr("Couldn't load cofounders.")); }, []);
  const data = s.cofounders;
  if (!data) return html`<div class="page"><h1 class="title">Cofounders</h1><p class=${err ? "error" : "center"}>${err || "Loading…"}</p></div>`;
  const mine = data.me;
  const list = data.matches.filter((m) => filter === "best" || (filter === "space" && m.shared_spaces.length)
    || (filter === "tech" && m.technical) || (filter === "near" && m.nearby) || (filter === "full" && m.commitment === "full_time"));
  const ask = async (m, on) => {
    try {
      if (on) await api("/cofounders/intro", { method: "POST", body: { user_id: store.userId, to: m.user_id } });
      else await api(`/cofounders/intro?${qs({ user_id: store.userId, to: m.user_id })}`, { method: "DELETE" });
      await loadCofounders();
    } catch { say("Couldn't send that. Try again."); }
  };
  return html`<div class="page">
    <div class="head"><h1 class="title">Cofounders</h1>
      ${mine && html`<button type="button" class="btn" onClick=${() => setEditing(!editing)} aria-expanded=${editing}>Edit my profile</button>`}</div>
    ${editing || !mine ? html`<${ProfileForm} mine=${mine} me=${me} onDone=${() => setEditing(false)} first=${!mine} />` : html`<section class="card dark strip" aria-label="My profile">
      <div><p class="k">Skills</p><div class="chips">${mine.skills.map((x) => html`<span class="tag">${x}</span>`)}</div></div>
      <div><p class="k">Looking for</p><div class="chips">${mine.looking_for.map((x) => html`<span class="tag accent">${x}</span>`)}</div></div>
      <div><p class="k">Project</p><p>${[mine.project, COMMIT[mine.commitment] && low(COMMIT[mine.commitment])].filter(Boolean).join(" · ") || "Not set"}</p></div>
    </section>`}
    ${mine && html`<div class="chips" role="group" aria-label="Filter matches">${FILTERS.map(([k, l]) => html`<button type="button" class="chip"
      aria-pressed=${filter === k} onClick=${() => setFilter(k)}>${l}</button>`)}</div>`}
    ${mine && !list.length && html`<div class="card"><p class="muted">${data.matches.length ? "No matches with this filter." : "No other builders have a profile yet."}</p></div>`}
    ${mine && list.length > 0 && html`<div class="cofo stagger">${list.map((m) => {
      const [bg, fg] = tint(m.name);
      const status = m.email ? html`<p class="meta" style="color:var(--info-fg)">You both asked. Write to <a href=${"mailto:" + m.email}>${m.email}</a></p>`
        : m.asked_me ? html`<p class="meta" style="color:var(--info-fg)">Asked you for an intro</p>`
        : m.asked ? html`<p class="meta">Intro requested. Their email shows here if they agree.</p>`
        : m.shared_spaces.length ? html`<p class="meta">Also exploring: ${m.shared_spaces.join(", ")}</p>` : null;
      return html`<article class="card tight">
        <div class="row" style="flex-wrap:nowrap;align-items:flex-start">
          <span class="big-avatar" style=${`background:${bg};color:${fg}`} aria-hidden="true">${initials(m.name)}</span>
          <div style="flex:1;min-width:0"><h3 class="ctitle" style="font-size:17px">${m.name}</h3>
            <p class="meta">${[m.role, m.location].filter(Boolean).join(" · ")}</p></div>
          <span class=${"badge" + (m.match >= 80 ? " hi" : "")}>${m.match}%</span></div>
        ${m.bio && html`<p>${m.bio}</p>`}
        <div class="chips">${[...m.skills.slice(0, 3), COMMIT[m.commitment]].filter(Boolean).map((x) => html`<span class="tag soft">${x}</span>`)}</div>
        ${m.project && html`<p class="meta">Project: ${m.project}</p>`}
        ${status}
        <div class="row" style="flex-wrap:nowrap">
          ${m.email ? html`<a class="btn primary wide" href=${"mailto:" + m.email}>Message</a>`
            : m.asked ? html`<button type="button" class="btn wide" onClick=${() => ask(m, false)}>Cancel request</button>`
            : html`<button type="button" class="btn primary wide" onClick=${() => ask(m, true)}>${m.asked_me ? "Accept intro" : "Request intro"}</button>`}
        </div>
      </article>`;
    })}</div>`}
  </div>`;
}

function ProfileForm({ mine, me, onDone, first }) {
  const [f, setF] = useState(() => ({
    name: mine?.name || displayName(me), role: mine?.role || "", location: mine?.location || "", bio: mine?.bio || "",
    skills: (mine?.skills || []).join(", "), looking_for: (mine?.looking_for || []).join(", "), project: mine?.project || "",
    commitment: mine?.commitment || "", visible: mine ? mine.visible : true,
  }));
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const list = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    try {
      await api("/cofounders/me", { method: "PUT", body: { user_id: store.userId, ...f, skills: list(f.skills).slice(0, 15),
        looking_for: list(f.looking_for).slice(0, 10), commitment: f.commitment || null, role: f.role || null,
        location: f.location || null, bio: f.bio || null, project: f.project || null } });
      await loadCofounders();
      onDone();
    } catch { say("Couldn't save your profile."); }
    setBusy(false);
  };
  const projects = (store.items || []).map((w) => shortTitle(w.problem));
  return html`<form class="card form" onSubmit=${submit} style="max-width:820px">
    ${first && html`<h2 class="sec">Your profile</h2>`}
    <div class="form2"><div><label class="lbl" for="pf-n">Name</label><input id="pf-n" class="input" required maxlength="80" value=${f.name} onInput=${set("name")} /></div>
      <div><label class="lbl" for="pf-r">Role</label><input id="pf-r" class="input" maxlength="120" placeholder="e.g. Full-stack engineer" value=${f.role} onInput=${set("role")} /></div></div>
    <div class="form2"><div><label class="lbl" for="pf-l">Location</label><input id="pf-l" class="input" maxlength="120" placeholder="City, or Remote" value=${f.location} onInput=${set("location")} /></div>
      <div><label class="lbl" for="pf-c">Commitment</label><select id="pf-c" class="input" value=${f.commitment} onChange=${set("commitment")}>
        <option value="">Not set</option>${Object.entries(COMMIT).map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select></div></div>
    <div><label class="lbl" for="pf-s">Skills <span>(comma-separated)</span></label><input id="pf-s" class="input" value=${f.skills} onInput=${set("skills")} /></div>
    <div><label class="lbl" for="pf-f">Looking for <span>(comma-separated)</span></label><input id="pf-f" class="input" value=${f.looking_for} onInput=${set("looking_for")} /></div>
    <div><label class="lbl" for="pf-p">Project</label><input id="pf-p" class="input" maxlength="200" list="pf-projects" value=${f.project} onInput=${set("project")} />
      <datalist id="pf-projects">${projects.map((p) => html`<option value=${p}></option>`)}</datalist></div>
    <div><label class="lbl" for="pf-b">Bio <span>(one line)</span></label><input id="pf-b" class="input" maxlength="400" value=${f.bio} onInput=${set("bio")} /></div>
    <label class="row" style="gap:8px;min-height:44px"><input type="checkbox" class="check" checked=${f.visible} onChange=${set("visible")} />
      Show my profile to other builders</label>
    <div class="row"><button class="btn primary" disabled=${busy}>Save</button>${!first && html`<button type="button" class="btn" onClick=${onDone}>Cancel</button>`}</div>
  </form>`;
}

// --- Sign-in, terms, quiz -----------------------------------------------------------------------

function SoloHead({ right }) {
  return html`<header class="solo-head"><span class="logo"><${Logo} /><span>Signal</span></span>
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
      <p class="intro">Signal reads forums, job boards, tenders, reviews and new rules, and shows you problems people
        would pay to have solved. Then it helps you reach your first customers.</p></div>
    ${state === "sent" ? html`<div class="card"><h2 class="ctitle">Check your inbox</h2>
        <p>We sent a sign-in link to <b>${email}</b>. It works once, for 15 minutes.</p>
        <button class="textbtn" onClick=${() => setState("idle")}>Use a different email</button></div>`
      : html`<form onSubmit=${submit} style="display:flex;flex-direction:column;gap:12px">
        <label for="email" class="sr">Email</label>
        <div class="field">
          <input id="email" class="input" type="email" required autocomplete="email" placeholder="you@example.com" value=${email}
                 onInput=${(e) => setEmail(e.target.value)} />
          <button class="pill dark" disabled=${state === "sending"}>${state === "sending" ? "Sending…" : "Email me a link"}</button>
        </div>
        ${state === "error" && html`<p class="error">${error}</p>`}
        <p class="note">No password. We use your email only to send sign-in links.
          See the <a href="/terms" target="_blank" rel="noopener">terms</a>.</p>
      </form>`}
  </main>`;
}

// Brave's Search API terms (4(c)) need every user bound by Signal's terms, so nothing else shows until they agree.
function TermsStep({ me, onDone }) {
  const [state, setState] = useState("idle");
  const agree = async () => {
    setState("saving");
    try {
      await api("/auth/terms", { method: "POST", body: { version: me.terms_version } });
      onDone();
    } catch (err) {
      if (err.status === 409) location.reload();
      setState("error");
    }
  };
  return html`<${SoloHead} /><main class="solo signin" style="padding-top:12vh">
    <div><h1>Before you start</h1>
      <p class="intro">Signal shows summaries of public posts and some live search results. They are for your own
        use: you can't store, republish or resell them, or use them to train or test AI models.</p></div>
    <p>Read the <a href="/terms" target="_blank" rel="noopener" style="text-decoration:underline;text-underline-offset:3px">Terms of Service</a>${" "}
      (version ${me.terms_version}). Selecting "I agree" means you accept them.</p>
    <div><button class="pill dark" disabled=${state === "saving"} onClick=${agree}>${state === "saving" ? "Saving…" : "I agree"}</button></div>
    ${state === "error" && html`<p class="error">Couldn't save that. Try again.</p>`}
  </main>`;
}

function toggle(list, v) { return list.includes(v) ? list.filter((x) => x !== v) : [...list, v]; }

const Tick = ({ on }) => html`<span class="tick" aria-hidden="true">${on && html`<svg width="11" height="11" viewBox="0 0 12 12"
  fill="none" stroke="#121417" stroke-width="2.2"><path d="M2.5 6.2l2.3 2.3 4.7-4.8"/></svg>`}</span>`;

function Quiz({ onDone }) {
  const [opts, setOpts] = useState(null);
  const [industries, setIndustries] = useState([]);
  const [skills, setSkills] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { api("/onboarding/options").then(setOpts); }, []);
  if (!opts) return html`<${SoloHead} /><div class="center">Loading…</div>`;
  const submit = async () => {
    setBusy(true); setError("");
    try {
      const r = await api("/onboarding", { method: "POST", body: { industries: industries.slice(0, 20), skills: skills.slice(0, 20), works_in: [] } });
      onDone(r.user_id);
    } catch (e) { setError(e.message); setBusy(false); }
  };
  const n = industries.length;
  return html`<${SoloHead} right="Getting started" /><main class="solo">
    <div><h1>Which industries do you know?</h1>
      <p class="intro">Pick the ones you understand, or want to.</p></div>
    <section aria-label="Industries"><div class="tiles">${opts.industries.map((i) => {
      const on = industries.includes(i.name);
      return html`<button class="tile" type="button" aria-pressed=${on} onClick=${() => setIndustries(toggle(industries, i.name))}>
        <b>${i.name}</b><span class="sub">${i.branches.length ? plural(i.branches.length, "area") : "Whole industry"}</span>
        <${Tick} on=${on} /></button>`;
    })}</div></section>
    <div><div class="label">What can you build with?</div><p class="hint">Optional.</p>
      <div class="chips">${opts.skills.map((s) => html`<button class="chip" type="button" title=${s.covers}
        aria-pressed=${skills.includes(s.name)} onClick=${() => setSkills(toggle(skills, s.name))}>${s.name}</button>`)}</div></div>
    ${error && html`<p class="error">${error}</p>`}
    <div class="go-row"><span class="num" aria-live="polite" style=${n ? "" : "color:var(--muted)"}>
      ${n ? `${n} ${n === 1 ? "industry" : "industries"} picked` : "Pick at least one industry"}</span>
      <button class="pill dark" type="button" disabled=${!n || busy} onClick=${submit}>${busy ? "Saving…" : "Continue"}</button></div>
  </main>`;
}

// --- App ----------------------------------------------------------------------------------------

function Shell({ me }) {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const on = () => setHash(location.hash);
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  useEffect(() => { loadWorkspaces(); }, []);
  const route = useMemo(() => parseRoute(hash), [hash]);
  const key = route.name === "workspace" ? "workspace" : `${route.name}/${route.id || ""}`;
  const [shown, leaving] = useLeaving(key, route);
  // Within the workspace (tab or problem switch) the page stays; Workspace animates its own content.
  const live = shown.name === "workspace" && route.name === "workspace" ? route : shown;
  const shownKey = live.name === "workspace" ? "ws" : `${live.name}/${live.id || ""}`;
  const active = { problem: "problems" }[live.name] || live.name;
  let page;
  if (live.name === "problems") page = html`<${Problems} />`;
  else if (live.name === "problem") page = html`<${ProblemPage} id=${live.id} key=${live.id} />`;
  else if (live.name === "workspace") page = html`<${Workspace} id=${live.id} tab=${live.tab} me=${me} />`;
  else if (live.name === "cofounders") page = html`<${Cofounders} me=${me} />`;
  else page = html`<${Home} me=${me} />`;
  return html`<div class="wrap"><${Sidebar} me=${me} active=${active} />
    <main class=${"main" + (leaving ? " leaving" : "")} key=${shownKey}>${page}</main></div><${Toast} />`;
}

function App() {
  const [me, setMe] = useState(undefined);       // undefined: loading; null: signed out
  useEffect(() => {
    api("/auth/me").then(setMe).catch((err) => setMe(err.status === 403 ? "suspended" : null));
  }, []);
  if (me === undefined) return html`<div class="center">Loading…</div>`;
  if (me === null) return html`<${SignIn} />`;
  if (me === "suspended") return html`<${SoloHead} /><main class="solo signin" style="padding-top:12vh">
    <h1>Account suspended</h1><p class="intro">This account can't use Signal right now. Write to us if you think
    this is a mistake.</p></main>`;
  if (!me.terms_ok) return html`<${TermsStep} me=${me} onDone=${() => setMe({ ...me, terms_ok: true })} />`;
  if (!me.user_id) return html`<${Quiz} onDone=${(id) => setMe({ ...me, user_id: id })} />`;
  store.userId = me.user_id;
  return html`<${Shell} me=${me} />`;
}

render(html`<${App} />`, document.getElementById("app"));
