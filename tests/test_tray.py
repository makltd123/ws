"""TrayController на фейках: меню, хоткеи, ошибки. Трей и хоткей делают то же, что ws switch."""

from dataclasses import replace

import pytest
from fakes import make_runtime

from ws import freezer
from ws import state as state_mod
from ws.models import SpaceState, State, WindowRef
from ws.tray import TrayController


def _write(rt, name, folder):
    (rt.paths.spaces / f"{name}.yml").write_text(
        f"name: {name}\nitems:\n  - type: explorer\n    path: '{folder}'\n", encoding="utf-8"
    )


class Harness:
    def __init__(self, tmp_path, names=("api", "docs", "notes")):
        self.rt, self.win, self.procs, self.clock = make_runtime(tmp_path)
        self.notes: list[tuple[str, str]] = []
        self.changes = 0
        self.folder = tmp_path
        for n in names:
            _write(self.rt, n, tmp_path)
        self.procs.add(700, "explorer.exe")
        self.procs.on_spawn = lambda argv: self.win.appear_later(
            0.3, self.win.make_window("explorer.exe", "CabinetWClass", 700)
        )
        self.controller = TrayController(
            self.rt, lambda t, m: self.notes.append((t, m)), lambda fn: fn(), on_change=self._changed
        )
        self.controller.refresh_bindings()

    def _changed(self):
        self.changes += 1

    def run(self, name, exe="app.exe", pid=10):
        """Пространство уже запущено: окно и процесс."""
        self.procs.add(pid, exe)
        info = self.win.add(replace(self.win.make_window(exe, "Chrome_WidgetWin_1", pid), title=exe))
        state = state_mod.load(self.rt.paths)
        state.spaces.setdefault(name, SpaceState()).windows.append(
            WindowRef(info.hwnd, pid, self.procs.procs[pid].created, exe, exe, 0)
        )
        state_mod.save(self.rt.paths, state)
        return info


def test_menu_lists_spaces_with_hotkeys_and_marks_the_active_one(tmp_path):
    h = Harness(tmp_path)
    h.run("docs")
    state = state_mod.load(h.rt.paths)
    state.active = "docs"
    state_mod.save(h.rt.paths, state)

    entries = {e.name: e for e in h.controller.entries()}

    assert list(entries) == ["api", "docs", "notes"]
    assert entries["api"].label == "api    Ctrl+Alt+1" and not entries["api"].checked
    assert entries["docs"].checked and entries["docs"].label.startswith("docs    Ctrl+Alt+2")
    assert entries["notes"].label == "notes    Ctrl+Alt+3"


def test_menu_shows_frozen_spaces(tmp_path):
    h = Harness(tmp_path)
    h.run("docs")
    state = state_mod.load(h.rt.paths)
    freezer.freeze(h.rt, "docs", state)
    assert "(заморожено)" in {e.name: e for e in h.controller.entries()}["docs"].label


def test_hotkey_does_the_same_as_ws_switch(tmp_path):
    """Приёмка этапа 4: переключение по хоткею = ws switch."""
    h = Harness(tmp_path)
    a = h.run("api", pid=10)
    state = state_mod.load(h.rt.paths)
    state.active = "api"
    state_mod.save(h.rt.paths, state)

    h.controller.on_hotkey(2)  # Ctrl+Alt+2 = docs

    after = state_mod.load(h.rt.paths)
    assert after.active == "docs"
    assert after.spaces["api"].frozen and h.procs.suspended == {10}
    assert not h.win.windows[a.hwnd].visible
    assert len(after.spaces["docs"].windows) == 1  # docs запущено
    assert h.changes == 1 and h.notes == []


def test_menu_items_are_wired_to_the_controller(tmp_path):
    """Меню собирается настоящим pystray.Menu; клик по пункту запускает то же переключение."""
    import pystray

    from ws.tray import build_menu_items

    h = Harness(tmp_path)
    h.run("api")
    state = state_mod.load(h.rt.paths)
    state.active = "api"
    state_mod.save(h.rt.paths, state)
    quits = []
    refreshes = []
    menu = pystray.Menu(lambda: build_menu_items(h.controller, lambda: refreshes.append(1), lambda: quits.append(1)))

    items = list(menu.items)
    texts = [str(i.text) for i in items]
    assert any(t.startswith("api") for t in texts) and "Заморозить активное" in texts and "Выход" in texts
    api, docs = items[0], items[1]
    assert api.checked and not docs.checked

    docs(None)  # клик по «docs» в меню
    assert state_mod.load(h.rt.paths).active == "docs"
    next(i for i in items if i.text == "Обновить")(None)
    next(i for i in items if i.text == "Выход")(None)
    assert refreshes == [1] and quits == [1]


def test_menu_without_spaces_shows_a_hint(tmp_path):
    import pystray

    from ws.tray import build_menu_items

    h = Harness(tmp_path, names=())
    items = list(pystray.Menu(lambda: build_menu_items(h.controller, lambda: None, lambda: None)).items)
    assert "Нет пространств" in str(items[0].text) and not items[0].enabled


def test_unknown_hotkey_id_does_nothing(tmp_path):
    h = Harness(tmp_path)
    h.controller.on_hotkey(0)
    h.controller.on_hotkey(99)
    assert h.procs.spawned == [] and h.notes == [] and h.changes == 0


def test_user_error_becomes_a_notification_not_a_crash(tmp_path):
    h = Harness(tmp_path)
    h.controller.switch("ghost")
    assert h.notes and h.notes[0][0] == "ws: ошибка" and "ghost" in h.notes[0][1]
    assert h.changes == 1  # меню всё равно обновляется


def test_unexpected_error_is_logged_and_notified(tmp_path, monkeypatch):
    h = Harness(tmp_path)

    def boom(rt, name):
        raise RuntimeError("что-то сломалось")

    monkeypatch.setattr("ws.switcher.switch_named", boom)
    h.controller.switch("docs")
    assert h.notes[0][0] == "ws: системная ошибка" and "RuntimeError" in h.notes[0][1]


def test_warnings_from_switch_are_shown(tmp_path):
    h = Harness(tmp_path)
    h.run("api", pid=10)
    state = state_mod.load(h.rt.paths)
    state.active = "api"
    state_mod.save(h.rt.paths, state)
    h.procs.suspend_refuses.add(10)
    h.controller.switch("docs")
    assert h.notes and h.notes[0][0] == "ws: docs" and "не удалось заморозить" in h.notes[0][1]


def test_freeze_active_from_the_menu(tmp_path):
    h = Harness(tmp_path)
    h.run("api")
    state = state_mod.load(h.rt.paths)
    state.active = "api"
    state_mod.save(h.rt.paths, state)

    h.controller.freeze_active()

    after = state_mod.load(h.rt.paths)
    assert after.spaces["api"].frozen and after.active is None and h.procs.suspended == {10}


def test_freeze_active_without_active_space_explains_itself(tmp_path):
    h = Harness(tmp_path)
    h.controller.freeze_active()
    assert "Нет активного пространства" in h.notes[0][1]


def test_tooltip_follows_the_active_space(tmp_path):
    h = Harness(tmp_path)
    assert h.controller.tooltip() == "ws: нет активного пространства"
    h.run("docs")
    state = state_mod.load(h.rt.paths)
    state.active = "docs"
    state_mod.save(h.rt.paths, state)
    assert h.controller.tooltip() == "ws: docs"


def test_bindings_warnings_are_notified_once(tmp_path):
    h = Harness(tmp_path)
    h.rt.config = replace(h.rt.config, hotkeys={"api": "bogus", "docs": "ctrl+alt+7"})
    h.controller.refresh_bindings()
    h.controller.refresh_bindings()
    assert len(h.notes) == 1 and "bogus" in h.notes[0][1]
    ids = h.controller.refresh_bindings()
    assert [(i, hk.label) for i, hk in ids] == [(1, "Ctrl+Alt+7")]
    h.controller.on_hotkey(1)  # id 1 теперь docs
    assert state_mod.load(h.rt.paths).active == "docs"


def test_new_manifest_gets_a_hotkey_after_refresh(tmp_path):
    h = Harness(tmp_path, names=("api",))
    assert h.controller.bindings.label_for("zeta") is None
    _write(h.rt, "zeta", h.folder)
    h.controller.refresh_bindings()
    assert h.controller.bindings.label_for("zeta") == "Ctrl+Alt+2"
