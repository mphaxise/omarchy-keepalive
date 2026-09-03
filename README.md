# Keepalive

Coding agents that stay running on your Omarchy desktop. Close the terminal and the agent keeps working. Reboot, and the session comes back as orphaned; press Enter and the agent picks up its own transcript where it left off.

Each session shows up in the Omarchy bar. The panel tells you who needs an answer, who is working, and what finished today. When an agent needs you, you get a notification. When a session ends, you get a receipt: the branch, the commits, the diff, the files it made, and your verdict.

![The Keepalive panel with one session that needs an answer](preview.png)

Herdr, Omarchy's agent runtime, keeps the agent process alive. This plugin keeps the record. Every session has a folder under `~/.local/state/omarchy/sessions/` with a log of everything that happened. The bar, the commands, and the notifications all read from that folder.

## What you get

**A bar widget.** A robot icon. It turns red with a badge when a session needs you.

**A panel you can drive from the keyboard.** Arrows move. Enter opens the session, or answers it. `s` sends an instruction. `x` twice stops it. `r` shows the receipt. `p` focuses the session's preview window. `n` starts a new session: type what the agent should do, press Enter, and a terminal opens on it.

**Lanes (0.2.0).** A session can hold more than one agent. Press `a` on a running session, type the task, and a second agent starts in a pane beside the first, on its own git worktree cut from the session's branch. Each lane shows under the session with its state and its task; `w` walks the lanes so Enter, `s`, and `x` act on one of them. A lane that asks a question makes the session need you, with the lane named on the toast and the row. `omarchy-agent-session-done <session> --lane <name> --verdict kept` merges the lane's commits onto the session branch; a conflict shows as a blocked lane and loses nothing. The receipt lists commits per lane. Tested with two Claude Code lanes on one page.

**Twenty-four commands.** All start with `omarchy-agent-session-`. The main ones are `new`, `list`, `open`, `send`, `stop`, `done`, `receipt`, and `show`; `add` and `lanes` are the lane ones. The rest handle names, goals, modes, previews, captures, artifacts, and watchers.

**Notifications.** "api-refactor needs you" when the agent asks a question or a permission. "api-refactor finished" when it is done; click it to see the receipt. "api-refactor stopped unexpectedly" when Herdr loses the process or the machine rebooted; click it to revive.

**Revive.** An orphaned session revives with Enter: the agent starts again with `--resume` on its saved transcript, in the same worktree, with the same goal. Proven with Claude Code on the rig. Whatever tool call the agent was in the middle of is lost; the conversation is kept.

**Permission modes.** Personal: the agent runs without asking. Shared: the agent asks before it acts. Restricted: read-only tools. The plugin checks the launch command before it runs. Only Personal can skip prompts.

## Requirements

- Omarchy Quattro (4.0.x). I tested on Gabriel Galán's aarch64 UTM image ([ggalancs/omarchy-arm-utm](https://github.com/ggalancs/omarchy-arm-utm)) at Omarchy commit `0b3f1b7`. I have not tested on x86.
- Herdr. Omarchy installs it.
- Python 3.11 or later.
- `grim` and `hyprctl`, for screen captures and window focus. Both come with Omarchy.
- A coding agent. I tested with Claude Code. Codex, OpenCode, and the others have permission tables and no live run yet.

## Install

```bash
omarchy plugin add https://github.com/mphaxise/omarchy-keepalive --enable
~/.config/omarchy/plugins/io.github.mphaxise.keepalive/install.sh
```

The first command clones this repo into `~/.config/omarchy/plugins/io.github.mphaxise.keepalive/` and puts the widget in the bar. The second links the commands into `~/.local/bin/` and installs two user units: one keeps the Herdr server running, one sends the notifications. It needs no root. It will not overwrite a file it did not create. Run it again after `omarchy plugin update`.

Keybindings are optional. Add these to `~/.config/hypr/bindings.lua` if you want them:

```lua
o.bind("SUPER + CTRL + G", "Keepalive", "omarchy-shell io.github.mphaxise.keepalive toggle")
o.bind("SUPER + CTRL + SHIFT + G", "Agent that needs you", "omarchy-shell io.github.mphaxise.keepalive openMostUrgent")
```

The plugin leaves Omarchy's own `omarchy-agent` keybinding alone. I have a version of `omarchy-agent` that creates a session and opens a terminal. It replaces Omarchy's command, so it stays out of this plugin. It is in the development repo under `bin/upstream-proposed/`.

## Use

```bash
omarchy-agent-session-new --agent claude --mode personal --name api-refactor --goal "Split the router into modules"
omarchy-agent-session-list
omarchy-agent-session-send api-refactor "Keep the public API unchanged"
omarchy-agent-session-open api-refactor          # opens a terminal; focuses the preview if there is one
omarchy-agent-session-preview api-refactor http://localhost:3000
omarchy-agent-session-done api-refactor --verdict kept --note "Reviewed the diff, merged"
omarchy-agent-session-receipt api-refactor
omarchy-agent-session-show api-refactor --loop  # goal, captures, instructions, commits, verdict, in order
```

`omarchy-shell io.github.mphaxise.keepalive open|close|toggle|refresh|openMostUrgent` controls the panel from a script or a keybinding. `omarchy bar move io.github.mphaxise.keepalive --section left|center|right` moves the widget.

## Remove

```bash
~/.config/omarchy/plugins/io.github.mphaxise.keepalive/uninstall.sh
omarchy plugin remove io.github.mphaxise.keepalive
```

The first command stops and removes the two units and the command links. It keeps your session records in `~/.local/state/omarchy/sessions/`. Delete that folder yourself if you want them gone. Running sessions keep running in Herdr. Stop them first with `omarchy-agent-session-stop` if you want a clean slate.

## What it writes

- Symlinks in `~/.local/bin/`
- Two unit files in `~/.config/systemd/user/`
- Session records in `~/.local/state/omarchy/sessions/`
- Git worktrees in `~/.herdr/worktrees/`, one per session, each on a `session/<name>` branch

That is the full list. It writes nothing to `/usr`, your project files, or Omarchy's config.

## Known issues

- A question and a permission prompt look the same. Herdr reports both as `blocked`, so the panel says "needs you" for both.
- Previews given as a URL open in a Chromium app window. Previews given as an app id are untested.
- On that aarch64 image, every Claude Code launch opens the keyring password prompt. That comes from the image.
- After you edit the plugin's files, the shell's hot reload can keep running the old code. `omarchy-restart-shell` fixes it.

## Source

This plugin is the installable part of [omarchy-multiplayer](https://github.com/mphaxise/omarchy-multiplayer). That repo has the specs, the test runs with screenshots, and the decision log. Run the tests with `python3 -m unittest discover -s tests`: 155 tests, standard library only.

## Credits

The panel follows the structure of Omarchy's built-in `agents` plugin and the `stevequinn/omarchy-hermes-sessions` plugin. The shell components (`Panel`, `KeyboardPanel`, `PanelKeyCatcher`, `CursorSurface`, `Button`, `TextField`) are Omarchy's own, used through `qs.Ui`.

## License

MIT. It depends on Herdr (MIT), the Omarchy shell (MIT), Python 3, grim, and hyprctl. It downloads nothing.
