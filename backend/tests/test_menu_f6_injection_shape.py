from app.services import incidents, menu


def test_catalog_ignores_incomplete_non_menu_injection_record(monkeypatch):
    """An injection without target cannot be the menu-store blocker; it must not crash reads."""
    key = ("test_partial_injection", "v1")
    menu._CACHE.pop(key, None)
    monkeypatch.setattr(incidents, "active_injections", lambda: [{"mode": "block"}])
    expected = {"menus": [], "by_id": {}, "alias_index": {}, "options": [],
                "store_words": [], "loaded_at": 0, "checksum": "test"}
    monkeypatch.setattr(menu, "_load", lambda *_: expected)

    try:
        assert menu.catalog(*key) == expected
    finally:
        menu._CACHE.pop(key, None)
