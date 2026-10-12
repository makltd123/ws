"""Реестр адаптеров: тип элемента -> адаптер."""

from __future__ import annotations

from ws.adapters.base import Adapter
from ws.adapters.browser import BrowserAdapter
from ws.adapters.explorer import ExplorerAdapter
from ws.adapters.generic import GenericAdapter
from ws.adapters.terminal import TerminalAdapter
from ws.adapters.vscode import VSCodeAdapter
from ws.models import Item, WindowInfo

# Порядок важен для ws save: специфичные адаптеры первыми, generic последним (owns() у него всегда True).
ADAPTERS: dict[str, Adapter] = {
    a.type_name: a
    for a in (VSCodeAdapter(), TerminalAdapter(), BrowserAdapter(), ExplorerAdapter(), GenericAdapter())
}


def adapter_for(item: Item) -> Adapter:
    return ADAPTERS[item.type]


def adapter_owning(window: WindowInfo) -> Adapter:
    return next(a for a in ADAPTERS.values() if a.owns(window))
