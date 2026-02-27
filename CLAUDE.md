# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

TUI-Mail is a Python curses-based terminal UI for browsing and composing emails. It wraps the [himalaya](https://github.com/pimalaya/himalaya) CLI binary, delegating all IMAP/SMTP operations to it via subprocess calls. The entire application lives in a single file: `tui_mail.py` (~2100 lines, Python 3.11+ required for `tomllib`).

## Running

```bash
# Basic usage (himalaya must be on PATH or specified with --bin)
python3 tui_mail.py

# Common flags
python3 tui_mail.py --account myaccount --folder INBOX --page-size 20
python3 tui_mail.py --debug                    # enables debug logging
python3 tui_mail.py --debug --debug-log path   # custom log path
python3 tui_mail.py --no-mark-seen             # don't mark messages read on open
python3 tui_mail.py --sender "me@example.com"  # override From header
```

There is no build step, no test suite, and no linter configured. The project has no external Python dependencies beyond the standard library.

## Architecture

The app is structured around four classes in `tui_mail.py`:

- **`HimalayaClient`** (line ~181) — Wraps the himalaya CLI binary. All email operations (list envelopes, read message, delete, reply, send, folder listing) are subprocess calls to `himalaya` with `--quiet` and sometimes `--output json`. Parses both JSON and plain-text fallback output.

- **`TuiMailApp`** (line ~621) — The curses UI application. Manages modes (`list`, `message`, `folders`, `compose`), keyboard input dispatch, and screen drawing. The main loop is `run()` → `_draw()` → `getch()` → handler.

- **`DebugLogger`** (line ~32) — Simple file-based logger writing timestamped `[CATEGORY] message` lines. Enabled via `--debug` flag.

- **`EnvelopeRow` / `FolderRow`** — Dataclasses for parsed email envelope lines and folder entries.

### Key design patterns

- **Himalaya config parsing**: The app reads himalaya's TOML config (`~/.config/himalaya/config.toml` or `$HIMALAYA_CONFIG`) directly via `tomllib` to resolve sender email and account names, independent of the himalaya binary.

- **Modal UI**: Four modes — `list` (envelope list), `message` (reading), `folders` (folder picker), `compose` (writing emails). Each mode has its own key handler (`_handle_list_key`, `_handle_message_key`, `_handle_folders_key`, `_handle_compose_key`).

- **Error resilience**: `HimalayaClient` methods catch `HimalayaError` and attempt fallbacks (e.g., JSON folder list fails → plain text fallback; delete fails due to missing trash → flag as deleted instead).

## Configuration

The app depends on himalaya's TOML configuration. Config lookup order:
1. `$HIMALAYA_CONFIG` (colon-separated paths)
2. `$XDG_CONFIG_HOME/himalaya/config.toml`
3. `~/.config/himalaya/config.toml`
4. `~/.himalaya/config.toml`
5. `~/.himalayarc`

The `himalaya/` subdirectory in this repo is a cloned reference copy of the himalaya project (gitignored), not part of the application code.
