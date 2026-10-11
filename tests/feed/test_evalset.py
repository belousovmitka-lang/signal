from datetime import datetime, timezone

from signal_connectors.models import RawPost
from signal_feed.db import EnrichedPostRow
from signal_feed.enrichment.evalset import Label, draw_sample, level, load_labels, report, score


def raw(i, source, source_type):
    return RawPost(source=source, source_type=source_type, route="rss", url=f"https://x.example/{source}/{i}",
                   text=f"post {i}", is_snippet=False, published_at=datetime(2026, 10, 1, tzinfo=timezone.utc))


def test_sample_spreads_across_sources_and_skips_aggregates():
    posts = ([raw(i, "big", "news") for i in range(50)] + [raw(i, "small", "news") for i in range(3)]
             + [raw(i, "cfpb", "government") for i in range(20)])
    picked = draw_sample(posts, ["cfpb"], plan={"news": 6, "government": 5})
    assert sum(p.source == "small" for p in picked) == 3 and len(picked) == 6
    assert draw_sample(posts, ["cfpb"], plan={"news": 6}) == draw_sample(posts, ["cfpb"], plan={"news": 6})


def label(pid, **kw):
    base = dict(post_id=pid, source="s", post_type="pain", industry="Legal", money=False, spam=False,
                intensity="high", buyer="medium")
    return Label(**(base | kw))


def enriched(pid, **kw):
    base = dict(post_id=pid, status="ok", post_type="pain", industry_path=["Legal"], money_signal=None,
                spam_score=0.0, intensity=0.9, buyer_likelihood=0.5, prompt_version="v1",
                enriched_at=datetime(2026, 10, 9, tzinfo=timezone.utc))
    return EnrichedPostRow(**(base | kw))


def test_score_counts_agreement():
    labels = [label("a"), label("b", post_type="opinion", money=True), label("c")]
    rows = {"a": enriched("a"), "b": enriched("b", money_signal={"kind": "rate"}, industry_path=["Other"])}
    s = score(labels, rows, spam_threshold=0.7, drop_types=["opinion"])
    assert (s.n, s.missing) == (2, 1)
    assert (s.feed["tp"], s.feed["fp"]) == (1, 1)   # "b" should be dropped as opinion but was kept
    assert s.post_type_accuracy == 0.5 and s.industry_hits == 1
    assert s.money["tp"] == 1 and len(s.disagreements) == 1
    assert "post_type accuracy" in report(s)


def test_levels():
    assert [level(v) for v in (0.1, 0.5, 0.9, None)] == ["low", "medium", "high", None]


def test_shipped_labels_cover_the_sample():
    import yaml
    from signal_feed.settings import REPO_ROOT
    sample = yaml.safe_load((REPO_ROOT / "evals" / "enrichment_sample.yaml").read_text())
    labels = load_labels().labels
    assert [lb.post_id for lb in labels] == [e["post_id"] for e in sample]
    assert len(labels) == 200
