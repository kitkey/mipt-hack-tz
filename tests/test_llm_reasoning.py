from tzcheck.llm import _reasoning_body


def test_openrouter_off_by_default(monkeypatch):
    monkeypatch.delenv("TZCHECK_LLM_REASONING", raising=False)
    assert _reasoning_body("openrouter/deepseek/deepseek-v4.1-flash-1017", False) == {"reasoning": {"enabled": False}}


def test_local_openai_off(monkeypatch):
    monkeypatch.delenv("TZCHECK_LLM_REASONING", raising=False)
    assert _reasoning_body("openai/qwen3.8-27b", True) == {"chat_template_kwargs": {"enable_thinking": False}}


def test_on_keeps_model_default(monkeypatch):
    monkeypatch.setenv("TZCHECK_LLM_REASONING", "on")
    assert _reasoning_body("openrouter/deepseek/deepseek-v4.1-flash-1017", False) == {}


def test_other_providers_untouched(monkeypatch):
    monkeypatch.delenv("TZCHECK_LLM_REASONING", raising=False)
    assert _reasoning_body("gpt-4o", False) == {}


def test_openrouter_budget(monkeypatch):
    monkeypatch.setenv("TZCHECK_LLM_REASONING", "2000")
    assert _reasoning_body("openrouter/deepseek/deepseek-v4.1-flash-1017", False) == {"reasoning": {"max_tokens": 2000}}


def test_openrouter_effort(monkeypatch):
    monkeypatch.setenv("TZCHECK_LLM_REASONING", "low")
    assert _reasoning_body("openrouter/deepseek/deepseek-v4.1-flash-1017", False) == {"reasoning": {"effort": "low"}}


def test_local_budget_keeps_thinking(monkeypatch):
    monkeypatch.setenv("TZCHECK_LLM_REASONING", "2000")
    assert _reasoning_body("openai/qwen3.8-27b", True) == {}


def test_length_retry_disables_reasoning(monkeypatch):
    import sys
    import types
    from tzcheck import llm
    calls = []

    def fake(**kw):
        calls.append(kw.get("extra_body"))
        reason = "length" if len(calls) == 1 else "stop"
        msg = types.SimpleNamespace(content='{"ok": 1}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(finish_reason=reason, message=msg)])

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(completion=fake))
    monkeypatch.setenv("TZCHECK_LLM_MODEL", "openrouter/deepseek/x")
    monkeypatch.setenv("TZCHECK_LLM_REASONING", "on")
    monkeypatch.setattr(llm, "allow_external", lambda *a, **k: True)
    assert llm._call("p") == {"ok": 1}
    assert calls == [None, {"reasoning": {"enabled": False}}]
