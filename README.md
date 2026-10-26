# ws: context workspaces for Windows

[Русская версия](README.ru.md)

`ws` describes a project as a *workspace*: which apps to open, with which folders, at which window positions. It launches a workspace from a YAML manifest, binds the new windows to it, arranges them, snapshots the current layout (`ws save`), and switches between workspaces by hiding and freezing (suspending) the processes of the one you leave. The tray, hotkeys and "where I stopped" notes (stages 4-5 in [SPEC.md](SPEC.md)) are not implemented yet.

It is **not** a replacement for Windows virtual desktops, and it never uses their internal API.

## Install

```
uv sync
uv run ws doctor
```

## Usage

```
ws list                # workspaces and their status
ws edit <name>         # open (or create from a template) the manifest in $EDITOR / notepad
ws open <name>         # launch the workspace, or bring its windows forward if already running
ws open .              # launch the .workspace.yml of the current repository (and remember it by name)
ws hook show|install|uninstall   # optional PowerShell hook for "where I stopped" notes
ws save <name>         # snapshot the currently visible windows into a new manifest (--force to overwrite, old one kept as .bak)
ws switch <name>       # freeze the active workspace (hide windows, suspend processes), open or thaw <name>
ws freeze [<name>]     # freeze the active (or the named) workspace
ws thaw <name>         # thaw without switching
ws close <name>        # ask the windows to close (WM_CLOSE), with confirmation; --force offers to terminate stubborn processes
ws tray                # tray icon with the workspace list + global hotkeys (--background to detach, --stop to quit)
ws status              # what is active now
ws doctor              # check wt, code, browsers, DPI, rights
```

Manifests live in `%APPDATA%\ws\spaces\<name>.yml`, state in `%APPDATA%\ws\state.json`, log in `%APPDATA%\ws\ws.log`. Set `WS_HOME` to use another folder (handy for experiments). See [SPEC.md](SPEC.md) for the manifest format.

Exit codes: 0 success, 1 user error (no such workspace, bad manifest), 2 system error.

## Tray and hotkeys

`ws tray` (or `ws tray --background`, stop it with `ws tray --stop`) puts an icon in the notification area. Its menu lists the workspaces (the active one is checked, frozen ones are marked) and switching from it does exactly what `ws switch` does. Warnings and errors come as notifications.

By default the first nine workspaces (alphabetically) get `Ctrl+Alt+1` ... `Ctrl+Alt+9`. To choose your own, list them in `%APPDATA%\ws\config.yml`; once the key is present only the listed ones are bound (`hotkeys: {}` disables hotkeys):

```yaml
hotkeys:
  api-project: ctrl+alt+1
  notes: ctrl+shift+f2
```

A combination needs at least one modifier (`ctrl`, `alt`, `shift`, `win`). New manifests and config changes are picked up within a few seconds, no restart needed. `ws` never adds itself to autostart; if you want that, put a shortcut to `ws tray --background` into the Startup folder yourself.

## A manifest inside a repository

Put a `.workspace.yml` in the root of a project (same format as a personal manifest, `name:` defaults to the folder name). Relative paths in it are resolved against the file's folder, so it works for everyone who clones the repo:

```yaml
name: shop
items:
  - {type: vscode, path: .}
  - type: terminal
    tabs: [{dir: ., cmd: "npm run dev", title: dev}]
```

`ws open .` (in the project or any subfolder; a path to the folder or the file also works) launches it and remembers it in `%APPDATA%\ws\repos.json`, after which `ws switch shop`, the tray and hotkeys know it by name. A personal manifest with the same name takes precedence, and `ws open .` refuses to register a clashing name.

## Where I stopped

When you leave a workspace (`ws switch`, `ws freeze`, tray, hotkey), `ws` saves a note: the title of the topmost window of that workspace and, if the hook below is installed, the last command and folder of its terminals. When you come back, `ws switch` prints it (`↩ You left 12 min ago. Last window: ... Last command: pytest -q (in C:\proj\api).`) and the tray shows it as a notification.

The last command comes from a small PowerShell hook. It is **not** installed automatically:

```
ws hook show          # prints the block and the profile file it would go to; changes nothing
ws hook install       # asks, makes a one-time backup, appends the block to your $PROFILE
ws hook uninstall     # removes the block
```

`--shell pwsh` targets PowerShell 7 instead of Windows PowerShell 5.1. The block works only in shells started by `ws` (it checks the `WS_SPACE` variable that `ws` sets), writes `%APPDATA%\ws\notes\<space>\<pid>.json` on every prompt, and does nothing elsewhere. If your execution policy is `Restricted`, PowerShell does not load profiles at all; `ws hook install` tells you, and does not change the policy itself.

## Branch triggers

```yaml
triggers:
  git_branch: "feature/*"      # a mask (* and ?), case-sensitive
  repo: .                      # optional; by default the first folder from items
```

While `ws tray` runs, a change of the git branch in that repository to a matching one produces a notification and a first line in the tray menu: `→ Go to «shop» (branch feature/payments)`. It only suggests and never switches by itself, and it stays quiet about the branch that was already checked out when the tray started.

## Safety

`ws` does not delete anything, does not touch the registry, autostart or system settings, and does not need administrator rights. It terminates a process only for `ws close --force`, after an explicit question listing the processes. VS Code and browsers are started with their own profile per workspace (`--user-data-dir`), so freezing one workspace cannot affect another. Set `isolate: false` on a `vscode` item to use your normal VS Code instead.

How freezing stays safe:
- A process is suspended only if neither it nor its children own a visible window outside the workspace, and only after its windows were successfully hidden. If in doubt it is left running and only the window is hidden, with a warning.
- Never suspended: `explorer.exe` and other shell/system processes, Windows Terminal itself (only the shells of the tabs that `ws` started are suspended), and the whole chain of parents of the running `ws` (so `ws switch` typed inside a workspace's own terminal cannot freeze itself).
- Apps listed in `freeze.exclude` (manifest or `%APPDATA%\ws\config.yml`) keep running and are minimized instead of hidden.
- Anything left frozen is recoverable: `ws doctor` lists it, `ws thaw <name>` returns it, and a reboot clears every suspension. Intent is written to `state.json` before any process is suspended.

## Manual checklist (needs a real Windows desktop)

1. `ws doctor`: per-monitor DPI is on, `wt`, VS Code and browsers are found.
2. Create a workspace with `ws edit demo` (explorer + terminal with two tabs + browser with a profile + vscode), put window positions in it.
3. `ws open demo` on a clean desktop: every window appears at its position; warnings (if any) are printed.
4. `ws open demo` again: no duplicates, windows come to the front.
5. Close one window by hand, `ws open demo`: only that item is relaunched.
6. Unplug/disable the second monitor and repeat: windows fall back to the primary monitor.
7. Arrange a few windows, `ws save x`, close them all, `ws open x`: the same layout comes back (browser URLs and terminal tabs need to be added to the manifest by hand).
8. With two workspaces `a` and `b`: `ws open a`, `ws switch b`. The windows of `a` are gone, its processes are suspended (Task Manager shows them as "Suspended" with 0% CPU, `ws status` agrees). `ws switch a` brings everything back without restarting anything, in well under 2 seconds.
9. `ws freeze` then `ws doctor` (shows what is frozen) then `ws thaw a`.
10. Run `ws switch b` from a terminal tab that belongs to `a`: the command finishes and does not freeze itself.
11. `ws tray --background`, then press `Ctrl+Alt+1` / `Ctrl+Alt+2`: the workspaces switch exactly like `ws switch`. Right-click the icon: the list matches, the active one is checked. `ws tray --stop` removes the icon.
12. In a repo with `.workspace.yml`: `ws open .` from a subfolder, then `ws list` shows it with its repository and `ws switch <name>` works from anywhere.
13. `ws hook install`, open a workspace with a terminal tab, run a few commands, `ws switch` away and back: the `↩` line names the last command and folder. `ws hook uninstall` restores your profile.
14. With `triggers.git_branch` set and `ws tray` running: `git checkout -b feature/x` in the repo gives a notification and a «→ Go to» line in the tray menu.

## Known limitations

- `env` from a manifest applies only to newly started processes. An already running VS Code or browser instance will not receive it.
- Terminal tabs can only be described by the manifest; they cannot be read back from a running Windows Terminal.
- A terminal tab with `cmd` starts it through `powershell.exe -NoExit -Command` (so the tab stays open). Quotes inside `cmd` are passed through `wt`'s own parser; keep commands simple or put them in a script.
- `ws save` takes **every** visible window, so delete what you do not need with `ws edit`. Minimized windows, windows owned by another workspace, UWP apps and elevated windows are skipped and listed with the reason. Command-line arguments of generic apps are deliberately not saved (they may contain tokens).
- `ws save` cannot read open browser tabs or terminal tabs. The VS Code folder is read from the command line, which works for instances started by `ws` (or `code <folder>` as the first instance); for a normally started VS Code the window is written as a commented-out block with its position, and you fill in `path` by hand.
- Terminal tabs you open by hand after `ws open` are not tracked: their shells keep running when the workspace is frozen (the window is still hidden). Only the shells of tabs described in the manifest are suspended.
- Windows Terminal may ask for confirmation before closing a window with running commands; `ws close` then reports the window as not closed and leaves the decision to you.
- A suspended process can still show `running` in `psutil` if a few of its threads sit in a kernel wait; they use no CPU. `ws status` shows this count only as an estimate.
- A suspended process that owns the clipboard can make pasting in other apps wait until it is thawed.
- If a process shared by two workspaces (for example a normal, non-isolated VS Code) is involved, its window is hidden but the process is not suspended.
- A hotkey that another app already owns cannot be registered; the tray tells you which one. `Ctrl+Alt+digit` is used by some input methods and games.
- Windows may refuse to bring a window to the front when the switch is triggered from a background process; the windows are still shown, only the focus can stay where it was.
- The "where I stopped" and branch notifications are balloon notifications from the tray icon. They have no clickable button (that would need a Windows toast library, which is not a dependency of `ws`); the action lives in the tray menu instead.
- Notes show the last command only for shells started by `ws` with the hook installed. Other terminals, and shells that are not PowerShell, contribute nothing; the window title is still recorded.
- Windows run as administrator cannot be managed by a normal `ws`; such items are marked `uncontrollable` and a warning is shown.
- UWP/Store apps are treated as generic apps and may be managed incompletely.
- An isolated VS Code instance has its own settings copy (taken from your main profile on first start) and its own sign-ins.

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Fedor Makovskiy.

---

Author: [Fedor Makovskiy](https://fedor.alqira.ru), AI developer. Services and projects with quality metrics: [fedor.alqira.ru](https://fedor.alqira.ru).
