from datetime import datetime, timedelta, timezone

from signal_connectors.models import FetchResult, RawPost

LONG = "Every Friday I copy invoices from email into QuickBooks by hand and it takes four hours."


def post(url, text=LONG, source="hn", **kw):
    return RawPost(source=source, source_type="community", route="own_api", url=url, text=text,
                   is_snippet=False, published_at=datetime(2026, 10, 1, tzinfo=timezone.utc), **kw)


def test_save_and_read_back_round_trips(store):
    p = post("https://a.example/1", engagement={"upvotes": 3}, raw={"k": [1, 2]}, money={"budget_usd": 500})
    assert store.save_posts([p]).new == 1
    got = store.get_post(p.id)
    assert got == p
    assert got.published_at.tzinfo is not None


def test_idempotent_by_id(store):
    store.save_posts([post("https://a.example/1")])
    result = store.save_posts([post("https://a.example/1?utm_source=feed")])
    assert (result.new, result.duplicates) == (0, 1)
    assert store.count_posts() == 1


def test_cross_posts_dedupe_by_text_hash(store):
    store.save_posts([post("https://a.example/1")])
    r = store.save_posts([post("https://b.example/2", source="reddit", text=LONG.upper() + "!!")])
    assert r.duplicates == 1


def test_duplicates_within_one_batch(store):
    r = store.save_posts([post("https://a.example/1"), post("https://a.example/1"),
                          post("https://a.example/2", text="short"), post("https://a.example/3", text="short")])
    # short texts aren't hashed, so 2 and 3 are distinct posts
    assert (r.new, r.duplicates) == (3, 1)


def test_last_success_only_counts_ok_runs(store):
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    store.record_run(FetchResult(source="hn", started_at=t0, status="ok"))
    store.record_run(FetchResult(source="hn", started_at=t0 + timedelta(hours=1), status="partial"))
    store.record_run(FetchResult(source="hn", started_at=t0 + timedelta(hours=2), status="failed"))
    assert store.last_success("hn") == t0
    assert store.last_success("other") is None


def test_deletion_recheck_and_purge(store):
    a, b = post("https://a.example/1"), post("https://a.example/2", text="other text entirely, long enough to hash ok")
    store.save_posts([a, b])
    due = store.ids_for_recheck("hn", datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert {i for i, _ in due} == {a.id, b.id}
    assert store.delete_posts([a.id]) == 1
    assert store.get_post(a.id) is None and store.count_posts() == 1
