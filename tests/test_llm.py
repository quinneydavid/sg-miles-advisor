import io
import json
import urllib.error

from cards import llm


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_429_waits_and_tries_again(monkeypatch):
    calls, naps = [], []
    monkeypatch.setattr(llm, "_gap", 0.0)
    monkeypatch.setattr(llm.time, "sleep", naps.append)

    def fake(req, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError("u", 429, "Too Many Requests", {"Retry-After": "7"} if len(calls) == 1 else {}, None)
        return _Resp(json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode())

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake)
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_URL", "http://model.example/v1/chat/completions")
    assert llm.chat("hi") == "ok"
    assert len(calls) == 3 and naps[0] == 7.0 and naps[1] == 40.0      # Retry-After honoured, then a longer wait


def test_other_errors_are_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "_gap", 0.0)

    def fake(req, timeout):
        calls.append(1)
        raise urllib.error.HTTPError("u", 500, "boom", {}, None)

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake)
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_URL", "http://model.example/v1/chat/completions")
    assert llm.verify_card_image(b"x", "Card", "Bank") is None and len(calls) == 1
