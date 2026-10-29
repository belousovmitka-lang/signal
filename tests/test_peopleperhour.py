from datetime import datetime, timedelta, timezone

import httpx

from signal_connectors.connectors.peopleperhour import parse_age, parse_price
from signal_connectors.runner import run_source

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)

# The card markup as served on 2026-10-29 (class names carry build hashes), trimmed.
CARD = """<li class="list__item⤍List⤚2ytmm"><div class="item⤍ListItem⤚1iGUH">
<div class="card__user⤍ListItem⤚3sK3s"><a class="small card__user-link⤍ListItem⤚2M9qM"
 href="https://www.peopleperhour.com/freelancer/{slug}" title="View Ann's profile"><img alt="Ann's avatar"/>
 <span class="card__username⤍ListItem⤚QnBBG"> Ann P.</span></a></div>
<div class="u-txt--right card__price⤍ListItem⤚3VxJ9"><span class="title-nano"><div><span>{price}</span></div></span></div>
<p class="item__etiquettes⤍ListItem⤚3dvjP"><span class="etiquettes⤍JobEtiquette⤚1qsx8 label">opportunity</span></p>
<h6 class="item__title⤍ListItem⤚2FRMT"><a class="item__url⤍ListItem⤚20ULx"
 href="https://www.peopleperhour.com/freelance-jobs/technology-programming/mobile-app-development/{title_slug}-{job}">{title}</a></h6>
<p class="item__desc⤍ListItem⤚3f4JV"> I need my bookings app published, job {job}. Email ann@example.com </p>
<div class="card__footer⤍ListItem⤚1KHhv"><div class="nano card__footer-left⤍ListItem⤚16Odv"><span>{age}</span>
<span class="u-mgl--1">6 proposals</span><span>Remote</span></div></div></div></li>"""


def page(cards):
    return "<html><body><div class='list⤍List⤚3R-r9'><ul>" + "".join(cards) + "</ul></div></body></html>"


def card(job, age, price="$239", title="Expo + Supabase developer"):
    return CARD.format(slug=f"ann-{job}", price=price, title_slug="expo-supabase-developer", job=job, title=title, age=age)


def test_parse_age_and_price():
    assert parse_age("20 minutes ago 6 proposals", NOW) == NOW - timedelta(minutes=20)
    assert parse_age("an hour ago", NOW) == NOW - timedelta(hours=1) and parse_age("2 days ago", NOW) == NOW - timedelta(days=2)
    assert parse_age("Remote", NOW) is None
    assert parse_price("$2K") == {"amount": 2000.0, "currency": "USD", "type": "fixed", "stated": "$2K"}
    assert parse_price("£25/hr")["type"] == "hourly" and parse_price("€1,250")["amount"] == 1250.0


async def test_reads_pages_until_the_cursor(make_services):
    pages = {"1": page([card(4528819, "20 minutes ago"), card(4528817, "3 hours ago", price="$2K")]),
             "2": page([card(4528700, "2 days ago"), card(4528699, "3 days ago")]),
             "3": page([card(4528000, "5 days ago")])}
    seen = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /site/login\n")
        seen.append(dict(request.url.params))
        return httpx.Response(200, text=pages[request.url.params["page"]])

    sources = {"sources": {"peopleperhour": {"route": "public_pages", "source_type": "paid_demand",
                                             "access_check": {"robots": "allowed", "terms": "allowed", "decision": "full",
                                                              "checked_at": "2026-10-29"},
                                             "queries": [{"limit": 100}]}}}
    since = datetime.now(timezone.utc) - timedelta(days=1)
    async with make_services(sources, handler) as svc:
        out = await run_source(svc, svc.sources.get("peopleperhour"), since=since, collect=True)
    assert [s["page"] for s in seen] == ["1", "2"] and seen[0]["sort"] == "latest"     # page 2 reaches the cursor
    assert out.result.status == "ok" and out.result.new == 2                          # the 2- and 3-day-old jobs dropped
    p = out.posts[0]
    assert p.title == "Expo + Supabase developer" and p.money["amount"] == 239.0 and p.engagement == {"proposals": 6}
    assert p.community == "technology-programming / mobile-app-development" and p.raw["job_id"] == "4528819"
    assert "[email]" in p.text and p.author_hash and "Ann" not in p.model_dump_json()
    assert out.posts[1].money["amount"] == 2000.0 and p.raw["labels"] == ["opportunity"]
