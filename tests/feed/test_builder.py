"""The builder app's routes: problems from job clusters, saved problems' workspaces, cofounder matching."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from signal_connectors.models import RawPost
from signal_connectors.store import Store
from signal_feed.api import create_app
from signal_feed.db import EnrichedPostRow, JobClusterPostRow, JobClusterRow
from signal_feed.posts import PostsReader

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


def bank(feed_store, tmp_path, built_at=NOW):
    url = f"sqlite:///{tmp_path / 'sig.db'}"
    specs = [("hackernews", "community", 85, "core", 0.8, 0.6, {"hours": 1, "per": "day"}, ["Clio"]),
             ("github", "community", 90, "core", 0.6, 0.4, None, ["Clio", "Zapier"]),
             ("peopleperhour", "paid_demand", 70, "supporting", 0.4, 0.9, None, [])]
    posts = [RawPost(source=src, source_type=st, route="own_api", url=f"https://e.example/{i}", text=f"post {i}",
                     is_snippet=False, author_hash=f"{i:064x}", author_role="paralegal" if i == 0 else None,
                     published_at=datetime(2026, 10, 1 + i, tzinfo=timezone.utc))
             for i, (src, st, *_) in enumerate(specs)]
    Store(url).save_posts(posts, text_dedupe=False)
    with feed_store.session() as s, s.begin():
        s.add(JobClusterRow(id=7, need="US law firms need to re-key intake forms", project_idea="an intake form "
                            "that fills the case system", buyer_type="small_business", buyer_segment="small law firms",
                            core_posts=2, supporting_posts=1, distinct_buyers=3, sources={"hackernews": 1},
                            best_score=90, first_seen=NOW, last_seen=NOW, built_at=built_at))
        for p, (_, _, score, role, pain, pay, time_cost, tools) in zip(posts, specs, strict=True):
            s.add(EnrichedPostRow(post_id=p.id, status="ok", post_type="pain", prompt_version="v9", enriched_at=NOW,
                                  industry_path=["Legal", "Intake"], project_score=score, intensity=pain,
                                  buyer_likelihood=pay, recurrence="recurring", time_cost=time_cost,
                                  tools_mentioned=tools, plain_summary=f"summary {p.url}",
                                  current_workaround="They retype it" if role == "core" else None,
                                  money_signal={"amount_min": 300, "currency": "USD"} if role == "supporting" else None))
    with feed_store.session() as s, s.begin():
        for p, spec in zip(posts, specs, strict=True):
            s.add(JobClusterPostRow(cluster_id=7, post_id=p.id, role=spec[3], cosine=0.9))
    return url, posts


def test_problems_are_clusters_with_numbers_from_the_posts(feed_store, feed_config, tmp_path, user_id):
    url, posts = bank(feed_store, tmp_path)
    c = TestClient(create_app(feed_store, PostsReader(url), feed_config))
    r = c.get("/problems", params={"user_id": user_id}).json()
    assert r["total"] == 1 and r["industries"] == [{"name": "Legal", "count": 1}]
    p = r["items"][0]
    assert p["id"] == posts[1].id                                  # the best core post: stable across rebuilds
    assert p["title"] == "Law firms need to re-key intake forms"   # "US " dropped
    assert p["pain"]["value"] == 6 and p["will_pay"]["value"] == 6 and p["frequency"]["label"] == "Recurring"
    assert p["frequency"]["reason"] == "1 h per day"
    assert p["will_pay"]["reason"] == "1 stated budget, median $300"
    assert p["competition"] == {"value": 5, "label": "Medium", "reason": "Clio, Zapier"}
    assert c.get("/problems", params={"user_id": user_id, "q": "dentist"}).json()["total"] == 0
    d = c.get(f"/problems/{posts[0].id}", params={"user_id": user_id}).json()   # any member post finds it
    assert d["id"] == posts[1].id and [e["role"] for e in d["evidence"]] == ["core", "core", "supporting"]
    assert d["audience"]["role"] == "Paralegal" and d["workarounds"] == [{"what": "They retype it",
                                                                           "source": "github"}]
    assert c.get("/problems/nope", params={"user_id": user_id}).status_code == 404


def test_saving_makes_a_workspace_that_keeps_its_data(feed_store, feed_config, tmp_path, user_id):
    url, posts = bank(feed_store, tmp_path)
    c = TestClient(create_app(feed_store, PostsReader(url), feed_config))
    pid = posts[1].id
    w = c.post(f"/workspaces/{pid}", json={"user_id": user_id, "note": "I was a paralegal"}).json()
    assert w["problem_id"] == pid and w["note"] == "I was a paralegal" and w["in_bank"]
    assert c.put(f"/workspaces/{pid}", json={"user_id": user_id, "data": {"notes": "hi"}}).status_code == 200
    again = c.post(f"/workspaces/{posts[0].id}", json={"user_id": user_id}).json()   # same problem, another post
    assert again["problem_id"] == pid and again["data"] == {"notes": "hi"} and again["note"] == "I was a paralegal"
    assert [x["problem_id"] for x in c.get("/workspaces", params={"user_id": user_id}).json()["items"]] == [pid]
    big = {"x": "a" * 300_000}
    assert c.put(f"/workspaces/{pid}", json={"user_id": user_id, "data": big}).status_code == 413
    with feed_store.session() as s, s.begin():                      # the cluster leaves the bank
        s.query(JobClusterPostRow).delete()
        s.query(JobClusterRow).delete()
        s.add(JobClusterRow(id=8, need="other", core_posts=0, supporting_posts=0, distinct_buyers=0, best_score=80,
                            first_seen=NOW, last_seen=NOW, built_at=datetime(2026, 11, 9, tzinfo=timezone.utc)))
    kept = c.get("/workspaces", params={"user_id": user_id}).json()["items"][0]
    assert not kept["in_bank"] and kept["problem"]["title"] == "Law firms need to re-key intake forms"
    assert c.delete(f"/workspaces/{pid}", params={"user_id": user_id}).status_code == 200
    assert c.get("/workspaces", params={"user_id": user_id}).json()["items"] == []


def test_cofounders_match_on_skills_and_reveal_email_only_when_both_ask(feed_store, feed_config, tmp_path):
    from signal_feed.db import AccountRow
    url, _ = bank(feed_store, tmp_path)
    c = TestClient(create_app(feed_store, PostsReader(url), feed_config))
    for uid in ("a", "b", "c"):
        feed_store.create_user(uid)
    with feed_store.session() as s, s.begin():
        s.add(AccountRow(id="acc-b", email="b@example.com", user_id="b", created_at=NOW))
        s.add(AccountRow(id="acc-a", email="a@example.com", user_id="a", created_at=NOW))
    c.put("/cofounders/me", json={"user_id": "a", "name": "Ann", "skills": ["Sales"], "looking_for": ["Full-stack engineering"],
                                  "location": "Chicago, IL"})
    c.put("/cofounders/me", json={"user_id": "b", "name": "Ben", "skills": ["Full-stack engineering", "TypeScript"],
                                  "looking_for": ["Sales"], "location": "Chicago"})
    c.put("/cofounders/me", json={"user_id": "c", "name": "Cy", "skills": ["Cooking"], "visible": True})
    r = c.get("/cofounders", params={"user_id": "a"}).json()
    assert [m["name"] for m in r["matches"]] == ["Ben", "Cy"]
    ben = r["matches"][0]
    assert ben["match"] == 80 and ben["technical"] and ben["nearby"] and ben["email"] is None
    assert c.post("/cofounders/intro", json={"user_id": "a", "to": "b"}).status_code == 200
    assert c.get("/cofounders", params={"user_id": "a"}).json()["matches"][0]["email"] is None   # one side only
    assert c.get("/cofounders", params={"user_id": "b"}).json()["matches"][0]["asked_me"]
    c.post("/cofounders/intro", json={"user_id": "b", "to": "a"})
    assert c.get("/cofounders", params={"user_id": "a"}).json()["matches"][0]["email"] == "b@example.com"
    assert c.post("/cofounders/intro", json={"user_id": "a", "to": "a"}).status_code == 422


def test_the_score_is_the_weighted_tiles_and_an_assessment_replaces_the_tag_ratings(feed_store, feed_config, tmp_path, user_id):
    from signal_feed.db import ProblemAssessmentRow
    from signal_feed.opportunities.assess import VERSION, WEIGHTS
    url, posts = bank(feed_store, tmp_path)
    c = TestClient(create_app(feed_store, PostsReader(url), feed_config))
    p = c.get(f"/problems/{posts[1].id}", params={"user_id": user_id}).json()
    assert not p["assessed"] and p["market"]["value"] is None          # unknown ratings leave the average
    known = {"pain": 6, "frequency": 10, "will_pay": 6, "competition": 5, "evidence": 8}
    assert p["score"] == round(10 * sum(WEIGHTS[k] * v for k, v in known.items()) / sum(WEIGHTS[k] for k in known))
    assert "tags" not in p["evidence"][0]                              # the prompt's tags stay internal
    rating = lambda n: {"score": n, "reason": "r"}  # noqa: E731
    with feed_store.session() as s, s.begin():
        s.add(ProblemAssessmentRow(problem_id=posts[1].id, members_hash="x", version=VERSION, created_at=NOW, data={
            "pain": rating(7), "frequency": rating(8), "frequency_label": "daily", "will_pay": rating(6),
            "market": rating(5), "competition": "high", "competition_reason": "many tools", "existing_tools": [],
            "who_to_reach": [{"title": "Office manager", "org_type": "small law firm", "why": "w", "where": ["ABA"]}],
            "example_companies": [], "validation": [{"method": "presell", "plan": "ask for LOIs"}], "confidence": 0.7}))
    p = c.get(f"/problems/{posts[1].id}", params={"user_id": user_id}).json()
    assert p["assessed"] and p["frequency"]["label"] == "Daily" and p["who_to_reach"][0]["title"] == "Office manager"
    assert p["score"] == round(10 * (0.2 * 7 + 0.15 * 8 + 0.25 * 6 + 0.15 * 5 + 0.1 * 2 + 0.15 * 8))
    w = c.post(f"/workspaces/{posts[1].id}", json={"user_id": user_id}).json()
    assert c.post(f"/workspaces/{posts[1].id}/panel", json={"user_id": user_id}).status_code == 503   # no model here
    assert w["problem"]["validation"] == [{"method": "presell", "plan": "ask for LOIs"}]


def test_a_hosted_landing_page_counts_visits_and_collects_sign_ups(feed_store, feed_config, tmp_path, user_id):
    url, posts = bank(feed_store, tmp_path)
    c = TestClient(create_app(feed_store, PostsReader(url), feed_config))
    pid = posts[1].id
    page = {"user_id": user_id, "kind": "landing", "landing": {"headline": "Intake <script>x</script>", "price": "$49/month"}}
    assert c.put(f"/workspaces/{pid}/pages", json=page).status_code == 404            # save the problem first
    c.post(f"/workspaces/{pid}", json={"user_id": user_id})
    made = c.put(f"/workspaces/{pid}/pages", json=page).json()
    html_ = c.get(f"/p/{made['id']}", headers={"user-agent": "Mozilla"}).text
    assert "&lt;script&gt;" in html_ and "<script>x" not in html_                    # the builder's text is escaped
    assert c.post(f"/p/{made['id']}", content="email=nope", headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 422
    r = c.post(f"/p/{made['id']}", content="email=a%40b.example", follow_redirects=False,
               headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 303
    got = c.get(f"/workspaces/{pid}/pages", params={"user_id": user_id}).json()["landing"]
    assert got["views"] == 1 and [x["email"] for x in got["responses"]] == ["a@b.example"]
    survey = {"user_id": user_id, "kind": "survey", "survey": {"title": "Q", "questions": ["When?", "Cost?"]}}
    sid = c.put(f"/workspaces/{pid}/pages", json=survey).json()["id"]
    c.post(f"/p/{sid}", content="q0=yesterday", headers={"content-type": "application/x-www-form-urlencoded"})
    ans = c.get(f"/workspaces/{pid}/pages", params={"user_id": user_id}).json()["survey"]["responses"]
    assert ans[0]["answers"] == {"When?": "yesterday", "Cost?": ""}
    for _ in range(6):                                                                # 5 an hour per visitor
        last = c.post(f"/p/{sid}", content="q0=x", headers={"content-type": "application/x-www-form-urlencoded"})
    assert last.status_code == 429


def test_outreach_lists_only_organizations_whose_websites_check_out():
    from signal_feed.outreach import Group, OutreachPlan, Target, verify
    t = lambda name, site: Target(name=name, kind="company", website=site, contact_role="Owner", why="w",  # noqa: E731
                                  how="h", near_builder=False)
    plan = OutreachPlan(groups=[Group(name="Grocers", why="y", targets=[t("Real Foods", "realfoods.example"),
                                                                     t("Made Up", "madeup.example")]),
                                Group(name="Empty", why="y", targets=[t("Nope", "nope.example")])])
    groups, dropped = verify(plan, fetch=lambda x: "https://realfoods.example/" if x.name == "Real Foods" else None)
    assert dropped == 2 and [g["name"] for g in groups] == ["Grocers"]
    assert [x["name"] for x in groups[0]["targets"]] == ["Real Foods"] and groups[0]["targets"][0]["url"]


def test_the_builders_context_is_theirs(feed_store, feed_config, tmp_path, user_id):
    url, _ = bank(feed_store, tmp_path)
    c = TestClient(create_app(feed_store, PostsReader(url), feed_config))
    assert c.get("/me/context", params={"user_id": user_id}).json()["saved"] is False
    c.put("/me/context", json={"user_id": user_id, "context": {"location": "Nashville, TN", "now": "student"}})
    got = c.get("/me/context", params={"user_id": user_id}).json()
    assert got["saved"] and got["location"] == "Nashville, TN" and got["background"] == ""
