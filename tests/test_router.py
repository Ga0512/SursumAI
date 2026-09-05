"""Routing decisions.

The models are faked: what is under test is who gets picked and why, not what
a model would answer.
"""

import pytest

from central import router
from central.db import Pool, RouterSession, Store
from core.spec import Spec


class FakeDeploy:
    def __init__(self, id, model, endpoint="http://localhost:9000/v1"):
        self.id = id
        self.spec = Spec(model=model, api_key=f"sk-{id}")
        self.endpoint = endpoint


class FakeStore:
    """Just enough Store for the router: deploys, pool members, sessions."""

    def __init__(self, deploys, members=None):
        self._deploys = {d.id: d for d in deploys}
        self._members = members if members is not None else [d.id for d in deploys]
        self.saved_sessions = []

    def get(self, deploy_id):
        return self._deploys.get(deploy_id)

    def get_pool_models(self, pool_id):
        return list(self._members)

    def upsert_router_session(self, session):
        self.saved_sessions.append((session.streak, session.latched))

    def apply_judge_verdict(self, *a, **kw):
        pass


def _pool(mode="escalation", ids=("a", "b")):
    return Pool(user_id="u", name="p", weak_id=ids[0], strong_id=ids[-1], mode=mode)


def _session():
    return RouterSession(id="s", pool_id="p", user_id="u")


def _reply(text, model="m"):
    return {"choices": [{"message": {"content": text}}],
            "usage": {"total_tokens": 3}}


@pytest.fixture
def three_models():
    return [FakeDeploy("a", "small"), FakeDeploy("b", "medium"), FakeDeploy("c", "large")]


@pytest.fixture
def chat_calls(monkeypatch):
    """Record which endpoint every completion went to."""
    calls = []

    def _chat(endpoint, payload, timeout=180.0, api_key=None):
        calls.append({"endpoint": endpoint, "model": payload.get("model"),
                      "api_key": api_key})
        return _reply("answer")

    monkeypatch.setattr(router.agent_client, "chat", _chat)
    return calls


# ---- the ladder: pools of N ----

def test_the_ladder_is_the_pool_order(three_models):
    store = FakeStore(three_models)
    assert [d.id for d in router._ladder(store, _pool())] == ["a", "b", "c"]


def test_weak_and_strong_are_the_ends_of_the_ladder(three_models):
    """With 3+ models the strong one is the LAST, not the second."""
    weak, strong = router._weak_strong(FakeStore(three_models), _pool())
    assert weak.id == "a" and strong.id == "c"


def test_models_without_an_endpoint_are_skipped(three_models):
    three_models[1].endpoint = None
    assert [d.id for d in router._ladder(FakeStore(three_models), _pool())] == ["a", "c"]


def test_a_pool_with_fewer_than_two_live_models_says_so(three_models):
    for d in three_models[1:]:
        d.endpoint = None
    with pytest.raises(ValueError, match="at least 2 running models"):
        router._ladder(FakeStore(three_models), _pool())


def test_a_legacy_pool_without_members_falls_back_to_weak_strong(three_models):
    store = FakeStore(three_models, members=[])
    assert [d.id for d in router._ladder(store, _pool(ids=("a", "c")))] == ["a", "c"]


# ---- round robin over N ----

def test_round_robin_visits_every_model_in_order(three_models, chat_calls):
    store, pool, session = FakeStore(three_models), _pool("round_robin"), _session()
    served = []
    for _ in range(6):
        served.append(router.route_round_robin(store, pool, session, "u", [], 10, None)["served"])
    assert served == ["a", "b", "c", "a", "b", "c"]


def test_round_robin_decision_says_which_of_how_many(three_models, chat_calls):
    out = router.route_round_robin(FakeStore(three_models), _pool("round_robin"),
                                   _session(), "u", [], 10, None)
    assert out["decision"] == "round_robin[1/3]"


# ---- stage ----

@pytest.mark.parametrize("text", [
    "explain why this integral diverges",
    "debug this python traceback",
    "me explique a matemática disso",
    "prove o teorema",
    "write an essay outline",
])
def test_stage_escalates_technical_turns_in_both_languages(text):
    assert router._stage_wants_strong([{"role": "user", "content": text}])


@pytest.mark.parametrize("text", ["oi tudo bem", "hi there", "thanks!", ""])
def test_stage_keeps_small_talk_on_the_cheap_model(text):
    assert not router._stage_wants_strong([{"role": "user", "content": text}])


def test_stage_reads_the_last_user_turn_only():
    messages = [{"role": "user", "content": "explain calculus"},
                {"role": "assistant", "content": "..."},
                {"role": "user", "content": "thanks"}]
    assert not router._stage_wants_strong(messages)


def test_stage_sends_technical_work_to_the_strongest_model(three_models, chat_calls):
    out = router.route_stage(FakeStore(three_models), _pool("stage"), _session(), "u",
                             [{"role": "user", "content": "prove this theorem"}], 10, None)
    assert out["served"] == "c"


def test_stage_sends_chitchat_to_the_cheapest_model(three_models, chat_calls):
    out = router.route_stage(FakeStore(three_models), _pool("stage"), _session(), "u",
                             [{"role": "user", "content": "oi"}], 10, None)
    assert out["served"] == "a"


# ---- escalation ----

def test_a_good_weak_answer_is_served_and_resets_the_streak(three_models, monkeypatch,
                                                            chat_calls):
    monkeypatch.setattr(router, "_judge", lambda *a: False)
    session = _session()
    session.streak = 1
    out = router.route_escalation(FakeStore(three_models), _pool(), session, "u", [], 10, None)
    assert out["served"] == "a" and out["decision"] == "weak_ok"
    assert session.streak == 0


def test_two_judged_failures_latch_to_the_strongest(three_models, monkeypatch, chat_calls):
    monkeypatch.setattr(router, "_judge", lambda *a: True)
    store, pool, session = FakeStore(three_models), _pool(), _session()

    first = router.route_escalation(store, pool, session, "u", [], 10, None)
    assert first["served"] == "a" and session.streak == 1 and not session.latched

    second = router.route_escalation(store, pool, session, "u", [], 10, None)
    assert second["served"] == "c" and second["decision"] == "escalated"
    assert session.latched


def test_a_latched_session_goes_straight_to_the_strong_model(three_models, chat_calls):
    session = _session()
    session.latched = True
    out = router.route_escalation(FakeStore(three_models), _pool(), session, "u", [], 10, None)
    assert out["served"] == "c" and out["decision"] == "latched"
    assert len(chat_calls) == 1  # the weak model was never asked


def test_an_empty_weak_answer_escalates_without_asking_the_judge(three_models, monkeypatch):
    def _chat(endpoint, payload, timeout=180.0, api_key=None):
        return _reply("")

    monkeypatch.setattr(router.agent_client, "chat", _chat)
    judged = []
    monkeypatch.setattr(router, "_judge", lambda *a: judged.append(1) or False)

    session = _session()
    out = router.route_escalation(FakeStore(three_models), _pool(), session, "u", [], 10, None)
    assert out["decision"] == "weak_bad" and not judged


# ---- what the caller is told ----

def test_every_outcome_names_the_model_that_answered(three_models, chat_calls, monkeypatch):
    monkeypatch.setattr(router, "_judge", lambda *a: False)
    store, session = FakeStore(three_models), _session()
    for mode in ("escalation", "stage", "round_robin"):
        out = router.route_turn(store, _pool(mode), session, "u",
                                [{"role": "user", "content": "oi"}], 10, None)
        assert out["served_model"] in ("small", "medium", "large")
        assert out["served"] in ("a", "b", "c")


def test_each_deploy_is_called_with_its_own_api_key(three_models, chat_calls):
    session = _session()
    session.latched = True
    router.route_escalation(FakeStore(three_models), _pool(), session, "u", [], 10, None)
    assert chat_calls[0]["api_key"] == "sk-c"


# ---- pick_target: who can decide without generating ----

def test_streamable_modes_decide_up_front(three_models, chat_calls):
    store = FakeStore(three_models)
    for mode in ("stage", "round_robin"):
        target = router.pick_target(store, _pool(mode), _session(),
                                    [{"role": "user", "content": "oi"}])
        assert target is not None
        assert not chat_calls  # nothing was generated to make the decision


def test_escalation_cannot_decide_without_reading_a_reply(three_models):
    assert router.pick_target(FakeStore(three_models), _pool("escalation"),
                              _session(), []) is None


def test_a_latched_session_is_streamable_even_in_escalation(three_models):
    session = _session()
    session.latched = True
    target = router.pick_target(FakeStore(three_models), _pool("escalation"), session, [])
    assert target is not None and target[0].id == "c" and target[1] == "latched"


def test_an_unknown_mode_falls_back_to_escalation(three_models):
    assert router.pick_target(FakeStore(three_models), _pool("nonsense"),
                              _session(), []) is None
