"""Router streaming: chunks must reassemble into the answer exactly once, and
every chunk must say which model produced it."""

import json

from central import app as central_app


def _texts(events, key="content"):
    out = []
    for e in events:
        body = e[len("data: "):].strip()
        if body == "[DONE]":
            continue
        obj = json.loads(body)
        delta = obj["choices"][0].get("delta", {})
        if key in delta:
            out.append(delta[key])
    return out


def test_replay_chunks_reassemble_into_the_original_text():
    text = "abcdefghij" * 25  # 250 chars, several chunks
    events = list(central_app._replay_chunks(text, 0, "pool → m (weak)"))
    assert "".join(_texts(events)) == text


def test_replay_chunks_are_slices_not_growing_prefixes():
    """The old bug: each chunk was text[:i+80], so a client appending deltas
    rendered the answer over and over."""
    text = "x" * 200
    pieces = _texts(list(central_app._replay_chunks(text, 0, "label")))
    assert len(pieces) == 3
    assert all(len(p) <= central_app.CHUNK_SIZE for p in pieces)
    assert sum(len(p) for p in pieces) == len(text)


def test_replay_of_a_short_answer_is_a_single_chunk():
    assert _texts(list(central_app._replay_chunks("hi", 0, "label"))) == ["hi"]


def test_replay_of_an_empty_answer_emits_nothing():
    assert list(central_app._replay_chunks("", 0, "label")) == []


def test_reasoning_is_replayed_on_its_own_field():
    events = list(central_app._replay_chunks("thinking", 0, "label", reasoning=True))
    assert _texts(events, "reasoning_content") == ["thinking"]
    assert _texts(events, "content") == []


def test_every_chunk_names_the_model_that_answered():
    label = "my-pool → Qwen/Qwen3-8B (classifier)"
    for e in central_app._replay_chunks("some answer", 0, label):
        assert json.loads(e[6:])["model"] == label


def test_served_label_shows_pool_model_and_decision():
    class _Pool:
        name = "my-pool"

    label = central_app._served_label(_Pool(), "Qwen/Qwen3-8B", "escalated")
    assert "my-pool" in label and "Qwen/Qwen3-8B" in label and "escalated" in label


# ---- live forwarding from the chosen deploy ----

def test_upstream_chunks_are_relabelled_with_the_served_model():
    raw = b'data: {"choices":[{"delta":{"content":"hi"}}],"model":"raw-name"}\n\n'
    out = central_app._relabel_upstream(raw, "pool → real (stage)", "sess-1")
    obj = json.loads(out.decode()[6:])
    assert obj["model"] == "pool → real (stage)"
    assert obj["session_id"] == "sess-1"
    assert obj["choices"][0]["delta"]["content"] == "hi"


def test_done_is_signalled_to_the_caller():
    assert central_app._relabel_upstream(b"data: [DONE]\n\n", "l", "s") is None


def test_non_json_upstream_lines_pass_through_untouched():
    raw = b": keep-alive comment\n"
    assert central_app._relabel_upstream(raw, "l", "s") == raw


def test_malformed_json_is_not_dropped():
    raw = b"data: {not json}\n\n"
    assert central_app._relabel_upstream(raw, "l", "s") == raw
