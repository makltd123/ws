"""PowerShell-хук: установка в профиль не должна портить чужие строки и должна быть обратимой."""

import pytest

from ws import hook
from ws.errors import UserError

USER_PROFILE = "Set-Alias ll Get-ChildItem\nfunction hi { 'привет' }\n"


def test_snippet_is_ascii_and_has_markers():
    assert hook.SNIPPET.isascii()
    assert hook.SNIPPET.startswith(hook.START) and hook.SNIPPET.rstrip().endswith(hook.END)
    assert "WS_SPACE" in hook.SNIPPET and "$PID.json" in hook.SNIPPET


def test_add_keeps_user_content_and_is_idempotent():
    once = hook.add_block(USER_PROFILE)
    assert once.startswith(USER_PROFILE.rstrip()) and hook.has_block(once)
    assert hook.add_block(once) == once  # повторная установка не плодит блоки
    assert once.count(hook.START) == 1


def test_remove_restores_the_profile():
    added = hook.add_block(USER_PROFILE)
    assert hook.remove_block(added) == USER_PROFILE
    assert hook.remove_block(USER_PROFILE) == USER_PROFILE
    assert hook.remove_block(hook.add_block("")) == ""


def test_block_in_the_middle_is_removed_cleanly():
    text = "# before\n\n" + hook.SNIPPET + "\n# after\n"
    assert hook.remove_block(text) == "# before\n\n# after\n"


def test_install_creates_missing_profile_and_uninstall_removes_the_block(tmp_path):
    profile = tmp_path / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1"
    assert hook.install(profile) is None  # раньше профиля не было: копировать нечего
    assert hook.has_block(profile.read_text(encoding="utf-8-sig"))
    assert hook.uninstall(profile) is True
    assert profile.read_text(encoding="utf-8-sig") == ""
    assert hook.uninstall(profile) is False


def test_install_makes_a_backup_of_the_original_once(tmp_path):
    profile = tmp_path / "profile.ps1"
    profile.write_bytes(USER_PROFILE.encode("utf-8"))
    backup = hook.install(profile)
    assert backup.read_bytes() == USER_PROFILE.encode("utf-8")
    hook.install(profile)  # вторая установка не затирает исходную копию профиля с блоком
    assert backup.read_bytes() == USER_PROFILE.encode("utf-8")
    assert hook.uninstall(profile)
    assert profile.read_text(encoding="utf-8") == USER_PROFILE


def test_bom_is_preserved(tmp_path):
    profile = tmp_path / "profile.ps1"
    profile.write_bytes(b"\xef\xbb\xbf" + USER_PROFILE.encode("utf-8"))
    hook.install(profile)
    assert profile.read_bytes().startswith(b"\xef\xbb\xbf")
    hook.uninstall(profile)
    assert profile.read_bytes() == b"\xef\xbb\xbf" + USER_PROFILE.encode("utf-8")


def test_profile_in_another_encoding_is_not_touched(tmp_path):
    profile = tmp_path / "profile.ps1"
    original = "Write-Host 'привет'\n".encode("cp1251")
    profile.write_bytes(original)
    with pytest.raises(UserError, match="UTF-8"):
        hook.install(profile)
    assert profile.read_bytes() == original


def test_unknown_shell_is_rejected():
    with pytest.raises(UserError, match="powershell"):
        hook.profile_path("bash")
