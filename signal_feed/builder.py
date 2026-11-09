"""The builder app (design handoff 2026-11-08, web/app.js): Problems, saved problems with a Workspace each,
and cofounder matching.

A **problem** is a job cluster (signal_feed/opportunities/clusters.py) seen from the builder's side. Its id
is the cluster's best core post, because cluster ids change on every rebuild. Every number on a problem is
computed here from the posts' own tags; nothing is made up to fill the design:

- score: the best core post's project score (0-100);
- pain: mean `intensity` of the members, out of 10;
- frequency: the share of members whose need recurs, with the stated time cost when a post gives one;
- will pay: mean `buyer_likelihood`, out of 10, with the stated USD amounts when there are any;
- competition: how many distinct existing tools the posts name (Low 0-1, Medium 2-4, High 5+);
- open questions: what the evidence doesn't show yet (no stated price, a single buyer, ...).

Evidence lines are Signal's own summaries of each post with a link to the original (CLAUDE.md rule 5:
link back, don't republish); X posts keep their summary only, never X's wording.

A **workspace** holds what the user does with a saved problem; its `data` is the client's document. A
**cofounder profile** is opt-in and shown to other signed-in builders; a match's email is revealed only
when both have asked for an intro.
"""

from __future__ import annotations

import re
import statistics
import threading
from collections import Counter
from collections.abc import Callable
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from signal_connectors.models import utcnow

from .api import Me
from .opportunities.assess import COMPETITION_POINTS, WEIGHTS, evidence_points
from .opportunities.assess import load as load_assessments
from .db import (AccountRow, CofounderProfileRow, EnrichedPostRow, FeedStore, IntroRequestRow, JobClusterPostRow,
                 JobClusterRow, ProblemAssessmentRow, WorkspaceRow)
from .posts import PostsReader, _chunks

MAX_DATA_BYTES = 256_000

BUYER_LABEL = {"developer": "Developers", "small_business": "Small businesses", "mid_market": "Mid-size companies",
               "enterprise": "Large companies", "consumer": "Consumers", "public_sector": "Public sector",
               "solo": "Solo professionals"}

EVIDENCE_GROUP = {"community": "Forums", "social": "Social", "review": "Reviews", "paid_demand": "Paid demand",
                  "news": "News", "media": "News", "government": "Filings", "research": "Research",
                  "startup": "Launches"}


def _clean_need(text: str | None) -> str:
    """ "US freight brokers need to vet carriers" -> "Freight brokers need to vet carriers"."""
    t = re.sub(r"^(US|U\.S\.|American)\s+", "", (text or "").strip())
    return t[:1].upper() + t[1:]


def _cap(text: str | None) -> str | None:
    return text[:1].upper() + text[1:] if text else text


def _plural(n: int, word: str) -> str:
    return f"From {n} {word}{'' if n == 1 else 's'}"


def _buyer(kind: str | None) -> str | None:
    return BUYER_LABEL.get(kind or "") or (_cap(kind.replace("_", " ")) if kind else None)


def _mean(vals: list[float]) -> float | None:
    return statistics.fmean(vals) if vals else None


def _per(time_cost: dict | None) -> str | None:
    if not time_cost or not isinstance(time_cost.get("hours"), (int, float)):
        return None
    h, per = time_cost["hours"], time_cost.get("per") or "occurrence"
    amount = f"{h:g} h" if h >= 1 else f"{round(h * 60)} min"
    return f"{amount} per {per}"


def _usd(m: dict | None) -> float | None:
    if not m or (m.get("currency") or "").upper() != "USD":
        return None
    vals = [v for v in (m.get("amount_min"), m.get("amount_max")) if isinstance(v, (int, float)) and v > 0]
    return sum(vals) / len(vals) if vals else None


RATING_KEYS = {"pain": "pain", "frequency": "frequency", "will_pay": "will_pay", "market": "market",
               "competition": "competition", "evidence": "evidence_rating"}


def composite_score(p: dict) -> int:
    """The weighted ratings (assess.WEIGHTS) over the ones known: the score is its tiles."""
    known = {k: w for k, w in WEIGHTS.items() if p[RATING_KEYS[k]]["value"] is not None}
    total = sum(known.values())
    return round(10 * sum(w * p[RATING_KEYS[k]]["value"] for k, w in known.items()) / total) if total else 0


FREQ_LABEL = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly", "quarterly": "Quarterly", "rarely": "Rarely"}


def with_assessment(p: dict, a: dict) -> dict:
    """The problem with an assessment's ratings, prospects and validation plan."""
    q = dict(p)
    q["pain"] = {"value": a["pain"]["score"], "reason": a["pain"]["reason"]}
    q["frequency"] = {"value": a["frequency"]["score"], "label": FREQ_LABEL[a["frequency_label"]],
                      "reason": a["frequency"]["reason"]}
    q["will_pay"] = {"value": a["will_pay"]["score"], "reason": a["will_pay"]["reason"]}
    q["market"] = {"value": a["market"]["score"], "reason": a["market"]["reason"]}
    q["competition"] = {"value": COMPETITION_POINTS[a["competition"]], "label": a["competition"].capitalize(),
                        "reason": a["competition_reason"]}
    q["score"], q["assessed"] = composite_score(q), True
    q["existing_tools"] = a.get("existing_tools", [])
    q["who_to_reach"], q["companies"] = a.get("who_to_reach", []), a.get("example_companies", [])
    q["validation"] = a.get("validation", [])
    return q


def _problem(cluster: JobClusterRow, members: list[tuple[JobClusterPostRow, EnrichedPostRow, Any]]) -> dict:
    """One problem from a cluster and its (membership, tags, raw post) triples."""
    cores = [(m, r, p) for m, r, p in members if m.role == "core"]
    best = max(cores or members, key=lambda t: (t[1].project_score or 0, t[1].opp_confidence or 0, t[1].post_id))
    rows = [r for _, r, _ in members]
    industries = Counter((r.industry_path or [None])[0] for _, r, _ in cores or members)
    industries.pop(None, None)
    industry = industries.most_common(1)[0][0] if industries else None
    niche = Counter(r.industry_path[1] for _, r, _ in cores or members if r.industry_path and len(r.industry_path) > 1)

    pain = _mean([r.intensity for r in rows if r.intensity is not None])
    recurring = [r.recurrence for r in rows if r.recurrence]
    share = sum(x == "recurring" for x in recurring) / len(recurring) if recurring else None
    time = next((t for r in sorted(rows, key=lambda r: -(r.project_score or 0)) if (t := _per(r.time_cost))), None)
    pay = _mean([r.buyer_likelihood for r in rows if r.buyer_likelihood is not None])
    amounts = sorted(v for r in rows if (v := _usd(r.money_signal)) is not None)
    tools = Counter(t.strip() for r in rows for t in (r.tools_mentioned or []) if t and t.strip())
    n_tools = len(tools)

    def score10(v: float | None) -> int | None:
        return None if v is None else max(0, min(10, round(v * 10)))

    frequency_label = None if share is None else ("Recurring" if share >= 0.67 else "Mixed" if share >= 0.34
                                                  else "Occasional")
    comp = "low" if n_tools <= 1 else "medium" if n_tools <= 4 else "high"
    n_posts = cluster.core_posts + cluster.supporting_posts
    # Until the problem is assessed (opportunities/assess.py), the ratings come from the posts' own tags.
    score = {
        "pain": {"value": score10(pain), "reason": _plural(sum(r.intensity is not None for r in rows), "post")
                 if pain is not None else None},
        "frequency": {"value": score10(share), "label": frequency_label,
                      "reason": time or (f"{sum(x == 'recurring' for x in recurring)} of {len(recurring)} posts recur"
                                         if recurring else None)},
        "will_pay": {"value": score10(pay),
                     "reason": (f"{len(amounts)} stated budget{'s' if len(amounts) != 1 else ''}, median "
                                f"${statistics.median(amounts):,.0f}") if amounts else "No price stated yet"},
        "market": {"value": None, "reason": "Not assessed yet"},
        "competition": {"value": COMPETITION_POINTS[comp], "label": comp.capitalize(),
                        "reason": ", ".join(t for t, _ in tools.most_common(3)) or "No tools named"},
        "evidence_rating": {"value": evidence_points(cluster.distinct_buyers),
                            "reason": f"{n_posts} post{'s' if n_posts != 1 else ''}, {cluster.distinct_buyers} "
                                      f"buyer{'s' if cluster.distinct_buyers != 1 else ''}"},
    }

    evidence = []
    for m, r, p in sorted(members, key=lambda t: (t[0].role != "core", -(t[1].project_score or 0))):
        evidence.append({
            "post_id": r.post_id, "role": m.role, "summary": r.plain_summary or r.pain_statement or p.title,
            "source": p.source, "group": EVIDENCE_GROUP.get(p.source_type, "Other"),
            "who": p.author_role or r.who_has_pain, "published_at": p.published_at.isoformat(), "url": p.url,
            "score": r.project_score,
            "tags": {"time cost": _per(r.time_cost), "workaround": r.current_workaround, "recurs": r.recurrence,
                     "tools named": ", ".join(r.tools_mentioned or []), "budget USD": _usd(r.money_signal)}})

    workarounds, seen = [], set()
    for _, r, p in sorted(members, key=lambda t: -(t[1].project_score or 0)):
        w = (r.current_workaround or "").strip()
        if w and w.lower() not in seen:
            seen.add(w.lower())
            workarounds.append({"what": _cap(w), "source": p.source})
    roles = Counter((p.author_role or r.who_has_pain or "").strip() for _, r, p in members)
    roles.pop("", None)

    questions = []
    if not amounts:
        questions.append("What would they pay? No post states a price.")
    if cluster.distinct_buyers <= 1:
        questions.append("Is it more than one buyer? All the evidence is from one.")
    if share is None or share < 0.34:
        questions.append("How often does it happen? The posts don't say it recurs.")
    if not workarounds and not tools:
        questions.append("What do they use today? No post names a workaround.")
    if (cluster.core_posts + cluster.supporting_posts) < 3:
        questions.append("Do others have it? There are fewer than three posts so far.")

    return {
        "id": best[1].post_id, "title": _clean_need(cluster.need), "statement": _cap(cluster.project_idea),
        "summary": best[1].plain_summary, "industry": industry,
        "niche": niche.most_common(1)[0][0] if niche else None,
        "segment": _cap(cluster.buyer_segment), "buyer": _buyer(cluster.buyer_type),
        "score": composite_score(score), "assessed": False, **score,
        "who_to_reach": [], "companies": [], "validation": [], "existing_tools": [],
        "posts": cluster.core_posts + cluster.supporting_posts, "buyers": cluster.distinct_buyers,
        "price": cluster.price, "sources": cluster.sources,
        "evidence": evidence, "workarounds": workarounds[:5], "tools": [t for t, _ in tools.most_common(8)],
        "audience": {"role": _cap(roles.most_common(1)[0][0]) if roles else None, "segment": _cap(cluster.buyer_segment),
                     "payer": _buyer(cluster.buyer_type),
                     "channels": [s for s, _ in Counter(p.source for _, _, p in members).most_common(3)]},
        "open_questions": questions,
        "first_seen": cluster.first_seen.isoformat(), "last_seen": cluster.last_seen.isoformat(),
    }


class ProblemBank:
    """Every problem, rebuilt when the job clusters are (`built_at` changes)."""

    def __init__(self, store: FeedStore, posts: PostsReader) -> None:
        self.store, self.posts = store, posts
        self._key: tuple | None = None
        self._problems: list[dict] = []
        self._by_post: dict[str, str] = {}       # any member post -> its problem id
        self._lock = threading.Lock()

    def _load(self) -> None:
        with self.store.session() as s:
            key = tuple(s.execute(select(func.max(JobClusterRow.built_at), func.count(JobClusterRow.id))).one()) + tuple(
                s.execute(select(func.max(ProblemAssessmentRow.created_at), func.count())
                          .select_from(ProblemAssessmentRow)).one())
            if key == self._key:
                return
            clusters = list(s.scalars(select(JobClusterRow)))
            links = list(s.scalars(select(JobClusterPostRow)))
            ids = [m.post_id for m in links]
            rows = {r.post_id: r for part in _chunks(ids)
                    for r in s.scalars(select(EnrichedPostRow).where(EnrichedPostRow.post_id.in_(part)))}
        raw = {p.id: p for part in _chunks(ids) for p in self.posts.get_posts(part)}
        by_cluster: dict[int, list] = {}
        for m in links:
            if m.post_id in rows and m.post_id in raw:
                by_cluster.setdefault(m.cluster_id, []).append((m, rows[m.post_id], raw[m.post_id]))
        problems, by_post = [], {}
        for c in clusters:
            members = by_cluster.get(c.id)
            if not members:
                continue
            p = _problem(c, members)
            problems.append(p)
            for _, r, _ in members:
                by_post[r.post_id] = p["id"]
        assessed = load_assessments(self.store, [p["id"] for p in problems])
        problems = [with_assessment(p, assessed[p["id"]].data) if p["id"] in assessed else p for p in problems]
        problems.sort(key=lambda p: (-p["score"], -p["posts"], p["id"]))
        self._problems, self._by_post, self._key = problems, by_post, key

    def all(self) -> list[dict]:
        with self._lock:
            self._load()
            return self._problems

    def get(self, problem_id: str) -> dict | None:
        """By id, or by any post that is now in that problem (an old id after a rebuild)."""
        problems = self.all()
        pid = self._by_post.get(problem_id, problem_id)
        return next((p for p in problems if p["id"] == pid), None)


def card(p: dict) -> dict:
    """The list's fields: no evidence beyond the two best lines."""
    keep = ("id", "title", "statement", "summary", "industry", "niche", "segment", "buyer", "score", "assessed",
            "pain", "frequency", "will_pay", "market", "competition", "evidence_rating", "posts", "buyers", "price",
            "sources", "tools", "existing_tools", "who_to_reach", "companies", "validation", "last_seen")
    return {k: p[k] for k in keep} | {"evidence": [public(e) for e in p["evidence"][:2]],
                                      "workarounds": p["workarounds"][:3]}


def public(e: dict) -> dict:
    """An evidence line without the tags that only the assessment prompt reads."""
    return {k: v for k, v in e.items() if k != "tags"}


def detail(p: dict) -> dict:
    return p | {"evidence": [public(e) for e in p["evidence"]]}


# --- API --------------------------------------------------------------------------------------------

class SaveProblem(BaseModel):
    user_id: str
    note: str | None = Field(default=None, max_length=2000)


class OwnProblem(BaseModel):
    user_id: str
    title: str = Field(min_length=3, max_length=200)
    who: str | None = Field(default=None, max_length=200)
    industry: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)


PANEL_DAILY_LIMIT = 10


class PanelIn(BaseModel):
    user_id: str
    pitch: str | None = Field(default=None, max_length=600)
    price: str | None = Field(default=None, max_length=60)


class WorkspaceData(BaseModel):
    user_id: str
    data: dict[str, Any]


class ProfileIn(BaseModel):
    user_id: str
    name: str = Field(min_length=1, max_length=80)
    role: str | None = Field(default=None, max_length=120)
    location: str | None = Field(default=None, max_length=120)
    bio: str | None = Field(default=None, max_length=400)
    skills: list[str] = Field(default_factory=list, max_length=15)
    looking_for: list[str] = Field(default_factory=list, max_length=10)
    project: str | None = Field(default=None, max_length=200)
    commitment: Literal["full_time", "part_time", "exploring"] | None = None
    visible: bool = True


class IntroIn(BaseModel):
    user_id: str
    to: str = Field(min_length=1, max_length=64)


TECH = re.compile(r"engineer|develop|full.?stack|front.?end|back.?end|ml\b|machine learning|data|ai\b|hardware|"
                  r"python|typescript|javascript|react|ios|android|devops|cloud|security", re.I)


def _words(items: list[str]) -> set[str]:
    return {w for i in items for w in re.findall(r"[a-z0-9+#/]{2,}", i.lower())} - {"and", "the", "of", "for"}


def _overlap(want: list[str], have: list[str]) -> float:
    """How much of `want` the other side has: whole items, else shared words."""
    if not want:
        return 0.0
    hw, hits = _words(have), 0.0
    have_l = {h.lower().strip() for h in have}
    for w in want:
        if w.lower().strip() in have_l:
            hits += 1
        elif _words([w]) & hw:
            hits += 0.6
    return min(1.0, hits / len(want))


def _profile_out(row: CofounderProfileRow) -> dict:
    return {"user_id": row.user_id, "name": row.name, "role": row.role, "location": row.location, "bio": row.bio,
            "skills": row.skills or [], "looking_for": row.looking_for or [], "project": row.project,
            "commitment": row.commitment, "visible": row.visible, "technical": bool(TECH.search(" ".join(
                (row.skills or []) + [row.role or ""])))}


def builder_routes(app: FastAPI, store: FeedStore, posts: PostsReader, _user: Callable, config=None,
                   tagger=None) -> ProblemBank:
    """`tagger` (enrichment.llm.Tagger) runs the AI buyer panel; without it the panel answers 503."""
    bank = ProblemBank(store, posts)
    app.state.problem_bank = bank

    @app.get("/problems")
    def get_problems(user_id: str, me: Me, q: str | None = Query(None, max_length=200),
                     industry: str | None = Query(None, max_length=100), limit: int = Query(200, ge=1, le=500)) -> dict:
        _user(user_id, me)
        problems = bank.all()
        counts = Counter(p["industry"] for p in problems if p["industry"])
        if industry:
            problems = [p for p in problems if p["industry"] == industry]
        if q:
            words = q.lower().split()
            problems = [p for p in problems if all(w in " ".join(str(p[k] or "") for k in (
                "title", "statement", "summary", "industry", "niche", "segment")).lower() for w in words)]
        return {"total": len(problems), "industries": [{"name": n, "count": c} for n, c in counts.most_common()],
                "items": [card(p) for p in problems[:limit]]}

    @app.get("/problems/{problem_id}")
    def get_problem(problem_id: str, user_id: str, me: Me) -> dict:
        _user(user_id, me)
        p = bank.get(problem_id)
        if p is None:
            raise HTTPException(status_code=404, detail="unknown problem")
        return detail(p)

    # --- saved problems and their workspaces ---

    def _ws(row: WorkspaceRow, live: dict | None) -> dict:
        # Current numbers when the problem is still in the bank; the saved evidence and questions either way.
        problem = {**row.problem, **{k: v for k, v in card(live).items() if k != "evidence"}} if live else row.problem
        return {"problem_id": row.problem_id, "problem": problem, "in_bank": live is not None,
                "note": row.note, "data": row.data or {}, "saved_at": row.saved_at.isoformat(),
                "updated_at": row.updated_at.isoformat()}

    def _snapshot(p: dict) -> dict:
        snap = card(p)
        snap["evidence"] = [{k: e[k] for k in ("post_id", "summary", "source", "group", "who", "published_at", "url")}
                            for e in p["evidence"][:12]]
        snap["open_questions"] = p["open_questions"]
        snap["audience"] = p["audience"]
        return snap

    @app.get("/workspaces")
    def get_workspaces(user_id: str, me: Me) -> dict:
        _user(user_id, me)
        with store.session() as s:
            rows = list(s.scalars(select(WorkspaceRow).where(WorkspaceRow.user_id == user_id)
                                  .order_by(WorkspaceRow.saved_at)))
        return {"items": [_ws(r, bank.get(r.problem_id)) for r in rows]}

    @app.post("/workspaces/{problem_id}")
    def save_problem(problem_id: str, body: SaveProblem, me: Me) -> dict:
        """Save a problem: its workspace is created (or the note updated if it was saved before)."""
        _user(body.user_id, me)
        p = bank.get(problem_id)
        now = utcnow()
        with store.session() as s, s.begin():
            row = s.get(WorkspaceRow, (body.user_id, problem_id))
            if row is None and p is not None and p["id"] != problem_id:
                row = s.get(WorkspaceRow, (body.user_id, p["id"]))
            if row is None:
                if p is None:
                    raise HTTPException(status_code=404, detail="unknown problem")
                row = WorkspaceRow(user_id=body.user_id, problem_id=p["id"], problem=_snapshot(p), note=body.note,
                                   data={}, saved_at=now, updated_at=now)
                s.add(row)
            elif body.note is not None:
                row.note, row.updated_at = body.note, now
        return _ws(row, p)

    @app.post("/workspaces")
    def add_own_problem(body: OwnProblem, me: Me) -> dict:
        """A problem the builder brings themselves: a workspace with no evidence from the bank."""
        _user(body.user_id, me)
        import uuid
        now, pid = utcnow(), "own-" + uuid.uuid4().hex[:16]
        problem = {"id": pid, "title": body.title.strip(), "statement": None, "summary": None,
                   "industry": body.industry, "niche": None, "segment": body.who, "buyer": None, "score": None,
                   "posts": 0, "buyers": 0, "evidence": [], "workarounds": [], "tools": [], "own": True,
                   "audience": {"role": body.who, "segment": None, "payer": None, "channels": []}, "open_questions": []}
        with store.session() as s, s.begin():
            row = WorkspaceRow(user_id=body.user_id, problem_id=pid, problem=problem, note=body.note, data={},
                               saved_at=now, updated_at=now)
            s.add(row)
        return _ws(row, None) | {"in_bank": False}

    @app.put("/workspaces/{problem_id}")
    def put_workspace(problem_id: str, body: WorkspaceData, me: Me) -> dict:
        _user(body.user_id, me)
        import json
        if len(json.dumps(body.data)) > MAX_DATA_BYTES:
            raise HTTPException(status_code=413, detail="workspace too large")
        with store.session() as s, s.begin():
            row = s.get(WorkspaceRow, (body.user_id, problem_id))
            if row is None:
                raise HTTPException(status_code=404, detail="not saved")
            row.data, row.updated_at = body.data, utcnow()
        return {"ok": True, "updated_at": row.updated_at.isoformat()}

    @app.delete("/workspaces/{problem_id}")
    def delete_workspace(problem_id: str, me: Me, user_id: str = Query(min_length=1, max_length=64)) -> dict:
        _user(user_id, me)
        with store.session() as s, s.begin():
            n = s.execute(delete(WorkspaceRow).where(WorkspaceRow.user_id == user_id,
                                                     WorkspaceRow.problem_id == problem_id)).rowcount
        if not n:
            raise HTTPException(status_code=404, detail="not saved")
        return {"ok": True}

    panels: dict[tuple[str, str], int] = {}       # (user, day) -> panels run

    @app.post("/workspaces/{problem_id}/panel")
    def post_panel(problem_id: str, body: PanelIn, me: Me) -> dict:
        """Five simulated buyers react to the idea (opportunities/assess.py). A simulation to sharpen
        questions, never evidence; metered against the Claude budget, PANEL_DAILY_LIMIT a day per user."""
        _user(body.user_id, me)
        from .enrichment.costs import BudgetExceededError, CostLedger, claude_usd, claude_worst_case_usd
        from .enrichment.llm import TransientError
        from .opportunities.assess import PANEL_CALL, PANEL_SYSTEM, Panel, panel_prompt
        if tagger is None or config is None:
            raise HTTPException(status_code=503, detail="the panel is off")
        with store.session() as s:
            row = s.get(WorkspaceRow, (body.user_id, problem_id))
        if row is None:
            raise HTTPException(status_code=404, detail="not saved")
        day = (body.user_id, utcnow().date().isoformat())
        if panels.get(day, 0) >= PANEL_DAILY_LIMIT:
            raise HTTPException(status_code=429, detail="panel limit reached for today")
        live = bank.get(problem_id)
        problem = live or {**row.problem, "evidence": [e | {"role": "core", "tags": {}} for e in row.problem.get("evidence", [])]}
        user = panel_prompt(problem, body.pitch, body.price)
        ledger, ecfg = CostLedger(store), config.enrichment
        try:
            ledger.check("anthropic", claude_worst_case_usd(PANEL_CALL, len(PANEL_SYSTEM) + len(user), ecfg),
                         ecfg.monthly_budget_usd)
            attempt = tagger.tag(PANEL_CALL, PANEL_SYSTEM, user, output_model=Panel)
        except (BudgetExceededError, TransientError):
            raise HTTPException(status_code=503, detail="the panel is busy; try again later") from None
        for usage in attempt.usages:
            ledger.record(provider="anthropic", purpose="ai_panel", usage=usage, usd=claude_usd(usage, ecfg),
                          request_id=attempt.request_id)
        if attempt.output is None:
            raise HTTPException(status_code=503, detail="the panel didn't answer; try again")
        panels[day] = panels.get(day, 0) + 1
        return attempt.output.model_dump() | {"at": utcnow().isoformat(), "pitch": body.pitch, "price": body.price}

    # --- cofounders ---

    @app.get("/cofounders")
    def get_cofounders(user_id: str, me: Me) -> dict:
        """My profile, and every other visible profile scored against it, best first."""
        _user(user_id, me)
        with store.session() as s:
            mine = s.get(CofounderProfileRow, user_id)
            others = list(s.scalars(select(CofounderProfileRow).where(CofounderProfileRow.user_id != user_id,
                                                                      CofounderProfileRow.visible.is_(True))))
            asked = {r.to_user for r in s.scalars(select(IntroRequestRow).where(IntroRequestRow.from_user == user_id))}
            asked_me = {r.from_user for r in s.scalars(select(IntroRequestRow).where(IntroRequestRow.to_user == user_id))}
            spaces = {}
            for uid, problem in s.execute(select(WorkspaceRow.user_id, WorkspaceRow.problem)):
                spaces.setdefault(uid, set()).add((problem or {}).get("industry"))
            mutual = asked & asked_me
            emails = {u: e for u, e in s.execute(select(AccountRow.user_id, AccountRow.email).where(
                AccountRow.user_id.in_(mutual)))} if mutual else {}
        my_spaces = spaces.get(user_id, set()) - {None}
        out = []
        for o in others:
            shared = sorted(my_spaces & (spaces.get(o.user_id, set()) - {None}))
            if mine is not None:
                score = 0.45 * _overlap(mine.looking_for or [], o.skills or []) \
                    + 0.35 * _overlap(o.looking_for or [], mine.skills or []) + (0.2 if shared else 0.0)
            else:
                score = 0.2 if shared else 0.0
            d = _profile_out(o) | {"match": round(score * 100), "shared_spaces": shared,
                                   "asked": o.user_id in asked, "asked_me": o.user_id in asked_me,
                                   "email": emails.get(o.user_id)}
            d["nearby"] = bool(mine and mine.location and o.location
                               and mine.location.split(",")[0].strip().lower() == o.location.split(",")[0].strip().lower())
            out.append(d)
        out.sort(key=lambda d: (-d["asked_me"], -d["match"], d["name"]))
        return {"me": _profile_out(mine) if mine else None, "matches": out}

    @app.put("/cofounders/me")
    def put_profile(body: ProfileIn, me: Me) -> dict:
        _user(body.user_id, me)
        clean = lambda xs: list(dict.fromkeys(x.strip()[:60] for x in xs if x.strip()))  # noqa: E731
        with store.session() as s, s.begin():
            row = s.get(CofounderProfileRow, body.user_id) or CofounderProfileRow(user_id=body.user_id)
            row.name, row.role, row.location, row.bio = body.name.strip(), body.role, body.location, body.bio
            row.skills, row.looking_for = clean(body.skills), clean(body.looking_for)
            row.project, row.commitment, row.visible, row.updated_at = body.project, body.commitment, body.visible, utcnow()
            s.merge(row)
        return {"ok": True}

    @app.post("/cofounders/intro")
    def post_intro(body: IntroIn, me: Me) -> dict:
        _user(body.user_id, me)
        if body.to == body.user_id:
            raise HTTPException(status_code=422, detail="that's you")
        with store.session() as s, s.begin():
            other = s.get(CofounderProfileRow, body.to)
            if other is None or not other.visible or s.get(CofounderProfileRow, body.user_id) is None:
                raise HTTPException(status_code=404, detail="no such profile")
            if s.get(IntroRequestRow, (body.user_id, body.to)) is None:
                s.add(IntroRequestRow(from_user=body.user_id, to_user=body.to, at=utcnow()))
        return {"ok": True}

    @app.delete("/cofounders/intro")
    def delete_intro(me: Me, user_id: str = Query(min_length=1, max_length=64),
                     to: str = Query(min_length=1, max_length=64)) -> dict:
        _user(user_id, me)
        with store.session() as s, s.begin():
            s.execute(delete(IntroRequestRow).where(IntroRequestRow.from_user == user_id, IntroRequestRow.to_user == to))
        return {"ok": True}

    return bank
