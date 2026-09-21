"""LLM router and fact guard, tested with injected fake clients (no network, no keys)."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import explain
from agent.llm import LLMRouter, LLMUnavailable, Provider


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"http {status}")
        self.status_code = status


def resp(text="ok", pt=10, ct=5, tool_calls=None):
    msg = SimpleNamespace(content=text, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=SimpleNamespace(prompt_tokens=pt, completion_tokens=ct))


class FakeClient:
    """Plays a script: each item is a response or an exception to raise, in order."""

    def __init__(self, script):
        self.script, self.kwargs = list(script), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.embeddings = SimpleNamespace(create=self._embed)

    def _create(self, **kw):
        self.kwargs.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def _embed(self, model, input):
        self.kwargs.append({"model": model, "input": input})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(data=[SimpleNamespace(embedding=[float(len(t))]) for t in input])


def router(tmp_path, clients, providers=("groq", "gemini"), sleeps=None, **kw):
    provs = [Provider(n, f"http://{n}", "k", {"loop": f"{n}-m", "synth": f"{n}-m", "embed": f"{n}-e"}) for n in providers]
    return LLMRouter(provs, cache_dir=tmp_path / "cache", client_factory=lambda p: clients[p.name],
                     sleep=(sleeps.append if sleeps is not None else (lambda s: None)), **kw)


MSG = [{"role": "user", "content": "hi"}]


def test_no_keys_means_unavailable(tmp_path):
    r = LLMRouter([], cache_dir=tmp_path)
    assert not r.available
    with pytest.raises(LLMUnavailable):
        r.chat(MSG)


def test_role_order_groq_for_the_loop_and_gemini_for_synthesis(tmp_path):
    g, m = FakeClient([resp("from groq")]), FakeClient([resp("from gemini")])
    assert router(tmp_path, {"groq": g, "gemini": m}).chat(MSG, role="loop").provider == "groq"
    g, m = FakeClient([resp("from groq")]), FakeClient([resp("from gemini")])
    assert router(tmp_path / "b", {"groq": g, "gemini": m}).chat(MSG, role="synth").provider == "gemini"


def test_rate_limit_is_retried_with_backoff_then_succeeds(tmp_path):
    g = FakeClient([HttpError(429), HttpError(503), resp("finally")])
    sleeps = []
    out = router(tmp_path, {"groq": g}, providers=("groq",), sleeps=sleeps).chat(MSG, role="loop")
    assert out.text == "finally" and len(g.kwargs) == 3 and len(sleeps) == 2 and sleeps[1] > sleeps[0] - 0.5


def test_auth_failure_is_not_retried_and_falls_back_to_the_other_provider(tmp_path):
    g, m = FakeClient([HttpError(401)]), FakeClient([resp("gemini saved it")])
    sleeps = []
    out = router(tmp_path, {"groq": g, "gemini": m}, sleeps=sleeps).chat(MSG, role="loop")
    assert out.provider == "gemini" and len(g.kwargs) == 1 and sleeps == []


def test_exhausting_retries_falls_back_then_raises_if_everything_fails(tmp_path):
    g, m = FakeClient([HttpError(429)] * 2), FakeClient([HttpError(500)] * 2)
    r = router(tmp_path, {"groq": g, "gemini": m}, max_retries=1)
    with pytest.raises(LLMUnavailable):
        r.chat(MSG, role="loop")
    assert len(g.kwargs) == 2 and len(m.kwargs) == 2


def test_cache_makes_a_repeat_call_free_and_still_counts_tokens(tmp_path):
    g = FakeClient([resp("once", pt=100, ct=20)])
    r = router(tmp_path, {"groq": g}, providers=("groq",))
    a = r.chat(MSG, role="loop")
    b = r.chat(MSG, role="loop")                       # second call must not reach the client (script has one item)
    assert a.text == b.text == "once" and b.cached and len(g.kwargs) == 1
    assert r.tokens == 240 and r.cache_hits == 1       # reported usage includes the cached call's original tokens


def test_json_mode_and_tools_are_forwarded(tmp_path):
    g = FakeClient([resp('{"a": 1}')])
    router(tmp_path, {"groq": g}, providers=("groq",)).chat(MSG, role="loop", json_mode=True, tools=[{"type": "function"}])
    assert g.kwargs[0]["response_format"] == {"type": "json_object"} and g.kwargs[0]["tools"] == [{"type": "function"}]


def test_tool_calls_are_returned(tmp_path):
    tc = [SimpleNamespace(function=SimpleNamespace(name="device_neighbors", arguments='{"dev": "x"}'))]
    out = router(tmp_path, {"groq": FakeClient([resp("", tool_calls=tc)])}, providers=("groq",)).chat(MSG, role="loop")
    assert out.tool_calls == [{"name": "device_neighbors", "arguments": '{"dev": "x"}'}]


def test_embeddings_use_gemini_only_and_batch(tmp_path):
    m = FakeClient([None, None])
    r = router(tmp_path, {"gemini": m, "groq": FakeClient([])})
    vecs = r.embed(["aa", "bbb", "c"], batch=2)
    assert vecs == [[2.0], [3.0], [1.0]] and len(m.kwargs) == 2 and m.kwargs[0]["model"] == "gemini-e"


# ---- fact guard -----------------------------------------------------------------------------------------
ORIG = "Card C13487-K1 had 3 transactions totalling $1,906.07 on 2016-11-21; see CC-4124 and rule R9."


def test_guard_accepts_a_rewording_that_keeps_every_fact():
    ok, _ = explain.facts_preserved(ORIG, "On 2016-11-21 the card C13487-K1 made 3 purchases worth 1906.07, matching CC-4124 (R9).")
    assert ok


def test_guard_rejects_dropped_and_invented_facts():
    assert not explain.facts_preserved(ORIG, "Card C13487-K1 had 3 transactions on 2016-11-21; see CC-4124 and R9.")[0]          # amount dropped
    assert not explain.facts_preserved(ORIG, ORIG.replace("1,906.07", "2,906.07"))[0]                                          # amount changed
    assert not explain.facts_preserved(ORIG, ORIG + " Another 4 cards were affected.")[0]                                      # number invented


def test_polish_uses_the_llm_only_when_the_guard_passes(tmp_path):
    good = FakeClient([resp("On 2016-11-21, card C13487-K1 made 3 transactions totalling $1,906.07; see CC-4124 and R9.")])
    p = explain.polish(router(tmp_path, {"gemini": good}, providers=("gemini",)), ORIG, "summary")
    assert p.used_llm and "On 2016-11-21" in p.text

    bad = FakeClient([resp("Card C13487-K1 had 5 transactions totalling $1,906.07 on 2016-11-21; see CC-4124 and R9.")])
    p = explain.polish(router(tmp_path / "b", {"gemini": bad}, providers=("gemini",)), ORIG, "summary")
    assert not p.used_llm and p.text == ORIG and "fact guard rejected" in p.reason


def test_polish_falls_back_when_the_provider_is_down_or_absent(tmp_path):
    down = FakeClient([HttpError(500)] * 8)
    p = explain.polish(router(tmp_path, {"gemini": down}, providers=("gemini",), max_retries=1), ORIG, "summary")
    assert not p.used_llm and p.text == ORIG
    assert explain.polish(None, ORIG, "summary").text == ORIG


def test_polish_rejects_a_sar_that_loses_its_length(tmp_path):
    sar = " ".join(f"Sentence {i} about card C13487-K1 costing $10.00." for i in range(8))
    short = FakeClient([resp("Card C13487-K1 costing $10.00.")])
    assert not explain.polish(router(tmp_path, {"gemini": short}, providers=("gemini",)), sar, "sar").used_llm


# ---- orchestrator integration (needs the local store) --------------------------------------------------------
DB = Path(__file__).resolve().parents[1] / "data" / "store" / "graphsleuth.duckdb"
PACK = Path(__file__).resolve().parents[2] / "dataset" / "HHGOA_IEEE" / "case_pack.csv"


@pytest.mark.skipif(not (DB.exists() and PACK.exists()), reason="local DuckDB store or dataset not present")
def test_orchestrator_counts_tokens_and_keeps_the_answer_valid(tmp_path):
    import pandas as pd
    from agent.backend import LocalBackend
    from agent.orchestrator import Orchestrator
    from agent.schemas import Answer

    class Echo(FakeClient):                       # a model that returns its input unchanged: passes the fact guard
        def _create(self, **kw):
            self.kwargs.append(kw)
            return resp(kw["messages"][-1]["content"], pt=200, ct=100)

    r = router(tmp_path, {"gemini": Echo([])}, providers=("gemini",))
    b = LocalBackend("final")
    trig = pd.read_csv(PACK).set_index("case_id").loc["HHG-014"].to_dict()
    trig["case_id"] = "HHG-014"
    ans = Orchestrator(b, None, llm=r).run_case(trig, write_memory=False)
    assert ans.tokens == 600 and ans.sar.file            # summary + SAR polished: 2 calls x 300 tokens
    Answer.model_validate(ans.model_dump())
