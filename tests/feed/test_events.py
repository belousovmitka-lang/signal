from datetime import timedelta

import pytest
from pydantic import ValidationError

from signal_feed.db import ImpressionRow
from signal_feed.events import BatchTooLargeError, EventBatch, EventIn, UnknownUserError, log_events

from .conftest import NOW


def ev(i, type="save", item_id="", **kw):
    return {"client_event_id": f"e{i}", "session_id": "s1", "type": type, "item_id": item_id,
            "at": NOW - timedelta(minutes=1), **kw}


def run(batch_events, feed_store, posts, feed_config, user="u1"):
    return log_events(EventBatch(user_id=user, events=batch_events), feed_store, posts, feed_config, now=NOW)


def test_logs_events_with_spec_weights(feed_store, posts, feed_config, user_id, post_ids):
    short, _ = post_ids
    r = run([ev(1, "save", short), ev(2, "outreach", short), ev(3, "not_for_me", short, reason="too_crowded")],
            feed_store, posts, feed_config)
    assert (r.accepted, r.duplicates, r.rejected) == (3, 0, [])
    rows = feed_store.events_for(user_id)
    assert [(e.event_type, e.weight) for e in rows] == [("save", 4), ("outreach", 12), ("not_for_me", -6)]
    assert rows[2].reason == "too_crowded"
    assert all(e.config_version == feed_config.version and e.received_at == NOW for e in rows)


def test_resending_a_batch_is_idempotent(feed_store, posts, feed_config, user_id, post_ids):
    batch = [ev(1, "save", post_ids[0]), ev(2, "share", post_ids[0])]
    run(batch, feed_store, posts, feed_config)
    r = run(batch + [ev(3, "expand", post_ids[0])], feed_store, posts, feed_config)
    assert (r.accepted, r.duplicates) == (1, 2)
    assert feed_store.count_events(user_id) == 3


def test_duplicate_ids_within_one_batch(feed_store, posts, feed_config, user_id, post_ids):
    r = run([ev(1, "save", post_ids[0]), ev(1, "save", post_ids[0])], feed_store, posts, feed_config)
    assert (r.accepted, r.duplicates) == (1, 1)


def test_same_client_id_from_different_users_is_not_a_duplicate(feed_store, posts, feed_config, user_id, post_ids):
    feed_store.create_user("u2")
    run([ev(1, "save", post_ids[0])], feed_store, posts, feed_config)
    r = run([ev(1, "save", post_ids[0])], feed_store, posts, feed_config, user="u2")
    assert r.accepted == 1


def test_bad_events_are_rejected_individually(feed_store, posts, feed_config, user_id, post_ids):
    short, _ = post_ids
    r = run([
        ev(1, "save", "no-such-post"),
        ev(2, "save", short, at=NOW + timedelta(hours=1)),
        ev(3, "save", short, at=NOW - timedelta(days=31)),
        ev(4, "save", "999", item_type="problem"),
        ev(5, "save", short, impression_id=12345),
        ev(6, "save", short),
    ], feed_store, posts, feed_config)
    assert r.accepted == 1
    assert [(x.client_event_id, x.error) for x in r.rejected] == [
        ("e1", "unknown post"), ("e2", "timestamp in the future"), ("e3", "timestamp too old"),
        ("e4", "unknown problem"), ("e5", "unknown impression")]


def test_impressions_of_other_users_are_rejected(feed_store, posts, feed_config, user_id, post_ids):
    feed_store.create_user("u2")
    with feed_store.session() as s, s.begin():
        imp = ImpressionRow(user_id="u2", session_id="s", item_type="post", item_id=post_ids[0], position=0,
                            shown_at=NOW, config_version=1)
        s.add(imp)
    r = run([ev(1, "save", post_ids[0], impression_id=imp.id)], feed_store, posts, feed_config)
    assert r.rejected[0].error == "unknown impression"


@pytest.mark.parametrize("dwell_ms, visible, weight, note", [
    (800, True, -0.3, None),
    (1500, True, 0, "dwell_too_long"),
    (800, False, 0, "not_fully_visible"),
])
def test_scroll_past_counts_only_quick_and_fully_visible(feed_store, posts, feed_config, user_id, post_ids,
                                                         dwell_ms, visible, weight, note):
    run([ev(1, "scroll_past", post_ids[0], dwell_ms=dwell_ms, fully_visible=visible)], feed_store, posts, feed_config)
    row = feed_store.events_for(user_id)[0]
    assert (row.weight, row.weight_note) == (weight, note)


@pytest.mark.parametrize("dwell_ms, weight", [(72_000, 1), (71_000, 0)])
def test_read_needs_60_percent_of_expected_time(feed_store, posts, feed_config, user_id, post_ids, dwell_ms, weight):
    # 460 words at 230 wpm = 120 s expected; 60% = 72 s.
    run([ev(1, "read", post_ids[1], dwell_ms=dwell_ms)], feed_store, posts, feed_config)
    assert feed_store.events_for(user_id)[0].weight == weight


def test_short_posts_use_the_minimum_reading_time(feed_store, posts, feed_config, user_id, post_ids):
    # 8 words -> floor of 3 s; 60% = 1.8 s.
    run([ev(1, "read", post_ids[0], dwell_ms=1700), ev(2, "read", post_ids[0], dwell_ms=1900)],
        feed_store, posts, feed_config)
    assert [e.weight for e in feed_store.events_for(user_id)] == [0, 1]


def test_useful_answers_also_land_in_surveys(feed_store, posts, feed_config, user_id, post_ids):
    from signal_feed.db import SurveyRow
    run([ev(1, "useful_yes", post_ids[0]), ev(2, "useful_no", post_ids[1])], feed_store, posts, feed_config)
    with feed_store.session() as s:
        answers = [(r.item_id, r.useful) for r in s.query(SurveyRow).order_by(SurveyRow.id)]
    assert answers == [(post_ids[0], True), (post_ids[1], False)]


def test_unknown_user_and_oversized_batches_fail_whole(feed_store, posts, feed_config, user_id, post_ids):
    with pytest.raises(UnknownUserError):
        run([ev(1, "save", post_ids[0])], feed_store, posts, feed_config, user="ghost")
    too_many = [ev(i, "impression", post_ids[0]) for i in range(feed_config.events.max_batch_size + 1)]
    with pytest.raises(BatchTooLargeError):
        run(too_many, feed_store, posts, feed_config)
    assert feed_store.count_events() == 0


@pytest.mark.parametrize("fields, message", [
    ({"type": "save", "reason": "too_small"}, "only allowed on not_for_me"),
    ({"type": "read"}, "need dwell_ms"),
    ({"type": "scroll_past", "dwell_ms": 100}, "need fully_visible"),
    ({"type": "not_for_me", "reason": "boring"}, "reason"),
    ({"type": "like"}, "type"),
])
def test_shape_errors(fields, message):
    with pytest.raises(ValidationError, match=message):
        EventIn.model_validate({**ev(1, item_id="x"), **fields})
