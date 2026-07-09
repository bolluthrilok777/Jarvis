"""Test 1: All jarvis_core module imports succeed without error."""


def test_all_jarvis_core_imports():
    """Verify every jarvis_core submodule can be imported without ImportError or AttributeError."""
    import jarvis_core.config
    import jarvis_core.models
    import jarvis_core.logging_config
    import jarvis_core.ollama_service
    import jarvis_core.system_tools
    import jarvis_core.file_tools
    import jarvis_core.app_tools
    import jarvis_core.command_dispatcher
    import jarvis_core.routes

    # Verify key attributes exist
    assert hasattr(jarvis_core.config, "APP_NAME")
    assert hasattr(jarvis_core.config, "LIGHT_MODEL")
    assert hasattr(jarvis_core.config, "HEAVY_MODEL")
    assert hasattr(jarvis_core.config, "ALLOWED_APPS")
    assert hasattr(jarvis_core.config, "ALLOWED_ROOTS")
    assert hasattr(jarvis_core.models, "ChatRequest")
    assert hasattr(jarvis_core.models, "CommandRequest")
    assert hasattr(jarvis_core.models, "ConfirmRequest")
    assert hasattr(jarvis_core.ollama_service, "ask_ollama")
    assert hasattr(jarvis_core.ollama_service, "select_model")
    assert hasattr(jarvis_core.system_tools, "get_system_status")
    assert hasattr(jarvis_core.system_tools, "take_screenshot")
    assert hasattr(jarvis_core.file_tools, "read_text_file")
    assert hasattr(jarvis_core.app_tools, "open_known_app")
    assert hasattr(jarvis_core.app_tools, "google_search")
    assert hasattr(jarvis_core.command_dispatcher, "run_agent_command")
    assert hasattr(jarvis_core.command_dispatcher, "confirm_action")
    assert hasattr(jarvis_core.routes, "router")
