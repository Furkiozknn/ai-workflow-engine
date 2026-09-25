"""The engine against the real ai-job-gateway, not against our idea of it.

Every other test here drives `_fake_gateway()` — a handwritten `MockTransport`
that returns what we *believe* the gateway returns. That fake is the whole
risk. It encodes one reading of the contract, it was written once, and nothing
tells it when the gateway changes: a renamed field, a new status value, a
different envelope on submit, and these tests stay green while the pair stops
working. Two repositories that advertise compatibility with each other cannot
prove it by each mocking the other.

So this module runs the **actual** gateway. `ai-job-gateway` exposes
``create_app(manager)``, the engine accepts an injected ``http_client``, and
``httpx.ASGITransport`` connects them in-process: no server, no port, no
container, no network. A real submit, a real background provider run, real
polling until ready.

The suite skips itself when ai-job-gateway is not installed, so a contributor
working on the engine alone is not blocked. CI installs it, because the point
of the test is that CI is where the drift gets caught.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest

from ai_workflow_engine.pipeline import parse_pipeline_str
from ai_workflow_engine.runner import PipelineRunError, run_pipeline

gw = pytest.importorskip(
    "ai_job_gateway",
    reason="ai-job-gateway is not installed; the contract test needs the real one",
)

from ai_job_gateway.manager import JobManager  # noqa: E402
from ai_job_gateway.providers import EchoProvider, MockProvider  # noqa: E402
from ai_job_gateway.server import create_app  # noqa: E402
from ai_job_gateway.store import InMemoryJobStore  # noqa: E402


@pytest.fixture
async def gercek_gateway():
    """The real gateway, in this process, over ASGI."""
    registry = {
        "generate": EchoProvider(),
        "upscale": EchoProvider(),
        "lipsync": EchoProvider(),
        "kirik": MockProvider(delay_seconds=0.01, should_fail=True, failure_message="saglayici coktu"),
        "yavas": MockProvider(delay_seconds=0.25),
    }
    manager = JobManager(InMemoryJobStore(), registry, result_ttl=timedelta(minutes=5))
    app = create_app(manager)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gw.test"
    ) as client:
        yield client


def _boru(metin: str):
    return parse_pipeline_str(metin)


TEK_ADIM = """
name: tek
steps:
  - name: a
    capability: generate
    params:
      prompt: merhaba
"""

ZINCIR = """
name: zincir
steps:
  - name: a
    capability: generate
    params:
      prompt: merhaba
  - name: b
    capability: upscale
    depends_on: [a]
    params:
      source: "{{ steps.a.result.echoed.prompt }}"
"""

PARALEL = """
name: paralel
steps:
  - name: a
    capability: generate
    params: {prompt: bir}
  - name: b
    capability: upscale
    params: {prompt: iki}
  - name: c
    capability: lipsync
    depends_on: [a, b]
    params: {prompt: uc}
"""

KIRIK = """
name: kirik
steps:
  - name: a
    capability: kirik
    params: {prompt: x}
  - name: b
    capability: generate
    depends_on: [a]
    params: {prompt: y}
"""


@pytest.mark.asyncio
async def test_tek_adim_gercek_gateway_uzerinden_kosuyor(gercek_gateway):
    """The envelope, the polling URL and the ready payload all as the gateway
    really sends them - not as the fake believes."""
    sonuc = await run_pipeline(
        _boru(TEK_ADIM), "http://gw.test", http_client=gercek_gateway, poll_interval=0.01
    )
    assert set(sonuc) == {"a"}
    assert sonuc["a"].result == {"echoed": {"prompt": "merhaba"}}


@pytest.mark.asyncio
async def test_bir_adimin_ciktisi_digerine_gercekten_geciyor(gercek_gateway):
    sonuc = await run_pipeline(
        _boru(ZINCIR), "http://gw.test", http_client=gercek_gateway, poll_interval=0.01
    )
    assert sonuc["a"].result == {"echoed": {"prompt": "merhaba"}}
    assert sonuc["b"].result == {"echoed": {"source": "merhaba"}}


@pytest.mark.asyncio
async def test_bagimsiz_adimlar_birlikte_kosuyor(gercek_gateway):
    sonuc = await run_pipeline(
        _boru(PARALEL), "http://gw.test", http_client=gercek_gateway, poll_interval=0.01
    )
    assert set(sonuc) == {"a", "b", "c"}
    assert all(r.result for r in sonuc.values())


@pytest.mark.asyncio
async def test_saglayici_hatasi_gercek_hata_yolundan_geliyor(gercek_gateway):
    """The failure has to arrive the way the gateway actually reports it."""
    with pytest.raises(PipelineRunError) as ex:
        await run_pipeline(
            _boru(KIRIK), "http://gw.test", http_client=gercek_gateway, poll_interval=0.01
        )
    assert "saglayici coktu" in str(ex.value) or "kirik" in str(ex.value)


@pytest.mark.asyncio
async def test_gercekten_beklenen_bir_is_de_tamamlaniyor(gercek_gateway):
    """`yavas` finishes after a real delay, so polling is exercised rather than
    answered on the first request the way the fake answers it."""
    boru = _boru("""
name: yavas
steps:
  - name: a
    capability: yavas
    params: {prompt: sabir}
""")
    sonuc = await run_pipeline(
        boru, "http://gw.test", http_client=gercek_gateway, poll_interval=0.01, timeout=20
    )
    assert sonuc["a"].result is not None


@pytest.mark.asyncio
async def test_olmayan_yetenek_gercek_gateway_tarafindan_reddediliyor(gercek_gateway):
    boru = _boru("""
name: yok
steps:
  - name: a
    capability: boyle-bir-sey-yok
    params: {}
""")
    with pytest.raises(PipelineRunError):
        await run_pipeline(
            boru, "http://gw.test", http_client=gercek_gateway, poll_interval=0.01
        )


# --------------------------------------------------------------------------
# the contract itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_zarfi_sahtenin_varsaydigi_gibi(gercek_gateway):
    """The two fields the fake hardcodes, read off the real gateway.

    If the gateway ever renames either of these, this fails here - in the
    consumer - which is the only place the rename actually hurts.
    """
    yanit = await gercek_gateway.post("/v1/generate", json={"prompt": "x"})
    assert yanit.status_code == 202, yanit.text
    govde = yanit.json()
    assert "id" in govde, govde
    assert "polling_url" in govde, govde


@pytest.mark.asyncio
async def test_poll_yaniti_status_alanini_tasiyor(gercek_gateway):
    """Deliberately the slow provider: with `echo` the job is already ready on
    the first poll, and the whole not-yet-finished branch goes unexercised."""
    yanit = await gercek_gateway.post("/v1/yavas", json={"prompt": "x"})
    polling_url = yanit.json()["polling_url"]
    gorulen = set()
    for _ in range(400):
        await asyncio.sleep(0.01)   # saglayici arka planda kosuyor; sira ver
        p = await gercek_gateway.get(polling_url)
        assert p.status_code == 200, p.text
        durum = p.json().get("status")
        assert durum in {"pending", "processing", "ready", "error", "expired"}, p.json()
        gorulen.add(durum)
        if durum == "ready":
            assert "result" in p.json()
            break
    else:
        pytest.fail("is hicbir zaman hazir olmadi")

    # Bu testin asil bulgusu: gercek gateway "pending" yaziyor ve
    # tests/test_runner.py'deki sahte gateway bu durumu hic uretmiyor.
    # gateway_poll.py onu dogru isliyor (terminal degil, beklemeye devam),
    # ama o dal yalnizca burada kosuluyor.
    assert "pending" in gorulen or "processing" in gorulen, gorulen
