import asyncio, tempfile, os
from pathlib import Path

# package must import with NO browser_use / anthropic installed
import databroker.orchestrator as orch
from databroker.core.models import BrokerRecord, Candidate, User, Status, Method, JobState
from databroker.core.store import BrokerStore, CandidateStore
from databroker.core.queue import InMemoryQueue, SqliteQueue
from databroker.core import classify
from databroker.stages import remover

def test_models_roundtrip():
    r = BrokerRecord.from_dict({"name":"X","domain":"x.com","method":"web_form",
        "scouted":True,"last_verified":"2026-01-01"})
    assert r.status == Status.VERIFIED            # inferred from legacy dict
    assert r.is_actionable()
    assert BrokerRecord.from_dict(r.to_dict()).domain == "x.com"
    dead = BrokerRecord.from_dict({"name":"D","domain":"d.com","notes":"PAGE DEAD"})
    assert dead.status == Status.DEAD and not dead.is_actionable()
    print("models roundtrip OK")

def test_classify():
    broker = "<h1>Reverse Phone Lookup</h1> remove my information do not sell my info"
    notbroker = "<h1>Best Pasta Recipe</h1> add to cart free shipping"
    assert classify.classify(broker)[0] is True
    assert classify.classify(notbroker)[0] is False
    print("classify OK:", classify.classify(broker)[1], classify.classify(notbroker)[1])

async def test_store(tmp):
    bs = BrokerStore(tmp/"b.yaml")
    await bs.upsert(BrokerRecord(name="A", domain="https://www.a.com/", method="web_form",
                                 scouted=True, status=Status.VERIFIED, last_verified="2026-06-01"))
    # re-load and merge a richer record (adds a recipe)
    bs2 = BrokerStore(tmp/"b.yaml")
    assert bs2.has("a.com")
    await bs2.upsert(BrokerRecord(name="A", domain="a.com", method="web_form", scouted=True,
        status=Status.VERIFIED, click_path_structured=[{"action":"navigate","url":"x"}]))
    bs3 = BrokerStore(tmp/"b.yaml")
    assert len(bs3.records)==1 and bs3.get("a.com").click_path_structured
    # candidate dedup against known broker
    cs = CandidateStore(tmp/"c.yaml")
    assert await cs.add(Candidate(domain="new.com"), known=bs3.all_domains()) is True
    assert await cs.add(Candidate(domain="a.com"), known=bs3.all_domains()) is False
    assert await cs.add(Candidate(domain="new.com")) is False  # dup
    print("store OK")

async def test_queues(tmp):
    for q in (InMemoryQueue(), SqliteQueue(tmp/"q.sqlite","t")):
        await q.put({"x":1}); await q.put({"x":2})
        a = await q.get(timeout=2); assert a["x"]==1
        await q.ack(a)
        b = await q.get(timeout=2); assert b["x"]==2
        await q.nack(b)                      # requeue
        c = await q.get(timeout=2); assert c["x"]==2
        await q.ack(c)
        assert await q.size()==0
    print("queues OK (in-memory + durable sqlite)")

async def test_remover():
    # a real recipe shape from the registry (360 Media Direct style)
    rec = BrokerRecord(name="X", domain="x.com", method="web_form", scouted=True,
        status=Status.VERIFIED, opt_out_direct_url="https://x.com/optout",
        click_path_structured=[
            {"action":"navigate","url":"https://x.com/optout"},
            {"action":"fill","field":"email","value":"{user_email}"},
            {"action":"fill","field":"name","value":"{user_name}"},
            {"action":"submit","label":"Submit"}])
    user = User(user_id="u1", name="Jane Doe", emails=["jane@example.com"])
    steps, missing = remover.resolve_recipe(rec, user.profile())
    filled = [s["value"] for s in steps if s["action"]=="fill"]
    assert "jane@example.com" in filled and "Jane Doe" in filled and missing==[]
    assert remover.triage(rec, missing)==JobState.QUEUED
    job = await remover.Remover().execute(rec, user)
    assert job.state==JobState.DRY_RUN_OK   # NullDriver fills but does not submit
    # id-required -> human
    rec.id_required=True
    assert remover.triage(rec, [])==JobState.NEEDS_HUMAN
    # corrupt recipe -> human
    rec.id_required=False; rec.click_path_structured=["not a dict"]
    _, m = remover.resolve_recipe(rec, user.profile())
    assert remover.triage(rec, m)==JobState.NEEDS_HUMAN
    print("remover OK (resolve + triage + replay)")

async def test_end_to_end_no_network(tmp):
    # candidate -> scout (recon email short-circuit, fake fetcher) -> store -> remove
    import databroker.core.recon as R
    from databroker.stages import scout
    home='<html><a href="mailto:privacy@ex.com">Privacy</a></html>'
    def fake(url, want_bytes=False):
        if want_bytes: return {"status":200,"url":url,"text":"","headers":{},"content":b"i"}
        if url.rstrip("/")=="https://ex.com": return {"status":200,"url":url,"text":home,"headers":{},"content":b""}
        return {"status":404,"url":url,"text":"","headers":{},"content":b""}
    R._default_fetcher=fake
    bs=BrokerStore(tmp/"b.yaml")
    rec=await scout.scout_candidate(Candidate(domain="ex.com"), bs, do_shots=False)
    assert rec.method==Method.EMAIL and rec.status==Status.VERIFIED
    await bs.upsert(rec)
    assert BrokerStore(tmp/"b.yaml").get("ex.com").method=="email"
    print("end-to-end (candidate->scout->store) OK")

def main():
    test_models_roundtrip(); test_classify()
    with tempfile.TemporaryDirectory() as d:
        tmp=Path(d)
        asyncio.run(test_store(tmp))
        asyncio.run(test_queues(tmp))
        asyncio.run(test_remover())
        asyncio.run(test_end_to_end_no_network(tmp))
    print("\nALL PIPELINE TESTS PASSED")

main()
