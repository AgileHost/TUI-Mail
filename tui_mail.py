#!/usr/bin/env python3

import argparse
import curses
import datetime
import json
import locale
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class EnvelopeRow:
    id: int
    line: str


@dataclass
class FolderRow:
    name: str
    desc: str = ""


class HimalayaError(RuntimeError):
    pass


class DebugLogger:
    def __init__(self, path: Optional[str]) -> None:
        self.path = os.path.abspath(path) if path else None
        self.enabled = bool(path)
        if not self.enabled:
            return
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.log("BOOT", f"debug enabled path={self.path}")

    def log(self, category: str, message: str) -> None:
        if not self.enabled or not self.path:
            return
        timestamp = datetime.datetime.now().isoformat(timespec="milliseconds")
        line = f"{timestamp} [{category}] {message}".replace("\n", "\\n")
        try:
            with open(self.path, "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
        except OSError:
            pass


def _candidate_himalaya_config_paths() -> List[str]:
    paths: List[str] = []
    env_paths = os.environ.get("HIMALAYA_CONFIG", "").strip()
    if env_paths:
        for raw in env_paths.split(":"):
            path = raw.strip()
            if path:
                paths.append(os.path.abspath(os.path.expanduser(path)))

    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        paths.append(os.path.join(os.path.abspath(os.path.expanduser(xdg)), "himalaya", "config.toml"))
    else:
        paths.append(os.path.join(os.path.expanduser("~"), ".config", "himalaya", "config.toml"))

    paths.append(os.path.join(os.path.expanduser("~"), ".himalaya", "config.toml"))
    paths.append(os.path.join(os.path.expanduser("~"), ".himalayarc"))

    unique: List[str] = []
    seen = set()
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def resolve_default_sender_from_config(
    requested_account: str,
    logger: Optional[DebugLogger] = None,
) -> Tuple[str, str, str]:
    requested_account = requested_account.strip()
    for path in _candidate_himalaya_config_paths():
        if not os.path.isfile(path):
            continue

        try:
            with open(path, "rb") as fp:
                data = tomllib.load(fp)
        except Exception as err:
            if logger:
                logger.log("WARN", f"cannot parse config path={path} err={err}")
            continue

        accounts = data.get("accounts")
        if not isinstance(accounts, dict):
            continue

        if requested_account:
            account_cfg = accounts.get(requested_account)
            if isinstance(account_cfg, dict):
                email = str(account_cfg.get("email", "")).strip()
                if email:
                    return email, requested_account, path
            if "@" in requested_account:
                return requested_account, requested_account, path
            continue

        default_account = ""
        for account_name, account_cfg in accounts.items():
            if isinstance(account_cfg, dict) and account_cfg.get("default") is True:
                default_account = str(account_name).strip()
                break
        if not default_account and len(accounts) == 1:
            default_account = str(next(iter(accounts.keys()))).strip()

        if not default_account:
            continue

        account_cfg = accounts.get(default_account)
        if isinstance(account_cfg, dict):
            email = str(account_cfg.get("email", "")).strip()
            if email:
                return email, default_account, path
        if "@" in default_account:
            return default_account, default_account, path

    return "", "", ""


def list_accounts_from_config(
    logger: Optional[DebugLogger] = None,
) -> Tuple[List[str], str, str]:
    accounts: List[str] = []
    seen = set()
    default_account = ""
    source_path = ""

    for path in _candidate_himalaya_config_paths():
        if not os.path.isfile(path):
            continue

        try:
            with open(path, "rb") as fp:
                data = tomllib.load(fp)
        except Exception as err:
            if logger:
                logger.log("WARN", f"cannot parse config path={path} err={err}")
            continue

        raw_accounts = data.get("accounts")
        if not isinstance(raw_accounts, dict):
            continue

        if not source_path:
            source_path = path

        for account_name, account_cfg in raw_accounts.items():
            name = str(account_name).strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            accounts.append(name)
            if (
                not default_account
                and isinstance(account_cfg, dict)
                and account_cfg.get("default") is True
            ):
                default_account = name

    return accounts, default_account, source_path


class HimalayaClient:
    def __init__(
        self,
        binary: str,
        account: str,
        folder: str,
        page_size: int,
        preview_read: bool,
        logger: Optional[DebugLogger] = None,
    ) -> None:
        self.binary = binary
        self.account = account
        self.folder = folder
        self.page_size = page_size
        self.preview_read = preview_read
        self.logger = logger

    def _log(self, category: str, message: str) -> None:
        if self.logger:
            self.logger.log(category, message)

    def list_envelopes(self, page: int) -> Tuple[List[EnvelopeRow], str]:
        cmd = [
            self.binary,
            "--quiet",
            "envelope",
            "list",
            "--folder",
            self.folder,
            "--page",
            str(page),
        ]
        if self.page_size > 0:
            cmd.extend(["--page-size", str(self.page_size)])
        if self.account:
            cmd.extend(["--account", self.account])

        self._log("ACTION", f"list_envelopes page={page} page_size={self.page_size}")
        out = self._run(cmd)
        rows = self._parse_envelope_rows(out)
        self._log("ACTION", f"list_envelopes result count={len(rows)}")
        return rows, out

    def list_folders(self) -> List[FolderRow]:
        cmd = [self.binary, "--quiet", "--output", "json", "folder", "list"]
        if self.account:
            cmd.extend(["--account", self.account])
        self._log("ACTION", "list_folders (json)")

        try:
            out = self._run(cmd)
            rows = self._parse_folders_json(out)
            if rows:
                return self._normalize_folders(rows)
            self._log("WARN", "list_folders json returned empty after parse; trying plain fallback")
        except HimalayaError as err:
            self._log("WARN", f"list_folders json failed err={self._truncate(str(err))}")

        fallback_cmd = [self.binary, "--quiet", "folder", "list"]
        if self.account:
            fallback_cmd.extend(["--account", self.account])
        self._log("ACTION", "list_folders (plain fallback)")
        out = self._run(fallback_cmd)
        rows = self._parse_folders_plain(out)
        return self._normalize_folders(rows)

    def read_message(self, envelope_id: int) -> str:
        cmd = [
            self.binary,
            "--quiet",
            "message",
            "read",
            "--folder",
            self.folder,
            str(envelope_id),
        ]
        if self.account:
            cmd.extend(["--account", self.account])
        if self.preview_read:
            cmd.append("--preview")
        self._log("ACTION", f"read_message id={envelope_id} preview={self.preview_read}")
        return self._run(cmd)

    def delete_message(self, envelope_id: int) -> str:
        cmd = [
            self.binary,
            "--quiet",
            "message",
            "delete",
            "--folder",
            self.folder,
            str(envelope_id),
        ]
        if self.account:
            cmd.extend(["--account", self.account])
        self._log("ACTION", f"delete_message id={envelope_id}")
        try:
            return self._run(cmd)
        except HimalayaError as err:
            raw_err = str(err)
            if not self._is_missing_trash_error(raw_err):
                raise

            self._log(
                "WARN",
                f"delete_message missing trash folder, trying fallback with deleted flag id={envelope_id}",
            )
            fallback_cmd = [
                self.binary,
                "--quiet",
                "flag",
                "add",
                "--folder",
                self.folder,
                str(envelope_id),
                "deleted",
            ]
            if self.account:
                fallback_cmd.extend(["--account", self.account])

            try:
                return self._run(fallback_cmd)
            except HimalayaError as fallback_err:
                raise HimalayaError(
                    "Failed to delete email: Trash folder is missing and deleted-flag "
                    f"fallback also failed. Error: {fallback_err}"
                ) from fallback_err

    def mark_answered(self, envelope_id: int) -> str:
        cmd = [
            self.binary,
            "--quiet",
            "flag",
            "add",
            "--folder",
            self.folder,
            str(envelope_id),
            "answered",
        ]
        if self.account:
            cmd.extend(["--account", self.account])
        self._log("ACTION", f"mark_answered id={envelope_id}")
        return self._run(cmd)

    def compose_message(self, to: str, subject: str, body: str, sender: str = "") -> str:
        headers = []
        if sender.strip():
            headers.append(f"From: {sender.strip()}")
        headers.append(f"To: {to.strip()}")
        if subject.strip():
            headers.append(f"Subject: {subject.strip()}")
        normalized_body = body.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
        raw_message = "\r\n".join(headers) + "\r\n\r\n" + normalized_body

        cmd = [self.binary, "--quiet", "message", "send"]
        if self.account:
            cmd.extend(["--account", self.account])
        self._log(
            "ACTION",
            "compose_message "
            f"from={sender.strip() or '(none)'} to={to.strip()} "
            f"subject_len={len(subject.strip())} body_len={len(body)}",
        )
        return self._run_with_stdin(cmd, raw_message)

    def _run(self, cmd: List[str]) -> str:
        self._log("CMD", f"run {' '.join(cmd)}")
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=self._env(),
        )
        clean_stdout = self._sanitize_output(proc.stdout)
        clean_stderr = self._sanitize_output(proc.stderr)
        self._log(
            "CMD",
            f"rc={proc.returncode} stdout_len={len(clean_stdout)} stderr_len={len(clean_stderr)}",
        )

        if (
            proc.returncode != 0
            and "--quiet" in cmd
            and self._is_quiet_unsupported(clean_stdout, clean_stderr)
        ):
            return self._run(self._strip_quiet_arg(cmd))

        if proc.returncode != 0 and self._is_soft_success(clean_stdout, clean_stderr):
            return clean_stdout

        if proc.returncode != 0:
            err = (clean_stderr or clean_stdout).strip()
            if not err:
                err = f"Command failed: {' '.join(cmd)}"
            self._log("ERR", f"run failed err={self._truncate(err)}")
            raise HimalayaError(err)
        return clean_stdout

    def _run_with_stdin(self, cmd: List[str], raw_input: str) -> str:
        self._log("CMD", f"run_with_stdin {' '.join(cmd)} stdin_len={len(raw_input)}")
        proc = subprocess.run(
            cmd,
            input=raw_input,
            capture_output=True,
            text=True,
            check=False,
            env=self._env(),
        )
        clean_stdout = self._sanitize_output(proc.stdout)
        clean_stderr = self._sanitize_output(proc.stderr)
        self._log(
            "CMD",
            f"rc={proc.returncode} stdout_len={len(clean_stdout)} stderr_len={len(clean_stderr)}",
        )

        if (
            proc.returncode != 0
            and "--quiet" in cmd
            and self._is_quiet_unsupported(clean_stdout, clean_stderr)
        ):
            return self._run_with_stdin(self._strip_quiet_arg(cmd), raw_input)

        if proc.returncode != 0 and self._is_soft_success(clean_stdout, clean_stderr):
            return clean_stdout

        if proc.returncode != 0 and self._is_send_copy_failure(cmd, clean_stdout, clean_stderr):
            warning = (
                "Email sent, but saving a copy via IMAP failed. "
                "Your server may be legacy/incompatible with this append flow. "
                "Try disabling message.send.save-copy in config."
            )
            self._log(
                "WARN",
                "send succeeded but save-copy failed; "
                f"details={self._truncate(clean_stderr or clean_stdout)}",
            )
            return warning

        if proc.returncode != 0:
            err = (clean_stderr or clean_stdout).strip()
            if not err:
                err = f"Command failed: {' '.join(cmd)}"
            self._log("ERR", f"run_with_stdin failed err={self._truncate(err)}")
            raise HimalayaError(err)

        return clean_stdout

    @staticmethod
    def _env() -> dict:
        env = os.environ.copy()
        env["NO_COLOR"] = "1"
        env["CLICOLOR"] = "0"
        return env

    @staticmethod
    def _sanitize_output(text: Optional[str]) -> str:
        if not text:
            return ""
        ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
        clean = ansi_escape.sub("", text)
        return clean.strip()

    @staticmethod
    def _is_soft_success(stdout: str, stderr: str) -> bool:
        combined = f"{stdout}\n{stderr}".lower()
        return "successfully" in combined

    @staticmethod
    def _is_quiet_unsupported(stdout: str, stderr: str) -> bool:
        combined = f"{stdout}\n{stderr}".lower()
        return "unexpected argument '--quiet'" in combined

    @staticmethod
    def _strip_quiet_arg(cmd: List[str]) -> List[str]:
        stripped = list(cmd)
        if "--quiet" in stripped:
            stripped.remove("--quiet")
        return stripped

    @staticmethod
    def _is_send_copy_failure(cmd: List[str], stdout: str, stderr: str) -> bool:
        joined_cmd = " ".join(cmd).lower()
        if "message send" not in joined_cmd:
            return False
        combined = f"{stdout}\n{stderr}".lower()
        missing_folder_case = (
            "cannot add imap message" in combined
            and "cannot resolve imap task" in combined
            and ("folder doesn't exist" in combined or "folder does not exist" in combined)
        )
        legacy_stream_case = (
            "cannot add imap message" in combined
            and (
                "stream error" in combined
                or "unexpected tag in command completion result" in combined
            )
        )
        return missing_folder_case or legacy_stream_case

    @staticmethod
    def _is_missing_trash_error(text: str) -> bool:
        lowered = text.lower()
        return "no folder trash" in lowered or (
            "trash" in lowered and "cannot move imap message" in lowered
        )

    @staticmethod
    def _truncate(text: str, limit: int = 400) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    @staticmethod
    def _parse_envelope_rows(raw: str) -> List[EnvelopeRow]:
        rows: List[EnvelopeRow] = []
        patterns = [
            re.compile(r"^\s*(\d+)\s+"),
            re.compile(r"^\s*[|\u2502]\s*(\d+)\s*[|\u2502]"),
        ]

        for line in raw.splitlines():
            clean = line.rstrip()
            if not clean:
                continue
            found = None
            for pattern in patterns:
                match = pattern.match(clean)
                if match:
                    found = int(match.group(1))
                    break
            if found is None:
                continue
            rows.append(EnvelopeRow(found, clean))
        return rows

    def _parse_folders_json(self, raw: str) -> List[FolderRow]:
        rows: List[FolderRow] = []
        try:
            data = json.loads(raw)
        except Exception as err:
            self._log("WARN", f"cannot parse folders json err={err}")
            return rows

        items = data
        if isinstance(data, dict):
            if isinstance(data.get("folders"), list):
                items = data["folders"]
            elif isinstance(data.get("data"), list):
                items = data["data"]
            else:
                items = []

        if not isinstance(items, list):
            return rows

        for item in items:
            if isinstance(item, str):
                name = item.strip()
                if name:
                    rows.append(FolderRow(name=name))
                continue

            if not isinstance(item, dict):
                continue

            name = str(
                item.get("name")
                or item.get("folder")
                or item.get("path")
                or item.get("id")
                or ""
            ).strip()
            if not name:
                continue
            desc = str(item.get("desc") or item.get("description") or "").strip()
            rows.append(FolderRow(name=name, desc=desc))

        return rows

    def _parse_folders_plain(self, raw: str) -> List[FolderRow]:
        rows: List[FolderRow] = []
        skip_names = {
            "",
            "name",
            "desc",
            "description",
            "folder",
            "folders",
        }
        for line in raw.splitlines():
            clean = line.strip()
            if not clean:
                continue
            if clean.startswith("+") or clean.startswith("-") or clean.startswith("="):
                continue
            low = clean.lower()
            if low.startswith("warn ") or low.startswith("error:"):
                continue

            name = ""
            desc = ""
            if "|" in clean:
                parts = [part.strip() for part in clean.split("|")]
                cells = [cell for cell in parts if cell]
                if cells:
                    name = cells[0]
                    if len(cells) > 1:
                        desc = cells[1]
            else:
                chunks = re.split(r"\s{2,}", clean, maxsplit=1)
                name = chunks[0].strip()
                if len(chunks) > 1:
                    desc = chunks[1].strip()

            if not name or name.lower() in skip_names:
                continue
            rows.append(FolderRow(name=name, desc=desc))

        return rows

    def _normalize_folders(self, rows: List[FolderRow]) -> List[FolderRow]:
        unique: List[FolderRow] = []
        seen = set()
        for row in rows:
            key = row.name.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(FolderRow(name=row.name.strip(), desc=row.desc.strip()))

        has_inbox = any(row.name.upper() == "INBOX" for row in unique)
        if not has_inbox:
            unique.insert(0, FolderRow(name="INBOX", desc="Inbox"))
        else:
            unique.sort(key=lambda row: (0 if row.name.upper() == "INBOX" else 1, row.name.lower()))

        return unique


class TuiMailApp:
    def __init__(
        self,
        client: HimalayaClient,
        logger: Optional[DebugLogger] = None,
        default_sender: str = "",
    ) -> None:
        self.client = client
        self.logger = logger
        self.default_sender = default_sender
        self.page = 1
        self.rows: List[EnvelopeRow] = []
        self.cursor = 0
        self.list_scroll = 0
        self.mode = "list"
        self.status = ""
        self.raw_page_text = ""
        self.message_lines: List[str] = []
        self.message_scroll = 0
        self.message_title = ""
        self.current_message_id: Optional[int] = None
        self.folders: List[FolderRow] = []
        self.folder_cursor = 0
        self.folder_scroll = 0
        self.prev_mode_before_folders = "list"
        self.prev_mode_before_compose = "list"
        self.compose_focus = 0
        self.compose_kind = "new"
        self.compose_reply_all = False
        self.compose_reply_source_id: Optional[int] = None
        self.compose_to = ""
        self.compose_subject = ""
        self.compose_body_lines: List[str] = [""]
        self.compose_to_cursor = 0
        self.compose_subject_cursor = 0
        self.compose_body_row = 0
        self.compose_body_col = 0
        self.compose_body_scroll = 0
        self.stdscr = None
        self.help_visible = False

    def _log(self, category: str, message: str) -> None:
        if self.logger:
            self.logger.log(category, message)

    def run(self, stdscr) -> None:
        self.stdscr = stdscr
        self._log("BOOT", "tui started")
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.timeout(-1)
        self._load_page(reset_cursor=True)

        while True:
            self._draw(stdscr)
            ch = stdscr.getch()
            self._log("KEY", f"mode={self.mode} key={self._key_name(ch)} help={self.help_visible}")
            if self.mode == "compose":
                if not self._handle_compose_key(ch):
                    break
                continue
            if self.help_visible:
                if ch in (ord("?"), 27, curses.KEY_ENTER, 10, 13, ord("q"), ord("Q")):
                    self.help_visible = False
                    self.status = "Help closed."
                    self._log("ACTION", "help closed")
                continue
            if ch == ord("?"):
                self.help_visible = True
                self.status = "Help open. Close with ?, Esc, Enter, or q."
                self._log("ACTION", "help opened")
                continue
            if ch in (ord("f"), ord("F")):
                if self.mode == "folders":
                    self._open_folders_page(force_reload=True)
                else:
                    self._open_folders_page()
                continue
            if ch in (ord("a"), ord("A")):
                self._switch_account()
                continue
            if ch in (ord("c"), ord("C")):
                self._log("ACTION", "compose page requested")
                self._open_compose_page()
                continue
            if ch in (ord("q"), ord("Q")):
                self._log("BOOT", "tui exit requested")
                break
            if self.mode == "list":
                if not self._handle_list_key(ch):
                    break
            elif self.mode == "folders":
                if not self._handle_folders_key(ch):
                    break
            else:
                if not self._handle_message_key(ch):
                    break

    def _handle_list_key(self, ch: int) -> bool:
        if ch in (curses.KEY_UP, ord("k"), ord("K")):
            if self.cursor > 0:
                self.cursor -= 1
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            if self.cursor + 1 < len(self.rows):
                self.cursor += 1
        elif ch in (curses.KEY_ENTER, 10, 13):
            self._open_selected()
        elif ch in (ord("n"), ord("N")):
            self.page += 1
            self._load_page(reset_cursor=True)
        elif ch in (ord("p"), ord("P")):
            if self.page > 1:
                self.page -= 1
                self._load_page(reset_cursor=True)
            else:
                self.status = "Already on the first page."
        elif ch in (ord("r"), ord("R")):
            self._load_page(reset_cursor=False)
        elif ch in (ord("+"), ord("=")):
            self.client.page_size += 10
            self._load_page(reset_cursor=True, loading_msg="Reloading with larger page size...")
        elif ch in (ord("-"), ord("_")):
            if self.client.page_size <= 1:
                self.status = "Page size is already at minimum."
            else:
                self.client.page_size = max(1, self.client.page_size - 10)
                self._load_page(reset_cursor=True, loading_msg="Reloading with smaller page size...")
        elif ch in (ord("d"), ord("D")):
            self._delete_selected_from_list()
        return True

    def _handle_message_key(self, ch: int) -> bool:
        if ch in (ord("b"), ord("B")):
            self.mode = "list"
            self.status = "Back to message list."
        elif ch == ord("d") or ch == ord("D"):
            self._delete_current_message()
        elif ch == ord("r"):
            self._open_reply_compose_page(reply_all=False)
        elif ch == ord("R"):
            self._open_reply_compose_page(reply_all=True)
        elif ch in (curses.KEY_UP, ord("k"), ord("K")):
            if self.message_scroll > 0:
                self.message_scroll -= 1
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            self.message_scroll += 1
        elif ch in (curses.KEY_NPAGE,):
            self.message_scroll += 10
        elif ch in (curses.KEY_PPAGE,):
            self.message_scroll -= 10
        self.message_scroll = max(0, self.message_scroll)
        return True

    def _handle_folders_key(self, ch: int) -> bool:
        if ch in (curses.KEY_UP, ord("k"), ord("K")):
            if self.folder_cursor > 0:
                self.folder_cursor -= 1
        elif ch in (curses.KEY_DOWN, ord("j"), ord("J")):
            if self.folder_cursor + 1 < len(self.folders):
                self.folder_cursor += 1
        elif ch in (curses.KEY_ENTER, 10, 13):
            self._select_current_folder()
        elif ch in (ord("b"), ord("B"), 27):
            self.mode = self.prev_mode_before_folders
            self.status = "Folder selection canceled."
            self._log("ACTION", "folder selection canceled")
        elif ch in (ord("r"), ord("R")):
            self._open_folders_page(force_reload=True)
        return True

    def _handle_compose_key(self, ch: int) -> bool:
        key_tab = getattr(curses, "KEY_TAB", 9)
        key_btab = getattr(curses, "KEY_BTAB", 353)
        key_f5 = getattr(curses, "KEY_F5", -1)
        key_f1 = getattr(curses, "KEY_F1", -1)

        if self.help_visible:
            if ch in (ord("?"), 27, curses.KEY_ENTER, 10, 13, ord("q"), ord("Q")):
                self.help_visible = False
                self.status = "Help closed."
            return True

        if ch in (key_f1, ord("?")):
            self.help_visible = True
            self.status = "Help open. Close with ?, Esc, Enter, or q."
            return True

        if ch in (19, key_f5):  # Ctrl+S or F5
            self._submit_compose_page()
            return True

        if ch == 27:  # Esc
            self._cancel_compose_page()
            return True

        if ch in (9, key_tab):
            self._compose_focus_next()
            return True
        if ch == key_btab:
            self._compose_focus_prev()
            return True

        if self.compose_focus in (3, 4):
            if ch in (curses.KEY_LEFT, ord("h"), ord("H")):
                self.compose_focus = 3
            elif ch in (curses.KEY_RIGHT, ord("l"), ord("L")):
                self.compose_focus = 4
            elif ch in (curses.KEY_ENTER, 10, 13):
                if self.compose_focus == 3:
                    self._submit_compose_page()
                else:
                    self._cancel_compose_page()
            return True

        if self.compose_focus == 0:
            self.compose_to, self.compose_to_cursor = self._edit_single_line(
                self.compose_to,
                self.compose_to_cursor,
                ch,
            )
            if ch in (curses.KEY_ENTER, 10, 13):
                self.compose_focus = 1
            return True

        if self.compose_focus == 1:
            self.compose_subject, self.compose_subject_cursor = self._edit_single_line(
                self.compose_subject,
                self.compose_subject_cursor,
                ch,
            )
            if ch in (curses.KEY_ENTER, 10, 13):
                self.compose_focus = 2
            return True

        self._edit_body(ch)
        return True

    def _compose_focus_next(self) -> None:
        self.compose_focus = (self.compose_focus + 1) % 5

    def _compose_focus_prev(self) -> None:
        self.compose_focus = (self.compose_focus - 1) % 5

    def _open_folders_page(self, force_reload: bool = False) -> None:
        self._log("ACTION", f"folders page open requested force_reload={force_reload}")
        self._show_loading("Loading folders...")

        try:
            rows = self.client.list_folders()
        except HimalayaError as err:
            self.status = f"Error listing folders: {err}"
            self._log("ERR", f"folders page load error err={err}")
            return

        if not rows:
            self.status = "No folders found."
            self._log("WARN", "folders page loaded with empty list")
            return

        self.folders = rows
        self.folder_scroll = 0
        current = self.client.folder.strip()
        idx = 0
        if current:
            for i, row in enumerate(self.folders):
                if row.name.lower() == current.lower():
                    idx = i
                    break
        self.folder_cursor = idx
        if self.mode != "folders":
            self.prev_mode_before_folders = self.mode
        self.mode = "folders"
        self.status = f"{len(self.folders)} folders loaded. Press Enter to select."
        self._log("ACTION", f"folders page opened count={len(self.folders)} current={self.client.folder}")

    def _switch_account(self) -> None:
        accounts, default_account, _ = list_accounts_from_config(logger=self.logger)
        if not accounts:
            self.status = "No accounts found in Himalaya config."
            self._log("WARN", "account switch unavailable: no accounts found")
            return

        target = self._prompt_account_number_modal(
            accounts=accounts,
            default_account=default_account,
        )
        if target is None:
            self.status = "Account switch canceled."
            self._log("ACTION", "account switch canceled")
            return

        old_account = self.client.account
        old_folder = self.client.folder
        old_page = self.page
        old_sender = self.default_sender

        self.client.account = target
        self.client.folder = "INBOX"
        self.page = 1
        self.mode = "list"
        self.current_message_id = None
        self.message_lines = []
        self.message_scroll = 0
        self.message_title = ""

        auto_sender, _, _ = resolve_default_sender_from_config(
            requested_account=target,
            logger=self.logger,
        )
        if auto_sender:
            self.default_sender = auto_sender

        selected_label = target or "(default)"
        self._log("ACTION", f"account switch requested from={old_account or '(default)'} to={selected_label}")
        ok = self._load_page(reset_cursor=True, loading_msg=f"Switching account to {selected_label}...")
        if ok:
            sender_note = "auto-updated" if auto_sender else "unchanged"
            if target:
                self.status = f"Account changed to {target}. Folder reset to INBOX. Sender {sender_note}."
            elif default_account:
                self.status = (
                    f"Account changed to default ({default_account}). Folder reset to INBOX. "
                    f"Sender {sender_note}."
                )
            else:
                self.status = f"Account changed to default. Folder reset to INBOX. Sender {sender_note}."
            self._log("ACTION", f"account switch done to={selected_label} sender={sender_note}")
            return

        self.client.account = old_account
        self.client.folder = old_folder
        self.page = old_page
        self.default_sender = old_sender
        self.mode = "list"
        self.current_message_id = None
        self.message_lines = []
        self.message_scroll = 0
        self.message_title = ""
        self._load_page(reset_cursor=True, loading_msg="Restoring previous account...")
        self.status = f"Could not switch to account '{selected_label}'. Previous account restored."
        self._log("WARN", f"account switch failed target={selected_label}; previous restored")

    def _prompt_account_number_modal(
        self,
        accounts: List[str],
        default_account: str,
    ) -> Optional[str]:
        if self.stdscr is None:
            return None

        value = ""
        error = ""
        self._log("ACTION", "account number modal open")
        while True:
            self._draw(self.stdscr)
            self._draw_account_number_modal(
                self.stdscr,
                accounts=accounts,
                default_account=default_account,
                value=value,
                error=error,
            )
            ch = self.stdscr.getch()

            if ch == 27:
                self._log("ACTION", "account number modal canceled")
                return None
            if ch in (curses.KEY_ENTER, 10, 13):
                if not value:
                    error = "Enter a number from the list."
                    continue
                try:
                    selected = int(value)
                except ValueError:
                    error = "Invalid number."
                    value = ""
                    continue

                if selected == 0:
                    self._log("ACTION", "account number modal selected default")
                    return ""
                if 1 <= selected <= len(accounts):
                    chosen = accounts[selected - 1]
                    self._log("ACTION", f"account number modal selected={chosen}")
                    return chosen

                error = f"Invalid number. Choose 0 to {len(accounts)}."
                value = ""
                continue
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                value = value[:-1]
                error = ""
                continue
            if ch == curses.KEY_RESIZE:
                continue
            if ord("0") <= ch <= ord("9") and len(value) < 4:
                value += chr(ch)
                error = ""

    def _draw_account_number_modal(
        self,
        stdscr,
        accounts: List[str],
        default_account: str,
        value: str,
        error: str,
    ) -> None:
        height, width = stdscr.getmaxyx()
        if width < 28 or height < 10:
            self._safe_addstr(
                stdscr,
                0,
                0,
                self._fit("Modal: terminal too small.", width),
                curses.A_REVERSE,
            )
            return

        current_label = self.client.account or "(default)"
        lines = [
            "SWITCH ACCOUNT",
            "Select an account by number.",
            f"Current: {current_label}",
            f"0) (default){f' -> {default_account}' if default_account else ''}",
        ]

        max_items = max(1, height - 13)
        for i, account in enumerate(accounts[:max_items], start=1):
            tags = []
            if default_account and account.lower() == default_account.lower():
                tags.append("default")
            if self.client.account and account.lower() == self.client.account.lower():
                tags.append("current")
            label = account if not tags else f"{account} ({', '.join(tags)})"
            lines.append(f"{i}) {label}")
        if len(accounts) > max_items:
            lines.append(f"... and {len(accounts) - max_items} more account(s)")

        lines.extend(
            [
                "",
                f"Number: {value}",
                error or f"Type 0 to {len(accounts)} and press Enter. Esc cancels.",
            ]
        )

        box_w = min(width - 4, max(52, max(len(line) for line in lines) + 4))
        box_h = min(height - 4, len(lines) + 2)
        left = max(0, (width - box_w) // 2)
        top = max(0, (height - box_h) // 2)
        right = left + box_w - 1
        bottom = top + box_h - 1

        for x in range(left, right + 1):
            ch = "-"
            if x == left or x == right:
                ch = "+"
            self._safe_addstr(stdscr, top, x, ch, curses.A_BOLD)
            self._safe_addstr(stdscr, bottom, x, ch, curses.A_BOLD)

        for y in range(top + 1, bottom):
            self._safe_addstr(stdscr, y, left, "|", curses.A_BOLD)
            for x in range(left + 1, right):
                self._safe_addstr(stdscr, y, x, " ", curses.A_REVERSE)
            self._safe_addstr(stdscr, y, right, "|", curses.A_BOLD)

        max_line_w = box_w - 4
        for i, line in enumerate(lines[: box_h - 2]):
            if top + 1 + i >= bottom:
                break
            attr = curses.A_REVERSE
            if i == 0:
                attr |= curses.A_BOLD
            if error and i == len(lines) - 1:
                attr |= curses.A_BOLD
            self._safe_addstr(
                stdscr,
                top + 1 + i,
                left + 2,
                self._fit(line, max_line_w),
                attr,
            )

    def _select_current_folder(self) -> None:
        if not self.folders:
            self.status = "No folder available for selection."
            return

        selected = self.folders[self.folder_cursor]
        old_folder = self.client.folder
        self.client.folder = selected.name
        self.page = 1
        self.mode = "list"
        self.current_message_id = None
        self.message_lines = []
        self.message_scroll = 0
        self.message_title = ""
        self._log("ACTION", f"folder selected from={old_folder} to={selected.name}")
        self._load_page(reset_cursor=True, loading_msg=f"Loading folder {selected.name}...")

    def _load_page(self, reset_cursor: bool, loading_msg: str = "Loading message list...") -> bool:
        self._log("ACTION", f"load_page page={self.page} reset_cursor={reset_cursor}")
        self._show_loading(loading_msg)
        try:
            rows, raw = self.client.list_envelopes(self.page)
        except HimalayaError as err:
            self.status = f"Error loading page {self.page}: {err}"
            self._log("ERR", f"load_page error page={self.page} err={err}")
            if self.page > 1:
                self.page -= 1
            return False

        self.rows = rows
        self.raw_page_text = raw
        if reset_cursor:
            self.cursor = 0
            self.list_scroll = 0
        elif self.cursor >= len(self.rows):
            self.cursor = max(0, len(self.rows) - 1)
        if not self.rows:
            self.status = f"No messages on page {self.page}."
        else:
            self.status = (
                f"Page {self.page} loaded ({len(self.rows)} messages, page-size={self.client.page_size})."
            )
        self._log("ACTION", f"load_page done page={self.page} rows={len(self.rows)}")
        return True

    def _open_selected(self) -> None:
        if not self.rows:
            self.status = "No message available to open."
            return

        selected = self.rows[self.cursor]
        self._log("ACTION", f"open_selected id={selected.id}")
        self._show_loading(f"Loading email {selected.id}...")
        try:
            body = self.client.read_message(selected.id)
        except HimalayaError as err:
            self.status = f"Error opening email {selected.id}: {err}"
            self._log("ERR", f"open_selected error id={selected.id} err={err}")
            return

        self.message_lines = body.splitlines() or [""]
        self.message_scroll = 0
        self.message_title = f"Email {selected.id} ({self.client.folder})"
        self.current_message_id = selected.id
        self.mode = "message"
        self.status = f"Email {selected.id} loaded."
        self._log("ACTION", f"open_selected done id={selected.id}")

    def _draw(self, stdscr) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if self.mode == "list":
            self._set_cursor_visible(False)
            self._draw_list(stdscr, height, width)
        elif self.mode == "folders":
            self._set_cursor_visible(False)
            self._draw_folders(stdscr, height, width)
        elif self.mode == "compose":
            self._draw_compose(stdscr, height, width)
        else:
            self._set_cursor_visible(False)
            self._draw_message(stdscr, height, width)
        if self.help_visible:
            self._draw_help_modal(stdscr, height, width)
        stdscr.refresh()

    def _draw_list(self, stdscr, height: int, width: int) -> None:
        account_label = self.client.account or "(default)"
        header = (
            f"TUI Mail | account: {account_label} | folder: {self.client.folder} | page: {self.page} | page-size: {self.client.page_size}"
        )
        self._safe_addstr(stdscr, 0, 0, self._fit(header, width), curses.A_BOLD)

        usable_rows = max(1, height - 3)
        self._adjust_list_scroll(usable_rows)
        start = self.list_scroll
        end = min(len(self.rows), start + usable_rows)

        if self.rows:
            y = 1
            for i in range(start, end):
                row = self.rows[i]
                line = self._fit(row.line, width)
                attr = curses.A_REVERSE if i == self.cursor else curses.A_NORMAL
                self._safe_addstr(stdscr, y, 0, line, attr)
                y += 1
        else:
            self._safe_addstr(
                stdscr,
                1,
                0,
                self._fit("No messages found on this page.", width),
                curses.A_DIM,
            )

        help_line = (
            "j/k: move | Enter: open | d: delete | n/p: paginate | +/-: page-size | a: switch account (number) | f: folders | c: new email | r: refresh | ?: help | q: quit"
        )
        status_line = self.status or ""
        self._safe_addstr(stdscr, height - 2, 0, self._fit(status_line, width), curses.A_DIM)
        self._safe_addstr(stdscr, height - 1, 0, self._fit(help_line, width), curses.A_DIM)

    def _draw_folders(self, stdscr, height: int, width: int) -> None:
        account_label = self.client.account or "(default)"
        header = f"Folders | account: {account_label} | current: {self.client.folder}"
        self._safe_addstr(stdscr, 0, 0, self._fit(header, width), curses.A_BOLD)

        usable_rows = max(1, height - 3)
        self._adjust_folder_scroll(usable_rows)
        start = self.folder_scroll
        end = min(len(self.folders), start + usable_rows)

        if self.folders:
            y = 1
            for i in range(start, end):
                row = self.folders[i]
                marker = "*" if row.name.lower() == self.client.folder.lower() else " "
                line = f"{marker} {row.name}"
                if row.desc:
                    line = f"{line}  -  {row.desc}"
                attr = curses.A_REVERSE if i == self.folder_cursor else curses.A_NORMAL
                self._safe_addstr(stdscr, y, 0, self._fit(line, width), attr)
                y += 1
        else:
            self._safe_addstr(
                stdscr,
                1,
                0,
                self._fit("No folders found.", width),
                curses.A_DIM,
            )

        help_line = "j/k: move | Enter: select folder | a: switch account (number) | r: refresh | b/Esc: back | ?: help | q: quit"
        status_line = self.status or ""
        self._safe_addstr(stdscr, height - 2, 0, self._fit(status_line, width), curses.A_DIM)
        self._safe_addstr(stdscr, height - 1, 0, self._fit(help_line, width), curses.A_DIM)

    def _draw_message(self, stdscr, height: int, width: int) -> None:
        title = (
            f"{self.message_title} | r/R: reply | d: delete | a: switch account (number) | f: folders | c: new email | b: back | j/k: scroll | PgUp/PgDn | ?: help | q: quit"
        )
        self._safe_addstr(stdscr, 0, 0, self._fit(title, width), curses.A_BOLD)

        usable_rows = max(1, height - 2)
        max_scroll = max(0, len(self.message_lines) - usable_rows)
        if self.message_scroll > max_scroll:
            self.message_scroll = max_scroll

        y = 1
        for i in range(self.message_scroll, min(len(self.message_lines), self.message_scroll + usable_rows)):
            self._safe_addstr(stdscr, y, 0, self._fit(self.message_lines[i], width))
            y += 1

        footer = self.status or ""
        self._safe_addstr(stdscr, height - 1, 0, self._fit(footer, width), curses.A_DIM)

    def _draw_compose(self, stdscr, height: int, width: int) -> None:
        self._set_cursor_visible(True)
        if height < 12 or width < 40:
            self._safe_addstr(
                stdscr,
                0,
                0,
                self._fit("Terminal too small for compose view. Please enlarge it.", width),
                curses.A_BOLD,
            )
            self._safe_addstr(
                stdscr,
                1,
                0,
                self._fit("Esc: cancel | Ctrl+S/F5: send", width),
                curses.A_DIM,
            )
            return

        if self.compose_kind == "reply":
            title = "Reply All" if self.compose_reply_all else "Reply Email"
        else:
            title = "New Email"
        header = f"{title} | Tab/Shift+Tab: navigate fields | Ctrl+S/F5: send | Esc: cancel | F1: help"
        self._safe_addstr(stdscr, 0, 0, self._fit(header, width), curses.A_BOLD)

        button_send = "[ Send (Ctrl+S/F5) ]"
        button_cancel = "[ Cancel (Esc) ]"
        send_attr = curses.A_REVERSE if self.compose_focus == 3 else curses.A_NORMAL
        cancel_attr = curses.A_REVERSE if self.compose_focus == 4 else curses.A_NORMAL
        self._safe_addstr(stdscr, 1, 0, self._fit(button_send, width), send_attr)
        cancel_x = min(width - 1, len(button_send) + 2)
        self._safe_addstr(stdscr, 1, cancel_x, self._fit(button_cancel, max(0, width - cancel_x)), cancel_attr)

        form_x = 0
        input_x = 10
        input_w = max(8, width - input_x - 1)

        self._safe_addstr(stdscr, 3, form_x, "To:", curses.A_BOLD if self.compose_focus == 0 else 0)
        to_text, to_offset = self._slice_with_cursor(self.compose_to, self.compose_to_cursor, input_w)
        self._safe_addstr(stdscr, 3, input_x, self._fit(to_text, input_w), curses.A_REVERSE if self.compose_focus == 0 else 0)

        self._safe_addstr(stdscr, 4, form_x, "Subject:", curses.A_BOLD if self.compose_focus == 1 else 0)
        subj_text, subj_offset = self._slice_with_cursor(
            self.compose_subject,
            self.compose_subject_cursor,
            input_w,
        )
        self._safe_addstr(
            stdscr,
            4,
            input_x,
            self._fit(subj_text, input_w),
            curses.A_REVERSE if self.compose_focus == 1 else 0,
        )

        self._safe_addstr(stdscr, 6, form_x, "Body:", curses.A_BOLD if self.compose_focus == 2 else 0)
        body_top = 7
        body_h = max(1, height - body_top - 1)
        self._ensure_compose_body_valid()
        self._adjust_compose_body_scroll(body_h)
        body_w = max(8, width - 1)

        for i in range(body_h):
            line_idx = self.compose_body_scroll + i
            if line_idx >= len(self.compose_body_lines):
                break
            line = self.compose_body_lines[line_idx]
            if self.compose_focus == 2 and line_idx == self.compose_body_row:
                visible, _ = self._slice_with_cursor(line, self.compose_body_col, body_w)
            else:
                visible = line[:body_w]
            self._safe_addstr(stdscr, body_top + i, 0, self._fit(visible, width), curses.A_REVERSE if self.compose_focus == 2 else 0)

        footer = self.status or ""
        self._safe_addstr(stdscr, height - 1, 0, self._fit(footer, width), curses.A_DIM)

        try:
            if self.compose_focus == 0:
                cursor_x = input_x + max(0, self.compose_to_cursor - to_offset)
                stdscr.move(3, min(width - 1, cursor_x))
            elif self.compose_focus == 1:
                cursor_x = input_x + max(0, self.compose_subject_cursor - subj_offset)
                stdscr.move(4, min(width - 1, cursor_x))
            elif self.compose_focus == 2:
                cursor_y = body_top + (self.compose_body_row - self.compose_body_scroll)
                line = self.compose_body_lines[self.compose_body_row]
                _, body_offset = self._slice_with_cursor(line, self.compose_body_col, body_w)
                cursor_x = max(0, self.compose_body_col - body_offset)
                stdscr.move(min(height - 2, max(body_top, cursor_y)), min(width - 1, cursor_x))
            else:
                self._set_cursor_visible(False)
        except curses.error:
            pass

    def _adjust_list_scroll(self, usable_rows: int) -> None:
        if self.cursor < self.list_scroll:
            self.list_scroll = self.cursor
        elif self.cursor >= self.list_scroll + usable_rows:
            self.list_scroll = self.cursor - usable_rows + 1
        self.list_scroll = max(0, self.list_scroll)

    def _adjust_folder_scroll(self, usable_rows: int) -> None:
        if self.folder_cursor < self.folder_scroll:
            self.folder_scroll = self.folder_cursor
        elif self.folder_cursor >= self.folder_scroll + usable_rows:
            self.folder_scroll = self.folder_cursor - usable_rows + 1
        self.folder_scroll = max(0, self.folder_scroll)

    def _adjust_compose_body_scroll(self, usable_rows: int) -> None:
        if self.compose_body_row < self.compose_body_scroll:
            self.compose_body_scroll = self.compose_body_row
        elif self.compose_body_row >= self.compose_body_scroll + usable_rows:
            self.compose_body_scroll = self.compose_body_row - usable_rows + 1
        self.compose_body_scroll = max(0, self.compose_body_scroll)

    @staticmethod
    def _slice_with_cursor(text: str, cursor: int, width: int) -> Tuple[str, int]:
        if width <= 1:
            return text[: max(0, width)], 0
        cursor = max(0, min(cursor, len(text)))
        if len(text) <= width:
            return text, 0
        start = max(0, cursor - width + 1)
        end = start + width
        if end > len(text):
            end = len(text)
            start = max(0, end - width)
        return text[start:end], start

    @staticmethod
    def _fit(text: str, width: int) -> str:
        if width <= 0:
            return ""
        text = text.replace("\t", "    ")
        if len(text) <= width:
            return text
        if width <= 1:
            return text[:width]
        if width <= 3:
            return text[:width]
        return text[: width - 3] + "..."

    @staticmethod
    def _safe_addstr(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
        try:
            stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    @staticmethod
    def _set_cursor_visible(visible: bool) -> None:
        try:
            curses.curs_set(1 if visible else 0)
        except curses.error:
            pass

    def _show_loading(self, message: str) -> None:
        self.status = message
        self._log("STATE", f"loading message={message}")
        if self.stdscr is None:
            return
        self._draw(self.stdscr)

    def _open_compose_page(
        self,
        *,
        kind: str = "new",
        to: str = "",
        subject: str = "",
        body_lines: Optional[List[str]] = None,
        reply_source_id: Optional[int] = None,
        reply_all: bool = False,
    ) -> None:
        if self.mode != "compose":
            self.prev_mode_before_compose = self.mode
        self.mode = "compose"
        self.compose_kind = kind
        self.compose_reply_source_id = reply_source_id
        self.compose_reply_all = reply_all
        self.compose_focus = 2 if kind == "reply" else 0
        self.compose_to = to
        self.compose_subject = subject
        self.compose_body_lines = body_lines[:] if body_lines else [""]
        self.compose_to_cursor = len(self.compose_to)
        self.compose_subject_cursor = len(self.compose_subject)
        self.compose_body_row = 0
        self.compose_body_col = 0
        self.compose_body_scroll = 0
        if kind == "reply":
            self.status = (
                "Composing reply. Start typing at the top of the body; "
                "Tab moves between fields; Ctrl+S/F5 sends; Esc cancels."
            )
            self._log(
                "ACTION",
                f"compose page opened kind=reply source_id={reply_source_id} reply_all={reply_all}",
            )
        else:
            self.status = "Composing new email. Tab moves between fields; Ctrl+S/F5 sends; Esc cancels."
            self._log("ACTION", "compose page opened kind=new")

    def _open_reply_compose_page(self, reply_all: bool) -> None:
        if self.current_message_id is None:
            self.status = "No open email available for reply."
            return

        headers, body_lines = self._split_headers_and_body_from_message(self.message_lines)
        to = self._build_reply_to(headers, reply_all=reply_all)
        subject = headers.get("subject", "").strip()
        if subject and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        elif not subject:
            subject = "Re:"

        prefilled_body = self._build_reply_prefilled_body(body_lines)
        self._open_compose_page(
            kind="reply",
            to=to,
            subject=subject,
            body_lines=prefilled_body,
            reply_source_id=self.current_message_id,
            reply_all=reply_all,
        )

    def _split_headers_and_body_from_message(self, lines: List[str]) -> Tuple[dict, List[str]]:
        headers: dict = {}
        current_key = ""
        body_start: Optional[int] = None

        for i, raw in enumerate(lines):
            line = raw.rstrip("\n")
            if line.strip() == "":
                body_start = i + 1
                break
            if line.startswith((" ", "\t")) and current_key:
                headers[current_key] = (headers.get(current_key, "") + " " + line.strip()).strip()
                continue
            if ":" not in line:
                if not headers:
                    return {}, lines[:]
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            if not key:
                continue
            headers[key] = value.strip()
            current_key = key

        if body_start is None:
            body_start = len(lines) if headers else 0

        return headers, lines[body_start:]

    def _build_reply_to(self, headers: dict, reply_all: bool) -> str:
        sender = headers.get("reply-to") or headers.get("from") or ""
        if not reply_all:
            return sender.strip()

        own = set()
        own_sender_key = self._address_key(self.default_sender)
        if own_sender_key:
            own.add(own_sender_key)
        own_account_key = self._address_key(self.client.account)
        if own_account_key:
            own.add(own_account_key)

        recipients: List[str] = []
        seen = set()
        for source in [sender, headers.get("to", ""), headers.get("cc", "")]:
            for part in self._split_addresses(source):
                key = self._address_key(part)
                if not key:
                    continue
                if key in own or key in seen:
                    continue
                seen.add(key)
                recipients.append(part.strip())

        if recipients:
            return ", ".join(recipients)
        return sender.strip()

    @staticmethod
    def _split_addresses(raw: str) -> List[str]:
        if not raw.strip():
            return []
        parts = [part.strip() for part in raw.replace(";", ",").split(",")]
        return [part for part in parts if part]

    @staticmethod
    def _address_key(raw: str) -> str:
        if not raw:
            return ""
        bracket = re.search(r"<([^>]+@[^>]+)>", raw)
        if bracket:
            return bracket.group(1).strip().lower()
        plain = re.search(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", raw)
        if plain:
            return plain.group(1).strip().lower()
        return raw.strip().lower() if "@" in raw else ""

    def _build_reply_prefilled_body(self, body_lines: List[str]) -> List[str]:
        cleaned = [line.rstrip() for line in body_lines]
        while cleaned and not cleaned[0].strip():
            cleaned.pop(0)
        snippet = cleaned[:5]
        if not snippet:
            return [""]

        quoted = []
        for line in snippet:
            if line:
                quoted.append(f"> {self._truncate(line, 160)}")
            else:
                quoted.append(">")
        if len(cleaned) > 5:
            quoted.append("> ...")

        prefill = [
            "",
            "",
            "----- Original message snippet (max 5 lines) -----",
        ]
        prefill.extend(quoted)
        return prefill

    def _cancel_compose_page(self) -> None:
        self.mode = self.prev_mode_before_compose
        self.status = "Compose canceled."
        self._log("ACTION", "compose canceled")

    def _submit_compose_page(self) -> None:
        to = self.compose_to.strip()
        subject = self.compose_subject.strip()
        body = "\n".join(self.compose_body_lines)

        if not to:
            self.status = "To field is required."
            self.compose_focus = 0
            self._log("WARN", "compose submit blocked empty to")
            return

        self._show_loading("Sending email...")
        ok, result_or_err = self._send_composed_email(to=to, subject=subject, body=body)
        if not ok:
            self.status = result_or_err
            return

        if self.compose_kind == "reply" and self.compose_reply_source_id is not None:
            try:
                self.client.mark_answered(self.compose_reply_source_id)
            except HimalayaError as err:
                self._log(
                    "WARN",
                    f"cannot mark answered id={self.compose_reply_source_id} err={self._truncate(str(err))}",
                )

        self.mode = self.prev_mode_before_compose
        self.status = result_or_err
        if self.prev_mode_before_compose in ("list", "message"):
            self._load_page(reset_cursor=False, loading_msg="Refreshing list after send...")
        self._log("ACTION", f"compose submit done status={self._truncate(result_or_err)}")

    def _send_composed_email(self, to: str, subject: str, body: str) -> Tuple[bool, str]:
        try:
            result = self.client.compose_message(
                to=to,
                subject=subject,
                body=body,
                sender=self.default_sender,
            )
        except HimalayaError as err:
            if not self._is_missing_sender_error(str(err)):
                msg = f"Send error: {err}"
                self._log("ERR", f"compose send error err={err}")
                return False, msg

            self._log("WARN", "compose failed due to missing sender; prompting From")
            sender = self._prompt_text_modal(
                "SENDER (FROM)",
                "Enter sender. Example: Name <email@domain>:",
                max_len=320,
            )
            if sender is None or not sender.strip():
                self._log("ACTION", "compose canceled after missing sender prompt")
                return False, "Send canceled: sender was not provided."

            self.default_sender = sender.strip()
            self._show_loading("Retrying send with sender...")
            try:
                result = self.client.compose_message(
                    to=to,
                    subject=subject,
                    body=body,
                    sender=self.default_sender,
                )
            except HimalayaError as err2:
                self._log("ERR", f"compose resend error err={err2}")
                return False, f"Send error: {err2}"

        msg = result.strip() if result.strip() else "Email sent."
        self._log("ACTION", f"compose send done status={self._truncate(msg)}")
        return True, msg

    def _edit_single_line(self, text: str, cursor: int, ch: int) -> Tuple[str, int]:
        cursor = max(0, min(cursor, len(text)))
        if ch in (curses.KEY_LEFT,):
            cursor = max(0, cursor - 1)
        elif ch in (curses.KEY_RIGHT,):
            cursor = min(len(text), cursor + 1)
        elif ch == curses.KEY_HOME:
            cursor = 0
        elif ch == curses.KEY_END:
            cursor = len(text)
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            if cursor > 0:
                text = text[: cursor - 1] + text[cursor:]
                cursor -= 1
        elif ch == curses.KEY_DC:
            if cursor < len(text):
                text = text[:cursor] + text[cursor + 1 :]
        elif 32 <= ch <= 126:
            text = text[:cursor] + chr(ch) + text[cursor:]
            cursor += 1
        return text, cursor

    def _edit_body(self, ch: int) -> None:
        self._ensure_compose_body_valid()
        row = self.compose_body_row
        col = self.compose_body_col
        line = self.compose_body_lines[row]
        col = max(0, min(col, len(line)))

        if ch == curses.KEY_UP:
            if row > 0:
                row -= 1
                col = min(col, len(self.compose_body_lines[row]))
        elif ch == curses.KEY_DOWN:
            if row + 1 < len(self.compose_body_lines):
                row += 1
                col = min(col, len(self.compose_body_lines[row]))
        elif ch == curses.KEY_LEFT:
            if col > 0:
                col -= 1
            elif row > 0:
                row -= 1
                col = len(self.compose_body_lines[row])
        elif ch == curses.KEY_RIGHT:
            if col < len(line):
                col += 1
            elif row + 1 < len(self.compose_body_lines):
                row += 1
                col = 0
        elif ch == curses.KEY_HOME:
            col = 0
        elif ch == curses.KEY_END:
            col = len(line)
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            if col > 0:
                self.compose_body_lines[row] = line[: col - 1] + line[col:]
                col -= 1
            elif row > 0:
                prev = self.compose_body_lines[row - 1]
                self.compose_body_lines[row - 1] = prev + line
                del self.compose_body_lines[row]
                row -= 1
                col = len(prev)
        elif ch == curses.KEY_DC:
            if col < len(line):
                self.compose_body_lines[row] = line[:col] + line[col + 1 :]
            elif row + 1 < len(self.compose_body_lines):
                self.compose_body_lines[row] = line + self.compose_body_lines[row + 1]
                del self.compose_body_lines[row + 1]
        elif ch in (curses.KEY_ENTER, 10, 13):
            left = line[:col]
            right = line[col:]
            self.compose_body_lines[row] = left
            self.compose_body_lines.insert(row + 1, right)
            row += 1
            col = 0
        elif ch == curses.KEY_NPAGE:
            row = min(len(self.compose_body_lines) - 1, row + 10)
            col = min(col, len(self.compose_body_lines[row]))
        elif ch == curses.KEY_PPAGE:
            row = max(0, row - 10)
            col = min(col, len(self.compose_body_lines[row]))
        elif 32 <= ch <= 126:
            self.compose_body_lines[row] = line[:col] + chr(ch) + line[col:]
            col += 1

        self.compose_body_row = max(0, min(row, len(self.compose_body_lines) - 1))
        self.compose_body_col = max(0, min(col, len(self.compose_body_lines[self.compose_body_row])))

    def _ensure_compose_body_valid(self) -> None:
        if not self.compose_body_lines:
            self.compose_body_lines = [""]
        self.compose_body_row = max(0, min(self.compose_body_row, len(self.compose_body_lines) - 1))
        self.compose_body_col = max(
            0,
            min(self.compose_body_col, len(self.compose_body_lines[self.compose_body_row])),
        )

    def _delete_selected_from_list(self) -> None:
        if not self.rows:
            self.status = "No message available to delete."
            return

        selected = self.rows[self.cursor]
        self._log("ACTION", f"delete from list requested id={selected.id}")
        if not self._confirm_modal(
            "DELETE EMAIL",
            f"Delete email {selected.id} from folder {self.client.folder}? (y/n)",
        ):
            self.status = "Delete canceled."
            self._log("ACTION", f"delete from list canceled id={selected.id}")
            return

        self._show_loading(f"Deleting email {selected.id}...")
        try:
            self.client.delete_message(selected.id)
        except HimalayaError as err:
            self.status = f"Error deleting email {selected.id}: {err}"
            self._log("ERR", f"delete from list error id={selected.id} err={err}")
            return

        self._load_page(reset_cursor=False, loading_msg="Refreshing list after delete...")
        self.status = f"Email {selected.id} deleted."
        self._log("ACTION", f"delete from list done id={selected.id}")

    def _delete_current_message(self) -> None:
        if self.current_message_id is None:
            self.status = "No open email available to delete."
            return

        deleting_id = self.current_message_id
        self._log("ACTION", f"delete current requested id={deleting_id}")
        if not self._confirm_modal(
            "DELETE EMAIL",
            f"Delete email {deleting_id} from folder {self.client.folder}? (y/n)",
        ):
            self.status = "Delete canceled."
            self._log("ACTION", f"delete current canceled id={deleting_id}")
            return

        self._show_loading(f"Deleting email {deleting_id}...")
        try:
            self.client.delete_message(deleting_id)
        except HimalayaError as err:
            self.status = f"Error deleting email {deleting_id}: {err}"
            self._log("ERR", f"delete current error id={deleting_id} err={err}")
            return

        self.mode = "list"
        self.current_message_id = None
        self._load_page(reset_cursor=False, loading_msg="Refreshing list after delete...")
        self.status = f"Email {deleting_id} deleted."
        self._log("ACTION", f"delete current done id={deleting_id}")

    def _confirm_modal(self, title: str, prompt: str) -> bool:
        if self.stdscr is None:
            return False

        self._log("ACTION", f"confirm modal open title={title}")
        while True:
            self._draw(self.stdscr)
            self._draw_text_modal(
                self.stdscr,
                title=title,
                prompt=prompt,
                value="Press y to confirm, or n/Esc to cancel.",
            )
            ch = self.stdscr.getch()
            if ch in (ord("y"), ord("Y")):
                self._log("ACTION", f"confirm modal accepted title={title}")
                return True
            if ch in (ord("n"), ord("N"), 27):
                self._log("ACTION", f"confirm modal rejected title={title}")
                return False

    def _prompt_text_modal(self, title: str, prompt: str, max_len: int) -> Optional[str]:
        if self.stdscr is None:
            return None

        self._log("ACTION", f"text modal open title={title} prompt={self._truncate(prompt)}")
        value = ""
        while True:
            self._draw(self.stdscr)
            self._draw_text_modal(self.stdscr, title=title, prompt=prompt, value=value)
            ch = self.stdscr.getch()

            if ch == 27:
                self._log("ACTION", f"text modal canceled title={title}")
                return None
            if ch in (curses.KEY_ENTER, 10, 13):
                self._log("ACTION", f"text modal submitted title={title} len={len(value.strip())}")
                return value.strip()
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                value = value[:-1]
                continue
            if ch == curses.KEY_RESIZE:
                continue
            if 32 <= ch <= 126 and len(value) < max_len:
                value += chr(ch)

    @staticmethod
    def _key_name(ch: int) -> str:
        if ch == -1:
            return "NONE"
        if 32 <= ch <= 126:
            return chr(ch)
        return str(ch)

    @staticmethod
    def _truncate(text: str, limit: int = 200) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    @staticmethod
    def _is_missing_sender_error(text: str) -> bool:
        return "cannot send message without a sender" in text.lower()

    def _draw_text_modal(self, stdscr, title: str, prompt: str, value: str) -> None:
        height, width = stdscr.getmaxyx()
        if width < 24 or height < 8:
            self._safe_addstr(
                stdscr,
                0,
                0,
                self._fit("Modal: terminal too small.", width),
                curses.A_REVERSE,
            )
            return

        box_w = min(width - 4, max(42, len(prompt) + 4))
        box_h = 7
        left = max(0, (width - box_w) // 2)
        top = max(0, (height - box_h) // 2)
        right = left + box_w - 1
        bottom = top + box_h - 1

        for x in range(left, right + 1):
            ch = "-"
            if x == left or x == right:
                ch = "+"
            self._safe_addstr(stdscr, top, x, ch, curses.A_BOLD)
            self._safe_addstr(stdscr, bottom, x, ch, curses.A_BOLD)

        for y in range(top + 1, bottom):
            self._safe_addstr(stdscr, y, left, "|", curses.A_BOLD)
            for x in range(left + 1, right):
                self._safe_addstr(stdscr, y, x, " ", curses.A_REVERSE)
            self._safe_addstr(stdscr, y, right, "|", curses.A_BOLD)

        max_line_w = box_w - 4
        self._safe_addstr(
            stdscr,
            top + 1,
            left + 2,
            self._fit(title, max_line_w),
            curses.A_REVERSE | curses.A_BOLD,
        )
        self._safe_addstr(
            stdscr,
            top + 2,
            left + 2,
            self._fit(prompt, max_line_w),
            curses.A_REVERSE,
        )
        self._safe_addstr(
            stdscr,
            top + 4,
            left + 2,
            self._fit(value, max_line_w),
            curses.A_REVERSE,
        )

    def _draw_help_modal(self, stdscr, height: int, width: int) -> None:
        lines = [
            "HELP - TUI Mail",
            "",
            "Global:",
            "  ?: open/close help",
            "  a: switch account (choose by number)",
            "  c: open new-email page",
            "  f: open folders page",
            "  q: quit (or close help when modal is open)",
            "",
            "Message list:",
            "  j/k or arrows: move selection",
            "  Enter: open email",
            "  d: delete selected email",
            "  n/p: next/previous page",
            "  +/-: increase/decrease page size",
            "  r: refresh page",
            "",
            "Folders page:",
            "  j/k or arrows: move selection",
            "  Enter: select folder and list messages",
            "  r: refresh folders",
            "  b or Esc: go back without changing folder",
            "",
            "Compose page:",
            "  Tab / Shift+Tab: move across To, Subject, Body, and buttons",
            "  Body: Enter inserts a natural new line",
            "  Ctrl+S or F5: send",
            "  Esc: cancel",
            "  F1: open help",
            "",
            "Message reading:",
            "  j/k or arrows: scroll",
            "  PgUp/PgDn: fast scroll",
            "  r: reply (opens compose page with original snippet)",
            "  R: reply all (same approach)",
            "  d: delete open email",
            "  b: back to list",
            "",
            "Close help modal: ?, Esc, Enter, or q",
        ]

        if width < 20 or height < 8:
            self._safe_addstr(
                stdscr,
                0,
                0,
                self._fit("Help: terminal too small.", width),
                curses.A_REVERSE,
            )
            return

        box_w = min(width - 4, max(len(line) for line in lines) + 4)
        box_h = min(height - 4, len(lines) + 2)
        left = max(0, (width - box_w) // 2)
        top = max(0, (height - box_h) // 2)
        right = left + box_w - 1
        bottom = top + box_h - 1

        for x in range(left, right + 1):
            ch = "-"
            if x == left or x == right:
                ch = "+"
            self._safe_addstr(stdscr, top, x, ch, curses.A_BOLD)
            self._safe_addstr(stdscr, bottom, x, ch, curses.A_BOLD)

        for y in range(top + 1, bottom):
            self._safe_addstr(stdscr, y, left, "|", curses.A_BOLD)
            for x in range(left + 1, right):
                self._safe_addstr(stdscr, y, x, " ", curses.A_REVERSE)
            self._safe_addstr(stdscr, y, right, "|", curses.A_BOLD)

        max_line_w = box_w - 4
        for i, line in enumerate(lines[: box_h - 2]):
            self._safe_addstr(
                stdscr,
                top + 1 + i,
                left + 2,
                self._fit(line, max_line_w),
                curses.A_REVERSE | (curses.A_BOLD if i == 0 else 0),
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simple TUI client to browse emails using the himalaya binary."
    )
    parser.add_argument(
        "--bin",
        default="himalaya",
        help="Path to himalaya binary (default: himalaya)",
    )
    parser.add_argument(
        "--account",
        default="",
        help="Account configured in himalaya (default: default account)",
    )
    parser.add_argument(
        "--folder",
        default="INBOX",
        help="Folder to browse (default: INBOX)",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=20,
        help="Messages per page (default: 20)",
    )
    parser.add_argument(
        "--no-mark-seen",
        action="store_true",
        help="Do not mark message as read when opening it (uses --preview).",
    )
    parser.add_argument(
        "--sender",
        default="",
        help="Default sender (From header) for new emails.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging for all actions.",
    )
    parser.add_argument(
        "--debug-log",
        default="logs/tui_mail.debug.log",
        help="Log file used with --debug (default: logs/tui_mail.debug.log)",
    )
    return parser.parse_args()


def main() -> int:
    locale.setlocale(locale.LC_ALL, "")
    args = parse_args()
    logger = DebugLogger(args.debug_log if args.debug else None)
    resolved_sender = args.sender.strip()
    resolved_account = args.account.strip()
    resolved_config_path = ""

    if not resolved_sender:
        auto_sender, auto_account, auto_path = resolve_default_sender_from_config(
            requested_account=args.account,
            logger=logger,
        )
        if auto_sender:
            resolved_sender = auto_sender
            if not resolved_account:
                resolved_account = auto_account
            resolved_config_path = auto_path

    logger.log(
        "BOOT",
        "startup "
        f"bin={args.bin} account={resolved_account or '(default)'} folder={args.folder} "
        f"page_size={max(1, args.page_size)} sender={resolved_sender or '(none)'}",
    )
    if resolved_config_path:
        logger.log(
            "BOOT",
            f"sender resolved from config path={resolved_config_path} account={resolved_account}",
        )
    elif not args.sender:
        logger.log("WARN", "sender not resolved from config; compose may prompt for From")
    client = HimalayaClient(
        binary=args.bin,
        account=resolved_account,
        folder=args.folder,
        page_size=max(1, args.page_size),
        preview_read=args.no_mark_seen,
        logger=logger,
    )
    app = TuiMailApp(client, logger=logger, default_sender=resolved_sender)

    try:
        curses.wrapper(app.run)
    except KeyboardInterrupt:
        logger.log("BOOT", "keyboard interrupt")
        return 130
    except HimalayaError as err:
        logger.log("ERR", f"fatal HimalayaError err={err}")
        print(f"Error: {err}")
        return 1
    logger.log("BOOT", "shutdown ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
