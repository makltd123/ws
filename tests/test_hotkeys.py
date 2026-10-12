import pytest

from ws import hotkeys
from ws.config import Paths, load_config
from ws.errors import UserError
from ws.hotkeys import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, assign, parse_hotkey


def test_parse_digit_and_letters():
    hk = parse_hotkey("Ctrl+Alt+1")
    assert (hk.modifiers, hk.vk, hk.label) == (MOD_CONTROL | MOD_ALT, ord("1"), "Ctrl+Alt+1")
    assert parse_hotkey("ctrl+shift+k").vk == ord("K")
    assert parse_hotkey("win+alt+x").modifiers == MOD_WIN | MOD_ALT


def test_parse_function_and_named_keys():
    assert parse_hotkey("ctrl+alt+f12").vk == 0x7B and parse_hotkey("ctrl+alt+f12").label == "Ctrl+Alt+F12"
    assert parse_hotkey("ctrl+shift+space").vk == 0x20
    assert parse_hotkey("ctrl+alt+left").label == "Ctrl+Alt+Left"


def test_modifier_order_is_normalized_in_label():
    assert parse_hotkey("alt+ctrl+shift+q").label == "Ctrl+Alt+Shift+Q"
    assert parse_hotkey("control+alt+q").modifiers == MOD_CONTROL | MOD_ALT


@pytest.mark.parametrize(
    "text",
    ["1", "ctrl", "ctrl+", "ctrl+alt+", "hyper+1", "ctrl+alt+ctrl", "ctrl+alt+нет", "ctrl+f25", "", "ctrl+alt+ab"],
)
def test_bad_combinations_are_rejected(text):
    with pytest.raises(UserError):
        parse_hotkey(text)


def test_a_key_without_modifier_is_rejected_so_typing_is_never_hijacked():
    with pytest.raises(UserError, match="модификатор"):
        parse_hotkey("a")


def test_default_bindings_are_ctrl_alt_1_to_9_for_first_nine_spaces_alphabetically():
    names = [f"s{i:02}" for i in range(12)][::-1]
    result = assign(names, None)
    assert [b.space for b in result.items] == [f"s{i:02}" for i in range(9)]
    assert [b.hotkey.label for b in result.items] == [f"Ctrl+Alt+{i}" for i in range(1, 10)]
    assert result.label_for("s11") is None and result.warnings == []


def test_explicit_config_replaces_defaults():
    result = assign(["api", "docs", "notes"], {"docs": "ctrl+alt+f1", "api": "ctrl+shift+9"})
    assert [(b.space, b.hotkey.label) for b in result.items] == [("docs", "Ctrl+Alt+F1"), ("api", "Ctrl+Shift+9")]
    assert result.label_for("notes") is None


def test_empty_explicit_config_disables_hotkeys():
    assert assign(["api"], {}).items == []


def test_problems_become_warnings_not_errors():
    result = assign(["api", "docs"], {"ghost": "ctrl+alt+1", "api": "bogus", "docs": "ctrl+alt+2"})
    assert [b.space for b in result.items] == ["docs"]
    joined = " | ".join(result.warnings)
    assert "ghost" in joined and "bogus" in joined


def test_duplicate_combination_goes_to_the_first_space():
    result = assign(["a", "b"], {"a": "ctrl+alt+1", "b": "Alt+Ctrl+1"})
    assert [b.space for b in result.items] == ["a"] and "уже назначена" in result.warnings[0]


def test_config_distinguishes_missing_from_empty_hotkeys(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    assert load_config(paths).hotkeys is None
    paths.config_file.write_text("freeze:\n  exclude: [Zoom.exe]\n", encoding="utf-8")
    assert load_config(paths).hotkeys is None
    paths.config_file.write_text("hotkeys: {}\n", encoding="utf-8")
    assert load_config(paths).hotkeys == {}
    paths.config_file.write_text("hotkeys:\n  api: ctrl+alt+5\n", encoding="utf-8")
    assert load_config(paths).hotkeys == {"api": "ctrl+alt+5"}
    paths.config_file.write_text("hotkeys: [1, 2]\n", encoding="utf-8")
    with pytest.raises(UserError, match="hotkeys"):
        load_config(paths)


def test_module_exports_are_stable():
    assert hotkeys.DEFAULT_COUNT == 9
