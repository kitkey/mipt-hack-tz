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
