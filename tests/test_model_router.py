from jarvis_core.model_router import ModelRouter


def test_direct_tool_is_never_sent_to_ollama():
    decision = ModelRouter().decide("open calculator", direct_available=True, purpose="task")
    assert decision.execution == "direct"
    assert decision.model is None
    assert decision.label == "DIRECT EXECUTION · AI BYPASSED"


def test_auto_uses_small_coder_for_simple_code():
    decision = ModelRouter().decide("fix this python syntax", purpose="chat")
    assert decision.model == "qwen2.5-coder:3b"


def test_auto_uses_large_coder_for_complex_code():
    decision = ModelRouter().decide("review and refactor this API architecture", purpose="chat")
    assert decision.model == "qwen2.5-coder:7b"


def test_quality_is_only_auto_escalation_after_validation_failure():
    router = ModelRouter()
    normal = router.decide("write a detailed project plan", purpose="chat")
    escalated = router.decide("write a detailed project plan", purpose="chat", validation_failed=True)
    assert normal.model == "qwen2.5:7b"
    assert escalated.model == "gemma4:12b"
    assert escalated.escalated is True


def test_deep_is_manual_only():
    router = ModelRouter()
    auto = router.decide("very detailed analysis " * 100, purpose="chat")
    manual = router.decide("hello", requested_mode="deep", purpose="chat")
    assert auto.model != "qwen3.6:latest"
    assert manual.model == "qwen3.6:latest"
