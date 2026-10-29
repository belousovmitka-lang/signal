// Signal web app (beta): sign-in, quiz, feed, saved, clips, areas. Preact + htm, no build step.
// The feed never says why an item is shown, and the areas list never says how an area got there.
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

const TYPE_LABEL = { pain: "Problem", paid_demand: "Paid demand", tool_gap: "Tool gap", change: "Change",
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

function Evidence({ items }) {
  return html`<ul class="evidence">${items.map((e) => html`<li>
    ${e.embed ? html`<${XEmbed} url=${e.embed} />` : html`
      <div>${e.what_it_shows}</div>
      <div class="src">${safeUrl(e.url) ? html`<a href=${safeUrl(e.url)} target="_blank" rel="noopener noreferrer">
        ${sourceName(e.source)}</a>` : sourceName(e.source)}</div>`}
  </li>`)}</ul>`;
}

// --- Sign-in ------------------------------------------------------------------------------------

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
  return html`<main class="wrap"><div class="hero">
    <div class="brand" style="margin-bottom:28px"><i class="dot"></i> Signal</div>
    <h1>Real problems worth building.</h1>
    <p>A feed of real problems, paid demand and tool gaps, matched to what you know.</p>
    ${state === "sent" ? html`<div class="card">
        <h3>Check your inbox</h3>
        <p class="summary">We sent a sign-in link to <b>${email}</b>. It works once, for 15 minutes.</p>
        <button class="btn quiet" onClick=${() => setState("idle")}>Use a different email</button></div>`
      : html`<form onSubmit=${submit}>
        <div class="field">
          <input type="email" required autocomplete="email" placeholder="you@example.com" value=${email}
                 onInput=${(e) => setEmail(e.target.value)} aria-label="Email" />
          <button class="btn primary big" disabled=${state === "sending"}>
            ${state === "sending" ? "Sending…" : "Email me a link"}</button>
        </div>
        ${state === "error" && html`<p class="error">${error}</p>`}
        <p class="note">No password. We use your email only to send sign-in links.</p>
      </form>`}
  </div></main>`;
}

// --- Quiz ---------------------------------------------------------------------------------------

function toggle(list, v) { return list.includes(v) ? list.filter((x) => x !== v) : [...list, v]; }

function Quiz({ onDone }) {
  const [opts, setOpts] = useState(null);
  const [industries, setIndustries] = useState([]);   // "Legal" or "Legal > Litigation support"
  const [skills, setSkills] = useState([]);
  const [worksIn, setWorksIn] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { api("/onboarding/options").then(setOpts); }, []);
  if (!opts) return html`<div class="center">Loading…</div>`;

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

  return html`<main class="wrap">
    <div class="step" style="margin-top:40px">
      <h2>Which industries interest you?</h2>
      <p>Pick a few. Narrow one down if you like; your feed keeps learning as you use it.</p>
      <div class="chips">${opts.industries.map((i) => html`
        <button class="chip" aria-pressed=${chosenRoots.includes(i.name)} onClick=${() => pickRoot(i.name)}>
          ${i.name}</button>`)}</div>
      ${chosenRoots.map((root) => {
        const branches = opts.industries.find((i) => i.name === root)?.branches || [];
        return branches.length ? html`<div class="branches"><div class="note">${root}: any part in particular?</div>
          <div class="chips" style="margin-top:6px">${branches.map((b) => html`
            <button class="chip small" aria-pressed=${industries.includes(`${root} > ${b}`)}
                    onClick=${() => pickBranch(root, b)}>${b}</button>`)}</div></div>` : null;
      })}
    </div>
    <div class="step">
      <h2>What can you build with?</h2>
      <p>Optional. Skills you'd use to solve a problem.</p>
      <div class="chips">${opts.skills.map((s) => html`
        <button class="chip small" title=${s.covers} aria-pressed=${skills.includes(s.name)}
                onClick=${() => setSkills(toggle(skills, s.name))}>${s.name}</button>`)}</div>
    </div>
    <div class="step">
      <h2>Do you work in any of these?</h2>
      <p>Optional. Now and then we'll ask whether a problem is real where you work.</p>
      <div class="chips">${opts.industries.map((i) => html`
        <button class="chip small" aria-pressed=${worksIn.includes(i.name)}
                onClick=${() => setWorksIn(toggle(worksIn, i.name))}>${i.name}</button>`)}</div>
    </div>
    ${error && html`<p class="error">${error}</p>`}
    <button class="btn primary big" disabled=${!industries.length || busy} onClick=${submit}>
      ${busy ? "Building your feed…" : "Show my feed"}</button>
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

function Card({ item, onOpenInferred }) {
  const ref = useRef();
  useDwell(item, ref);
  const [saved, setSaved] = useState(false);
  const [menu, setMenu] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [answered, setAnswered] = useState(null);      // calibration answer
  const [engaged, setEngaged] = useState(false);       // opened: time to ask "Was this useful?"
  const [useful, setUseful] = useState(null);
  const inferred = item.kind === "inferred";
  const url = safeUrl(item.url);

  if (hidden) return html`<div class="card hidden-card">Hidden.</div>`;

  const open = () => { track("open_source", item); setEngaged(true); };
  const notForMe = (reason) => { track("not_for_me", item, reason ? { reason } : {}); setHidden(true); };
  const m = money(item.money);

  return html`<article class="card" ref=${ref}>
    <div class="meta">
      ${inferred ? html`<span class="label inferred">Inferred${item.confidence ? ` · ${item.confidence} confidence` : ""}</span>`
        : html`<span class="label">${TYPE_LABEL[item.post_type] || "Post"}</span>`}
      ${m && html`<span class="label money">${m}</span>`}
      <span class="path">${item.niche_path.join(" › ")}</span>
    </div>
    <h3>${url ? html`<a href=${url} target="_blank" rel="noopener noreferrer" onClick=${open}>${item.title || item.summary}</a>`
      : item.title || item.summary}</h3>
    ${item.summary && item.title && item.summary.trim() !== item.title.trim() && html`<p class="summary">${item.summary}</p>`}
    ${!inferred && html`<div class="meta">${sourceName(item.source)} · ${ago(item.published_at)}
      ${item.similar_posts > 0 && html` · ${item.similar_posts} similar posts`}</div>`}
    ${inferred && item.evidence.length > 0 && html`
      <${Evidence} items=${item.evidence} />
      <button class="more-link" onClick=${() => { track("expand", item); setEngaged(true); onOpenInferred(item); }}>
        ${item.evidence_more > 0 ? `See all evidence (+${item.evidence_more} more)` : "See the evidence"}</button>`}

    ${item.calibration ? html`<div class="actions">
        ${answered ? html`<span class="note" style="padding:7px 8px">Thanks.</span>` : html`
          <button class="btn" onClick=${() => { track("interested", item); setAnswered("yes"); }}>Interested</button>
          <button class="btn quiet" onClick=${() => { track("not_for_me", item); setAnswered("no"); }}>Not for me</button>`}
      </div>`
    : html`<div class="actions">
        ${url && html`<a class="btn quiet" href=${url} target="_blank" rel="noopener noreferrer" onClick=${open}>Open</a>`}
        <button class=${"btn quiet" + (saved ? " on" : "")} disabled=${saved}
                onClick=${() => { track("save", item); setSaved(true); setEngaged(true); }}>${saved ? "Saved" : "Save"}</button>
        <span class="spacer"></span>
        <button class="btn quiet" onClick=${() => setMenu(!menu)} aria-expanded=${menu}>Not for me</button>
      </div>
      ${menu && html`<div class="menu">
        ${NOT_FOR_ME.map(([r, label]) => html`<button class="chip small" onClick=${() => notForMe(r)}>${label}</button>`)}
        ${inferred && html`<button class="chip small" onClick=${() => notForMe("not_true")}>Not true in my experience</button>`}
        <button class="chip small" onClick=${() => notForMe(null)}>Just not for me</button>
        <button class="chip small" onClick=${() => { track("hide_source", item); setHidden(true); }}>
          Hide ${sourceName(item.source)}</button>
      </div>`}`}

    ${item.ask_useful && engaged && html`<div class="ask">
      ${useful ? html`<span>Thanks, that helps.</span>` : html`
        <span>Was this useful?</span>
        <button class="btn" onClick=${() => { track("useful_yes", item); setUseful("yes"); }}>Yes</button>
        <button class="btn" onClick=${() => { track("useful_no", item); setUseful("no"); }}>No</button>`}
    </div>`}
  </article>`;
}

function InferredDetail({ item, userId, onClose }) {
  const [data, setData] = useState(null);
  useEffect(() => {
    api(`/inferred/${item.item_id}?${qs({ user_id: userId })}`).then(setData).catch(() => setData({ error: true }));
    const esc = (e) => e.key === "Escape" && onClose();
    addEventListener("keydown", esc);
    return () => removeEventListener("keydown", esc);
  }, [item.item_id]);
  return html`<div class="modal-back" onClick=${(e) => e.target === e.currentTarget && onClose()}>
    <div class="modal" role="dialog" aria-modal="true">
      <div class="meta"><span class="label inferred">Inferred${data?.confidence ? ` · ${data.confidence} confidence` : ""}</span>
        <span class="spacer" style="flex:1"></span><button class="btn quiet" onClick=${onClose}>Close</button></div>
      <h2>${item.title || item.summary}</h2>
      ${!data ? html`<div class="center">Loading…</div>` : data.error ? html`<p class="note">This problem is no longer available.</p>`
        : html`
          ${data.who && html`<p class="note">Who: ${Array.isArray(data.who) ? data.who.join(", ") : data.who}</p>`}
          <div class="section-title">Evidence</div><${Evidence} items=${data.evidence} />
          ${data.counter_evidence?.length > 0 && html`<div class="section-title">What could argue against it</div>
            <${Evidence} items=${data.counter_evidence} />`}`}
    </div></div>`;
}

function InsiderPrompt({ userId }) {
  const [p, setP] = useState(null);
  const [note, setNote] = useState("");
  const [done, setDone] = useState(false);
  useEffect(() => { api(`/inferred/prompt?${qs({ user_id: userId })}`).then((r) => setP(r.prompt)).catch(() => {}); }, []);
  if (!p) return null;
  if (done) return html`<div class="card hidden-card">Thanks. Insiders like you keep these honest.</div>`;
  const vote = async (v) => {
    await api(`/inferred/${p.inferred_id}/vote`, { method: "POST",
      body: { user_id: userId, vote: v, note: note.trim() || null } }).catch(() => {});
    setDone(true);
  };
  const label = { yes: "Yes", partly: "Partly", no: "No" };
  return html`<div class="card">
    <div class="meta"><span class="label inferred">${p.question}</span></div>
    <h3>${p.hypothesis}</h3>
    <${Evidence} items=${p.evidence} />
    <textarea placeholder="Anything to add? (optional)" maxlength="300" value=${note}
              onInput=${(e) => setNote(e.target.value)} style="margin-top:10px"></textarea>
    <div class="actions">${p.answers.map((a) => html`<button class="btn" onClick=${() => vote(a)}>${label[a] || a}</button>`)}</div>
  </div>`;
}

function Feed({ userId }) {
  const [items, setItems] = useState([]);
  const [page, setPage] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState(null);
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

  return html`<main class="wrap">
    <${InsiderPrompt} userId=${userId} />
    ${page?.calibration && html`<p class="note" style="margin-top:20px">To start, tell us which of these interest you.</p>`}
    ${items.map((it) => html`<${Card} key=${it.impression_id} item=${it} onOpenInferred=${setDetail} />`)}
    ${error && html`<div class="center"><p class="error">${error}</p><button class="btn" onClick=${() => load()}>Retry</button></div>`}
    ${loading && html`<div class="center">Loading…</div>`}
    ${!loading && page && (page.calibration ? html`<div class="end">
        <button class="btn primary big" onClick=${() => load()}>Continue to my feed</button></div>`
      : page.caught_up || !page.items.length ? html`<div class="end">
        <h2>You're caught up</h2><p>That's the strongest for you right now.</p>
        <button class="btn" onClick=${() => load(true)}>Explore more</button></div>` : null)}
    <div ref=${sentinel}></div>
    ${detail && html`<${InferredDetail} item=${detail} userId=${userId} onClose=${() => setDetail(null)} />`}
  </main>`;
}

// --- Saved, clips, areas ------------------------------------------------------------------------

function Saved({ userId }) {
  const [items, setItems] = useState(null);
  useEffect(() => { api(`/saved?${qs({ user_id: userId })}`).then((r) => setItems(r.items)); }, []);
  if (!items) return html`<div class="center">Loading…</div>`;
  if (!items.length) return html`<main class="wrap"><div class="center">Nothing saved yet. Save posts from your feed to keep them here.</div></main>`;
  return html`<main class="wrap"><div class="section-title">Saved</div><div class="list">
    ${items.map((it) => html`<div class="row"><div class="grow">
      ${safeUrl(it.url) ? html`<a href=${safeUrl(it.url)} target="_blank" rel="noopener noreferrer">${it.title || it.summary}</a>`
        : html`<span>${it.title || it.summary}</span>`}
      <div class="sub">${it.kind === "inferred" ? "Inferred · " : ""}${it.niche_path.join(" › ")} · saved ${ago(it.saved_at)}</div>
    </div></div>`)}</div></main>`;
}

function Clips({ userId }) {
  const [clips, setClips] = useState(null);
  const [error, setError] = useState(false);
  const load = () => api(`/clips?${qs({ user_id: userId })}`).then(setClips).catch(() => setError(true));
  useEffect(() => { load(); }, []);
  if (error) return html`<main class="wrap"><div class="center">Clips aren't available on this server.</div></main>`;
  if (!clips) return html`<div class="center">Loading…</div>`;
  const remove = async (id) => {
    await api(`/clips/${encodeURIComponent(id)}?${qs({ user_id: userId })}`, { method: "DELETE" }).catch(() => {});
    load();
  };
  return html`<main class="wrap"><div class="section-title">Your clips</div>
    ${!clips.length ? html`<div class="center">No clips yet. Use the Clip to Signal button on any page to save what matters to you.</div>`
      : html`<div class="list">${clips.map((c) => html`<div class="row"><div class="grow">
        <a href=${safeUrl(c.url)} target="_blank" rel="noopener noreferrer">${c.title || c.url}</a>
        ${c.text && html`<blockquote class="clip">${c.text.slice(0, 400)}</blockquote>`}
        ${c.note && html`<div class="sub">Note: ${c.note}</div>`}
        <div class="sub">${c.site} · ${ago(c.created_at)}</div></div>
        <button class="btn quiet" onClick=${() => remove(c.id)}>Delete</button></div>`)}</div>`}
  </main>`;
}

function Areas({ userId }) {
  const [map, setMap] = useState(null);
  const [confirmReset, setConfirmReset] = useState(false);
  const load = () => api(`/map?${qs({ user_id: userId })}`).then(setMap);
  useEffect(() => { load(); }, []);
  if (!map) return html`<div class="center">Loading…</div>`;
  const act = async (path, body) => { await api(path, { method: "POST", body: { user_id: userId, ...body } }).catch(() => {}); load(); };
  const groups = [["core", "Closest to you"], ["regular", "Also yours"], ["exploring", "Exploring"]];
  const nodes = [...map.nodes].sort((a, b) => b.affinity - a.affinity);
  return html`<main class="wrap">
    ${groups.map(([ring, title]) => {
      const here = nodes.filter((n) => n.ring === ring);
      return here.length ? html`<div class="section-title">${title}</div><div class="list">
        ${here.map((n) => html`<div class=${"row" + (n.status === "context" ? " context" : "")}><div class="grow">
          <div>${n.label}</div><div class="sub">${n.path.slice(0, -1).join(" › ")}</div></div>
          <button class="btn quiet" title="Show more" onClick=${() => act("/map/nudge", { node_id: n.node_id, direction: "more" })}>More</button>
          <button class="btn quiet" title="Show less" onClick=${() => act("/map/nudge", { node_id: n.node_id, direction: "less" })}>Less</button>
          <button class="btn quiet" title="Remove" onClick=${() => act("/map/remove", { node_id: n.node_id })}>Remove</button>
        </div>`)}</div>` : null;
    })}
    ${!nodes.length && html`<div class="center">Your areas appear here as you use the feed.</div>`}
    <div class="end">${confirmReset ? html`<p class="note">This forgets what the feed has learned about you. Sure?</p>
        <button class="btn" onClick=${() => { setConfirmReset(false); act("/map/reset", {}); }}>Yes, start over</button>
        <button class="btn quiet" onClick=${() => setConfirmReset(false)}>Cancel</button>`
      : html`<button class="btn quiet" onClick=${() => setConfirmReset(true)}>Start over from my quiz answers</button>`}</div>
  </main>`;
}

// --- Shell --------------------------------------------------------------------------------------

const TABS = [["feed", "Feed"], ["saved", "Saved"], ["clips", "Clips"], ["areas", "Areas"]];

function App() {
  const [me, setMe] = useState(undefined);       // undefined: loading; null: signed out
  const [tab, setTab] = useState(() => location.hash.slice(1) || "feed");
  useEffect(() => { api("/auth/me").then(setMe).catch(() => setMe(null)); }, []);
  useEffect(() => {
    const on = () => setTab(location.hash.slice(1) || "feed");
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  events.userId = me?.user_id || null;

  if (me === undefined) return html`<div class="center">Loading…</div>`;
  if (me === null) return html`<${SignIn} />`;
  if (!me.user_id) return html`<${Quiz} onDone=${(uid) => setMe({ ...me, user_id: uid })} />`;

  const signOut = async () => { flush(); await api("/auth/sign-out", { method: "POST" }).catch(() => {}); location.hash = ""; location.reload(); };
  const view = { feed: Feed, saved: Saved, clips: Clips, areas: Areas }[tab] || Feed;
  return html`
    <header class="top"><div class="wrap">
      <div class="brand"><i class="dot"></i><span>Signal</span></div>
      <nav class="tabs">${TABS.map(([k, label]) => html`<button class="tab" aria-current=${tab === k ? "page" : undefined}
        onClick=${() => { location.hash = k; }}>${label}</button>`)}
        <button class="tab" onClick=${signOut} title=${me.email}>Sign out</button></nav>
    </div></header>
    <${view} key=${tab} userId=${me.user_id} />`;
}

render(html`<${App} />`, document.getElementById("app"));
