"""Ошибки ws. Код возврата CLI берётся из exit_code."""

from __future__ import annotations


class WsError(Exception):
    exit_code = 2


class UserError(WsError):
    """Ошибка пользователя: нет такого пространства, битый манифест."""

    exit_code = 1


class SystemFailure(WsError):
    """Системная ошибка: Windows отказала, не найдено приложение."""

    exit_code = 2
