import json

from .scrub import scrub_text


def test_json_personal_fields_and_person_objects():
    body = {"items": [{"title": "Q", "owner": {"account_id": 42, "display_name": "Real Name", "link": "https://x/u/1"},
                       "author": "pg", "_tags": ["story", "author_pg"], "zip_code": "43004", "score": 3}],
            "account": {"acct": "pat@x.social", "username": "pat", "note": "<p>bio</p>", "indexable": True,
                        "noindex": False, "bot": False, "url": "https://x.social/@pat"},
            "pointOfContact": [{"fullName": "Pat", "email": "p@a.gov"}],
            "url": "https://mastodon.social/@pat/1"}
    out = json.loads(scrub_text(json.dumps(body), "application/json"))
    item = out["items"][0]
    assert item["owner"] == {"account_id": 42, "display_name": "redacted", "link": "redacted"}
    assert item["author"] == "redacted" and item["_tags"] == ["story", "author_redacted"] and item["zip_code"] == "redacted"
    assert item["title"] == "Q" and item["score"] == 3
    acct = out["account"]
    assert acct["acct"] == acct["username"] == acct["note"] == acct["url"] == "redacted"
    assert acct["indexable"] is True and acct["bot"] is False          # consent flags kept
    assert out["pointOfContact"] == "redacted" and out["url"] == "https://mastodon.social/@pat/1"


def test_xml_and_feeds():
    xml = ("<rss><item><title>T</title><dc:creator><![CDATA[Jane]]></dc:creator><author>j@x.com (Jane)</author></item></rss>")
    assert scrub_text(xml, "application/rss+xml") == \
        "<rss><item><title>T</title><dc:creator>redacted</dc:creator><author>redacted</author></item></rss>"
    form_d = "<x><relatedPersonsList><relatedPersonInfo>Jane</relatedPersonInfo></relatedPersonsList><street1>1 Main</street1></x>"
    assert "Jane" not in scrub_text(form_d, "text/xml") and "Main" not in scrub_text(form_d, "text/xml")


def test_emails_in_free_text():
    from .scrub import scrub_body
    r = {"headers": {"Content-Type": ["application/json"]},
         "body": {"string": '{"description": "Contact Christopher.dickenson@nhs.net by Friday"}'}}
    assert "nhs.net" not in scrub_body(r)["body"]["string"]


def test_person_objects_keep_ids_and_flags():
    post = {"uri": "at://did:plc:x/app.bsky.feed.post/1",
            "author": {"did": "did:plc:x", "handle": "pat.bsky.social", "displayName": "Pat", "labels": []}}
    out = json.loads(scrub_text(json.dumps(post), "application/json"))
    assert out["author"] == {"did": "did:plc:x", "handle": "redacted", "displayName": "redacted", "labels": []}
    assert out["uri"] == post["uri"]


def test_github_users():
    issue = {"html_url": "https://github.com/a/b/issues/1", "user": {"login": "octo", "id": 9, "avatar_url": "x",
             "html_url": "https://github.com/octo", "type": "User"}, "assignees": [{"login": "cat"}]}
    out = json.loads(scrub_text(json.dumps(issue), "application/json"))
    assert out["user"] == {"login": "redacted", "id": 9, "avatar_url": "redacted", "html_url": "redacted", "type": "User"}
    assert out["assignees"] == [{"login": "redacted"}] and out["html_url"] == issue["html_url"]
