from jarvis_core.app_adapters import ADAPTERS, find_adapter


def test_required_adapters_registered():
    assert set(ADAPTERS) == {
        "calculator", "notepad", "browser", "file explorer", "paint",
        "youtube", "google", "notes",
    }


def test_alias_resolves_adapter():
    assert find_adapter("super calculator").app_name == "calculator"


def test_unknown_app_has_no_deep_adapter():
    assert find_adapter("unknown editor") is None
