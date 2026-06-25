import sys; sys.path.insert(0, ".")
from databroker.core.models import BrokerRecord, User, Status, Method, JobState
from databroker.stages import applicability as A
from databroker.stages import remover

def fetch_map(pages):
    def f(url, want_bytes=False):
        for k,v in pages.items():
            if url.startswith(k): return {"status":200,"url":url,"text":v,"headers":{},"content":b""}
        return {"status":404,"url":url,"text":"","headers":{},"content":b""}
    return f

us_user = User(user_id="u1", name="Jane Doe", emails=["j@x.com"],
               addresses=[{"city":"Phoenix","state":"AZ","zip":"85001"}], regions=["US"])

def t_jurisdiction():
    eu_only = BrokerRecord(name="EU", domain="eu.com", method="web_form", status=Status.VERIFIED,
                           jurisdiction=["EU"], scouted=True)
    d = A.gate(eu_only, us_user, fetch_map({}))
    assert d.action == "skip_not_listed", d
    print("jurisdiction skip OK")

def t_blanket():
    b = BrokerRecord(name="Acxiom", domain="acxiom.com", method="email", status=Status.VERIFIED,
                     jurisdiction=["US"], requires_listing_url=False, scouted=True)
    assert A.gate(b, us_user, fetch_map({})).action == "submit"
    print("blanket submit OK")

def t_listing_found():
    b = BrokerRecord(name="PS", domain="ps.com", method="web_form", status=Status.VERIFIED,
        jurisdiction=["US"], requires_listing_url=True,
        search_url_template="https://ps.com/s?fn={user_first}&ln={user_last}&st={user_state}",
        listing_link_pattern=r'href="(/profile/[^"]+)"')
    results = '<a href="/profile/jane-doe-az-12345">Jane Doe, AZ</a>'
    d = A.gate(b, us_user, fetch_map({"https://ps.com/s":results}))
    assert d.action=="submit" and d.listing_url=="https://ps.com/profile/jane-doe-az-12345", d
    print("listing found OK:", d.listing_url)

def t_listing_not_found():
    b = BrokerRecord(name="PS", domain="ps2.com", method="web_form", status=Status.VERIFIED,
        jurisdiction=["US"], requires_listing_url=True,
        search_url_template="https://ps2.com/s?ln={user_last}",
        listing_link_pattern=r'href="(/profile/[^"]+)"')
    d = A.gate(b, us_user, fetch_map({"https://ps2.com/s":"<p>no results</p>"}))
    assert d.action=="skip_not_listed", d
    print("listing not-found -> skip OK")

def t_listing_needs_human():
    # requires listing but no template to search
    b = BrokerRecord(name="PS", domain="ps3.com", method="web_form", status=Status.VERIFIED,
        jurisdiction=["US"], requires_listing_url=True, scouted=True)
    d = A.gate(b, us_user, fetch_map({}))
    assert d.action=="needs_human", d
    print("listing no-template -> needs_human OK")

def t_resolved_listing_runs_auto():
    # a listing-required broker whose recipe navigates to {listing_url}, listing resolved
    rec = BrokerRecord(name="PS", domain="ps.com", method="web_form", status=Status.VERIFIED,
        requires_listing_url=True,
        click_path_structured=[
            {"action":"navigate","url":"{listing_url}"},
            {"action":"click","label":"Remove this record"},
            {"action":"submit","label":"Confirm"}])
    import asyncio
    job = asyncio.run(remover.Remover().execute(rec, us_user,
        extra={"listing_url":"https://ps.com/profile/jane-doe-az-12345"}))
    # navigate url should be substituted, and it should NOT fall to needs_human
    steps,_ = remover.resolve_recipe(rec, {**us_user.profile(),"listing_url":"https://ps.com/p/1"})
    nav = [s for s in steps if s["action"]=="navigate"][0]
    assert nav["url"]=="https://ps.com/p/1", nav
    assert job.state == JobState.DRY_RUN_OK, job
    print("resolved listing -> auto (dry_run_ok) OK; nav substituted")

def t_unresolved_listing_to_human():
    rec = BrokerRecord(name="PS", domain="ps.com", method="web_form", status=Status.VERIFIED,
        requires_listing_url=True, click_path_structured=[{"action":"navigate","url":"{listing_url}"}])
    import asyncio
    job = asyncio.run(remover.Remover().execute(rec, us_user))  # no listing
    assert job.state == JobState.NEEDS_HUMAN, job
    print("unresolved listing -> needs_human OK")

for fn in [t_jurisdiction,t_blanket,t_listing_found,t_listing_not_found,
           t_listing_needs_human,t_resolved_listing_runs_auto,t_unresolved_listing_to_human]:
    fn()
print("\nALL APPLICABILITY TESTS PASSED")
