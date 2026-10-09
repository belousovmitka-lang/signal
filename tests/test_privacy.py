import pytest

from signal_connectors.privacy import MissingSaltError, extract_author_role, hash_author


def test_hash_is_stable_keyed_and_namespaced():
    h = hash_author("Alice", "reddit", salt="s1")
    assert h == hash_author(" alice ", "reddit", salt="s1")
    assert h != hash_author("alice", "reddit", salt="s2")
    assert h != hash_author("alice", "hn", salt="s1")
    assert len(h) == 64 and "alice" not in h
    assert hash_author("", "reddit", salt="s1") is None


def test_missing_salt_refuses(monkeypatch):
    monkeypatch.delenv("AUTHOR_HASH_SALT", raising=False)
    with pytest.raises(MissingSaltError):
        hash_author("alice", "reddit")


ROLES = ["paralegal", "practice manager", "estimator", "manager", "nurse"]


@pytest.mark.parametrize("text, role", [
    ("I'm a paralegal at a small firm and we track deadlines in Excel", "paralegal"),
    ("As a practice manager, billing eats my week", "practice manager"),
    ("i work as an estimator for a GC", "estimator"),
    ("ICU nurse here. Charting takes forever", "nurse"),
    ("I am a senior paralegal", "paralegal"),
    ("Our paralegal quit and the manager is overwhelmed", None),
    ("As a result, nothing works", None),
])
def test_extract_author_role(text, role):
    assert extract_author_role(text, ROLES) == role
