"""Popup exit paths: Esc everywhere, and the background dispatch worker the popup now uses.

The editor tests run against a real pty pair so cbreak reading, Esc detection, cursor motion and
split UTF-8 reads behave exactly as they do inside the Herdr popup.
"""

import contextlib
import errno
import io
import json
import os
import pty
import re
import select
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
import unicodedata
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from router import cli, herdr, model
from router.score import Decision

POLICY = model.load_default()


def decision() -> Decision:
    return cli.route_task("rename a variable", POLICY, use_backend=False)


class EditorTest(unittest.TestCase):
    """Drive _edited_line over a pty master; the slave fd plays the popup tty."""

    def setUp(self):
        cli._QUEUED.clear()
        cli._PASTE_CARRY, cli._PASTE_CR = b"", False
        self.master, self.slave = pty.openpty()
        tty.setcbreak(self.slave)  # _editor does this for real popups; keep it out of unit scope
    def tearDown(self):
        if self.master is not None:
            os.close(self.master)
        if self.slave is not None:
            os.close(self.slave)
        cli._QUEUED.clear()
        cli._PASTE_CARRY, cli._PASTE_CR = b"", False

    def drive(self, *chunks) -> str | None:
        """Feed chunks to the master, then return the accepted line (None when Esc closed)."""
        result = []

        def run():
            try:
                result.append(cli._edited_line("> ", self.slave))
            except cli._PopupExit:
                result.append(None)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        for delay, chunk in chunks:
            time.sleep(delay)
            os.write(self.master, chunk)
        t.join(3)
        self.assertFalse(t.is_alive(), "editor did not return")
        return result[0] if result else "TIMEOUT"

    def test_plain_typing_and_enter(self):
        self.assertEqual(self.drive((0.05, b"rename the variable"), (0.05, b"\r")),
                         "rename the variable")

    def test_esc_closes_every_prompt(self):
        self.assertIsNone(self.drive((0.08, b"\x1b")))

    def test_arrow_keys_and_midline_insert(self):
        self.assertEqual(self.drive((0.05, b"ab"), (0.05, b"\x1b[D"), (0.05, b"c"), (0.05, b"\r")),
                         "acb")

    def test_backspace_ctrl_u_ctrl_k(self):
        self.assertEqual(self.drive((0.05, b"abc"), (0.05, b"\x7f"), (0.05, b"\x15"),
                                    (0.05, b"xy"), (0.05, b"\x0bz"), (0.05, b"\r")),
                         "xyz")

    def test_torn_csi_sequence_is_not_an_escape(self):
        # A lone ESC closes; a truncated CSI stays harmless and must still allow Enter.
        self.assertEqual(self.drive((0.08, b"\x1b["), (0.08, b"\r")), "")

    def test_ss3_and_meta_final_bytes_do_not_leak_into_the_line(self):
        # "ESC O P" (a function key) and "ESC x" (a meta key) must leave no stray printable.
        self.assertEqual(self.drive((0.05, b"a"), (0.05, b"\x1bOP"), (0.05, b"\x1bq"),
                                    (0.05, b"b"), (0.05, b"\r")), "ab")

    def test_eof_on_the_tty_closes_the_editor(self):
        result = []

        def run():
            try:
                result.append(cli._edited_line("> ", self.slave))
            except cli._PopupExit:
                result.append(None)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        time.sleep(0.05)
        master, self.master = self.master, None  # tearDown must not double-close
        os.close(master)  # the generating side of the pty disappears → EOF on the slave
        t.join(3)
        self.assertEqual(result, [None])

    def test_ctrl_d_on_empty_line_closes(self):
        self.assertIsNone(self.drive((0.05, b"\x04")))

    def test_split_utf8_read_is_carried_over(self):
        self.assertEqual(self.drive((0.05, b"\xe5"), (0.05, b"\xa5\xbd"), (0.05, b"\r")), "好")

    def test_paste_continuation_lands_in_the_drain_queue(self):
        self.assertEqual(self.drive((0.05, b"luna\rrest of task\r"), (0.08, b"")), "luna")
        self.assertEqual(cli._drain_paste(), ["rest of task"])


class TerminalScreen:
    """Small VT screen oracle for the editor's output, including delayed wrap and CJK cells."""

    def __init__(self, columns, lines):
        self.columns, self.lines = columns, lines
        self.rows = [[" "] * columns for _ in range(lines)]
        self.row = self.col = 0
        self.pending_wrap = False

    def newline(self):
        self.row += 1
        if self.row == self.lines:
            self.rows.pop(0)
            self.rows.append([" "] * self.columns)
            self.row -= 1

    def feed(self, text):
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\x1b":
                match = re.match(r"\x1b\[([0-9;?]*)([A-Za-z~])", text[i:])
                if not match:
                    raise AssertionError(f"unexpected terminal escape: {text[i:i + 30]!r}")
                params, final = match.groups()
                i += len(match.group())
                if params.startswith("?"):  # bracketed-paste mode changes do not draw anything
                    continue
                n = int(params or "0")
                if final in "ABCD":
                    n = n or 1
                    self.row = max(0, min(self.lines - 1, self.row + (n if final == "B" else -n if final == "A" else 0)))
                    self.col = max(0, min(self.columns - 1, self.col + (n if final == "C" else -n if final == "D" else 0)))
                elif final == "K":
                    self.rows[self.row][self.col:] = [" "] * (self.columns - self.col)
                elif final == "J":
                    self.rows[self.row][self.col:] = [" "] * (self.columns - self.col)
                    for row in range(self.row + 1, self.lines):
                        self.rows[row] = [" "] * self.columns
                else:
                    raise AssertionError(f"unexpected terminal command: {match.group()!r}")
                self.pending_wrap = False
                continue
            i += 1
            if ch == "\r":
                self.col = 0
                self.pending_wrap = False
            elif ch == "\n":
                self.newline()
                self.pending_wrap = False
            elif ch == "\b":
                self.col = max(0, self.col - 1)
                self.pending_wrap = False
            elif unicodedata.combining(ch):
                col = self.col if self.pending_wrap else max(0, self.col - 1)
                while col and self.rows[self.row][col] == "":
                    col -= 1
                self.rows[self.row][col] += ch
            else:
                width = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
                if self.pending_wrap or self.col + width > self.columns:
                    self.col = 0
                    self.newline()
                self.rows[self.row][self.col] = ch
                if width == 2:
                    self.rows[self.row][self.col + 1] = ""
                self.col += width
                self.pending_wrap = self.col == self.columns
                if self.pending_wrap:
                    self.col -= 1

    def text(self):
        return ["".join(row).rstrip() for row in self.rows]


class RealPTYEditorTest(unittest.TestCase):
    """Both input and output use a fresh PTY; the child never opens a Herdr popup."""

    CHILD = r'''
import json, os, sys, termios
from router import cli
sys.stdout.write("HEADER\r\n")
sys.stdout.flush()
before = termios.tcgetattr(0)
try:
    text = cli._ask_task("> ") if sys.argv[1] == "task" else cli._editor("> ")
    result = {"text": text, "queued": cli._QUEUED}
except cli._PopupExit:
    result = {"exit": True}
except BaseException as error:
    result = {"error": repr(error)}
# macOS may set the transient PENDIN flag when restoring canonical input.
after = termios.tcgetattr(0)
for attrs in (before, after):
    attrs[3] &= ~getattr(termios, "PENDIN", 0)
result["restored"] = before == after
os.write(int(sys.argv[2]), json.dumps(result, ensure_ascii=False).encode())
'''

    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.result_fd, self.writer_fd = os.pipe()
        self.process = None
        self.output = bytearray()
        self.columns, self.lines = 12, 10

    def tearDown(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            self.process.wait(timeout=3)
        for fd in (self.master, self.slave, self.result_fd, self.writer_fd):
            if fd is not None:
                os.close(fd)

    def start(self, columns=12, lines=10, mode="line"):
        self.columns, self.lines = columns, lines
        termios.tcsetwinsize(self.slave, (lines, columns))
        self.process = subprocess.Popen(
            [sys.executable, "-c", self.CHILD, mode, str(self.writer_fd)],
            cwd=Path(__file__).resolve().parents[1], stdin=self.slave, stdout=self.slave,
            stderr=self.slave, pass_fds=(self.writer_fd,))
        os.close(self.slave)
        os.close(self.writer_fd)
        self.slave = self.writer_fd = None
        deadline = time.monotonic() + 3
        while b"> " not in self.output and time.monotonic() < deadline:
            self.drain(0.02)
        self.assertIn(b"> ", self.output, "child editor did not become ready")

    def drain(self, seconds=0.03):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not select.select([self.master], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            try:
                chunk = os.read(self.master, 65536)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            self.output.extend(chunk)

    def send(self, text, delay=0.03):
        os.write(self.master, text.encode() if isinstance(text, str) else text)
        self.drain(delay)

    def wait(self, timeout=3):
        # A real terminal consumes output continuously. On macOS, TCSADRAIN can wait for that
        # consumption, so blocking on wait() alone would deadlock the terminal restore.
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            self.drain(0.02)
        self.process.wait(timeout=0.2)
        self.drain()

    def screen(self):
        screen = TerminalScreen(self.columns, self.lines)
        screen.feed(self.output.decode("utf-8"))
        return screen

    def finish(self, expected):
        self.send(b"\r")
        self.wait()
        result = json.loads(os.read(self.result_fd, 65536))
        self.assertNotIn("error", result)
        self.assertTrue(result["restored"], "terminal settings were not restored")
        self.assertEqual(result.get("text"), expected, result)
        return result

    def test_complete_up_moves_on_wrapped_line_and_preserves_input(self):
        self.start()
        self.send("abcdefghijklmnopqr")
        self.send(b"\x1b[A")
        self.assertEqual((self.screen().row, self.screen().col), (1, 8))
        self.send("X")
        self.finish("abcdefXghijklmnopqr")

    def test_split_arrows_over_the_old_40ms_timeout(self):
        self.start()
        self.send("abcdefghijklmnopqr")
        self.send(b"\x1b", 0.08)
        self.send(b"[", 0.08)
        self.send(b"A")
        self.send("X")
        self.finish("abcdefXghijklmnopqr")

    def test_delayed_csi_final_keeps_sequence_state(self):
        self.start()
        self.send("abcdefghijklmnopqr")
        self.send(b"\x1b[", 0.35)
        self.send(b"A")
        self.send("X")
        self.finish("abcdefXghijklmnopqr")

    def test_single_row_up_down_preserves_input_at_boundaries(self):
        self.start()
        self.send("keep")
        self.send(b"\x1b[A\x1b[B")
        self.assertEqual(self.screen().text()[:2], ["HEADER", "> keep"])
        self.finish("keep")

    def test_input_already_in_pty_is_not_flushed_by_mode_change(self):
        os.write(self.master, b"queued-before-ready")
        self.start()
        self.finish("queued-before-ready")

    def test_modified_csi_and_split_ss3_up_down(self):
        self.start()
        self.send("abcdefghijklmnopqr")
        self.send(b"\x1b[1;5A")
        self.send(b"\x1bO", 0.35)
        self.send(b"B")
        self.assertEqual((self.screen().row, self.screen().col), (2, 8))
        self.finish("abcdefghijklmnopqr")

    def test_lone_escape_still_exits_with_bounded_delay(self):
        self.start()
        self.send("keep this task")
        started = time.monotonic()
        self.send(b"\x1b")
        self.wait(timeout=2)
        result = json.loads(os.read(self.result_fd, 65536))
        self.assertEqual(result, {"exit": True, "restored": True})
        self.assertLess(time.monotonic() - started, 1.5)

    def test_torn_sequence_does_not_consume_enter(self):
        self.start()
        self.send("keep")
        self.send(b"\x1b[\r")
        self.wait(timeout=2)
        result = json.loads(os.read(self.result_fd, 65536))
        self.assertEqual(result.get("text"), "keep", result)

    def test_ss3_arrows_and_meta_do_not_swallow_text(self):
        self.start()
        self.send("abc")
        self.send(b"\x1bODX\x1bqY")
        self.finish("abXYc")

    def test_redraw_after_wrap_does_not_duplicate_or_erase_input(self):
        self.start()
        self.send("abcdefghijklmnopqr")
        self.send(b"\x1b[DX")
        self.assertEqual(self.screen().text()[:3], ["HEADER", "> abcdefghij", "klmnopqXr"])
        self.finish("abcdefghijklmnopqXr")

    def test_up_down_keeps_preferred_column_across_short_last_row(self):
        self.start(columns=8)
        self.send("abcdefghijklmnopq")
        self.send(b"\x1b[H\x1b[C\x1b[C\x1b[B\x1b[B\x1b[A")
        self.assertEqual((self.screen().row, self.screen().col), (2, 4))
        self.send("X")
        self.finish("abcdefghijXklmnopq")

    def test_cjk_wrap_before_wide_character_and_vertical_movement(self):
        self.start(columns=7)
        self.send("abcd中文EF")
        self.assertEqual(self.screen().text()[:3], ["HEADER", "> abcd", "中文EF"])
        self.send(b"\x1b[A")
        self.send("X")
        self.assertEqual(self.screen().text()[:3], ["HEADER", "> abcdX", "中文EF"])
        self.finish("abcdX中文EF")

    def test_cjk_backspace_erases_both_cells(self):
        self.start(columns=6)
        self.send("好")
        self.send(b"\x7fX")
        self.assertEqual(self.screen().text()[:2], ["HEADER", "> X"])
        self.finish("X")

    def test_delete_clear_and_kill_remove_stale_wrapped_rows(self):
        self.start(columns=6)
        self.send("abcdefghijklmnop")
        self.send(b"\x1b[H\x1b[C\x1b[3~\x0b")
        self.assertEqual(self.screen().text()[:5], ["HEADER", "> a", "", "", ""])
        self.send(b"\x15Z")
        self.finish("Z")

    def test_full_last_column_home_end_and_prompt_fills_terminal(self):
        self.start(columns=2)
        self.send("abcde")
        self.send(b"\x1b[H\x1b[F\x1b[DX")
        self.assertEqual(self.screen().text()[:5], ["HE", "AD", "ER", ">", "ab"])
        self.finish("abcdXe")

    def test_viewport_handles_input_taller_than_terminal(self):
        self.start(columns=6, lines=4)
        text = "abcdefghijklmnopqrstuvwxyz0123456789"
        self.send(text)
        self.send(b"\x1b[H")
        self.assertIn("> abcd", self.screen().text())
        self.send("X")
        self.send(b"\x1b[FY")
        self.finish("X" + text + "Y")

    def test_unbracketed_multiline_paste_is_preserved(self):
        self.start(mode="task")
        self.send("one\r二\rthree\r")
        self.wait()
        result = json.loads(os.read(self.result_fd, 65536))
        self.assertEqual(result.get("text"), "one\n二\nthree", result)

    def test_split_crlf_paste_preserves_blank_lines_and_unterminated_tail(self):
        self.start(mode="task")
        self.send(b"one\r", 0.01)
        self.send("\n\r\n二\r\nlast", 0.01)
        self.wait()
        result = json.loads(os.read(self.result_fd, 65536))
        self.assertEqual(result.get("text"), "one\n\n二\nlast", result)

    def test_bracketed_multiline_paste_waits_for_enter_and_is_editable(self):
        self.start(columns=8, mode="task")
        self.send("\x1b[200~first\r\n中文\nlast\x1b[201~")
        self.assertIsNone(self.process.poll(), "a pasted newline submitted the task")
        self.assertEqual(self.screen().text()[:4], ["HEADER", "> first", "中文", "last"])
        self.send(b"\x1b[D\x1b[D\x1b[A")
        self.send("X")
        self.finish("first\n中X文\nlast")

    def test_split_utf8_in_paste_tail_is_not_replaced(self):
        self.start(mode="task")
        self.send(b"first\r\xe5", 0.01)
        self.send(b"\xa5\xbd\r", 0.02)
        self.wait()
        result = json.loads(os.read(self.result_fd, 65536))
        self.assertEqual(result.get("text"), "first\n好", result)

    def test_split_bracketed_paste_markers_and_literal_arrow(self):
        self.start(columns=8, mode="task")
        self.send(b"\x1b[20", 0.08)
        self.send(b"0~a\x1b[A\n", 0.08)
        self.send("好\x1b[20", 0.08)
        self.send(b"1~")
        self.assertIsNone(self.process.poll())
        self.finish("a\x1b[A\n好")

    def test_combining_character_does_not_shift_visual_column(self):
        self.start(columns=6)
        self.send("ae\u0301bcdef")
        self.send(b"\x1b[A")
        self.send("X")
        self.finish("aXe\u0301bcdef")


class DecisionRoundTripTest(unittest.TestCase):
    def test_to_dict_round_trips(self):
        self.assertEqual(cli._decision_from_dict(decision().to_dict()), decision())


class WorkerTest(unittest.TestCase):
    """Enter in a plugin popup spawns the worker; the worker reports what needs attention."""

    def setUp(self):
        self.d = decision()
        self.state = Path(tempfile.mkdtemp())
        env = mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": str(self.state),
                                           "HERDR_PLUGIN_ENTRYPOINT_ID": "popup"})
        env.start()
        self.addCleanup(env.stop)

    def write_job(self, **extra) -> Path:
        job = {"lang": "zh", "task": "rename a variable", "cwd": os.getcwd(), "target_pane": "w1:p1",
               "direction": "auto", "start_timeout_ms": 60000, "decision": self.d.to_dict()}
        job.update(extra)
        f = self.state / "popup-job.json"
        f.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        return f

    def test_job_payload_captures_decision_and_context(self):
        f = Path(self.state / "popup-job-x.json")
        popen = mock.MagicMock()
        with mock.patch.object(cli.model, "state_dir", return_value=self.state), \
                mock.patch.object(cli.subprocess, "Popen", popen) as _:
            cli._spawn_dispatch(self.d, task="rename a variable", target="w1:p1",
                                cwd=str(self.state), dcfg={"agent_start_timeout_ms": 60000,
                                                           "direction": "auto"}, lang="zh")
        payload = next(self.state.glob("popup-job-*.json"))
        job = json.loads(payload.read_text(encoding="utf-8"))
        self.assertEqual(job["task"], "rename a variable")
        self.assertEqual(job["target_pane"], "w1:p1")
        self.assertEqual(job["direction"], "auto")
        self.assertEqual(cli._decision_from_dict(job["decision"]), self.d)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[2:4], ["popup-dispatch", "--payload"])
        self.assertEqual(popen.call_args.kwargs["start_new_session"], True)
        payload.unlink()

    def test_spawn_failure_removes_the_payload(self):
        with mock.patch.object(cli.model, "state_dir", return_value=self.state), \
                mock.patch.object(cli.subprocess, "Popen", side_effect=OSError("no exec")):
            with self.assertRaises(OSError):
                cli._spawn_dispatch(self.d, task="t", target=None, cwd=".", dcfg={}, lang="zh")
        self.assertFalse(list(self.state.glob("popup-job-*.json")))

    def test_confirmed_delivery_reopens_nothing(self):
        outcome = SimpleNamespace(prompt_status="confirmed", session_warning=None, pane_id="w1:p9",
                                  agent="r-x-1", stage="prompt", dispatch_id="id1", session_id="s1",
                                  workspace=None)
        with mock.patch.object(cli, "dispatch", return_value=outcome) as send, \
                mock.patch.object(cli, "_popup_notify") as notify:
            code = cli.run_popup_dispatch(SimpleNamespace(payload=self.write_job()))
        self.assertEqual((code, send.call_count), (0, 1))
        notify.assert_not_called()

    def test_worker_drops_the_payload_file_after_reading(self):
        outcome = SimpleNamespace(prompt_status="confirmed", session_warning=None, pane_id="p",
                                  agent="a", stage="prompt", dispatch_id="id", session_id="s",
                                  workspace=None)
        with mock.patch.object(cli, "dispatch", return_value=outcome):
            f = self.write_job()
            cli.run_popup_dispatch(SimpleNamespace(payload=f))
        self.assertFalse(f.exists())

    def test_unconfirmed_delivery_reopens_with_a_warning(self):
        outcome = SimpleNamespace(prompt_status="unknown", session_warning=None, pane_id="w1:p9",
                                  agent="r-x-1", stage="prompt", dispatch_id="id1", session_id="s1",
                                  workspace=None)
        with mock.patch.object(cli, "dispatch", return_value=outcome), \
                mock.patch.object(cli, "_popup_notify") as notify:
            code = cli.run_popup_dispatch(SimpleNamespace(payload=self.write_job()))
        self.assertEqual((code, notify.call_count), (0, 1))
        info = notify.call_args.args[0]
        self.assertEqual(info["kind"], "warning")
        self.assertEqual(info["prompt_status"], "unknown")
        self.assertIn("可能已送达", info["recovery"])

    def test_failure_reopens_with_recovery_and_keeps_prompt_status(self):
        e = herdr.HerdrError("agent_not_ready", "blocked on a dialog")
        e.pane_id, e.agent, e.stage, e.prompt_status = "w1:p3", "r-x-1", "start", "not_sent"
        with mock.patch.object(cli, "dispatch", side_effect=e), \
                mock.patch.object(cli, "_popup_notify") as notify:
            code = cli.run_popup_dispatch(SimpleNamespace(payload=self.write_job()))
        self.assertEqual(code, 1)
        info = notify.call_args.args[0]
        self.assertEqual(info["kind"], "failure")
        self.assertEqual(info["code"], "agent_not_ready")
        self.assertEqual(info["prompt_status"], "not_sent")
        self.assertIn("仍保留", info["recovery"])

    def test_unreadable_payload_reports_a_failure(self):
        f = self.state / "popup-job-bad.json"
        f.write_text("{not json", encoding="utf-8")
        with mock.patch.object(cli, "_popup_notify") as notify:
            code = cli.run_popup_dispatch(SimpleNamespace(payload=f))
        self.assertEqual(code, 1)
        self.assertEqual(notify.call_args.args[0]["code"], "popup_job_unreadable")
        self.assertFalse(f.exists())


class ReportSurfaceTest(unittest.TestCase):
    """_popup_notify persists the outcome; the reopened popup consumes and shows it."""

    def setUp(self):
        self.state = Path(tempfile.mkdtemp())
        env = mock.patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": str(self.state)})
        env.start()
        self.addCleanup(env.stop)

    def test_notify_persists_the_result_and_passes_it_via_env(self):
        with mock.patch.object(cli.model, "state_dir", return_value=self.state), \
                mock.patch.object(cli.herdr, "plugin_pane_open") as open_pane:
            cli._popup_notify({"kind": "failure", "code": "x", "message": "boom"}, "zh")
        result = next(self.state.glob("popup-result-*.json"))
        env = open_pane.call_args.kwargs["env"]
        self.assertEqual(env["HERDR_ROUTER_RESULT"], str(result))
        self.assertEqual(env["HERDR_ROUTER_LANGUAGE"], "zh")
        result.unlink()

    def test_reopen_failure_falls_back_to_popup_log(self):
        with mock.patch.object(cli.model, "state_dir", return_value=self.state), \
                mock.patch.object(cli.herdr, "plugin_pane_open",
                                  side_effect=herdr.HerdrError("no_plugin", "not linked")):
            cli._popup_notify({"kind": "failure", "code": "x", "message": "boom"}, "zh")
        self.assertIn("could not reopen the popup",
                      (self.state / "popup.log").read_text(encoding="utf-8"))

    def run_screen(self, info: dict) -> tuple[int, io.StringIO, mock.Mock]:
        f = self.state / "popup-result-x.json"
        f.write_text(json.dumps(info), encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(cli, "_ask", return_value="") as ask, \
                mock.patch.object(herdr, "pane_screen", return_value="trust this dir? [y/n]"), \
                contextlib.redirect_stdout(out):
            code = cli._popup_result_screen(str(f), "zh")
        return code, out, ask

    def test_failure_screen_shows_error_recovery_and_pane(self):
        info = {"kind": "failure", "code": "agent_not_ready", "message": "blocked on a dialog",
                "pane_id": "w1:p3", "prompt_status": "not_sent", "dispatch_id": "id1",
                "recovery": "任务未发送，pane w1:p3 仍保留；请先处理"}
        code, out, ask = self.run_screen(info)
        self.assertEqual(code, 1)
        self.assertFalse((self.state / "popup-result-x.json").exists(),  # consumed by the screen
                         "the reopened popup must delete the result file")
        self.assertEqual(ask.call_args.args[0], "\n按回车或 Esc 关闭")
        self.assertIn("agent_not_ready", out.getvalue())
        self.assertIn("仍保留", out.getvalue())
        self.assertIn("trust this dir?", out.getvalue())
        self.assertIn("dispatch_id: id1", out.getvalue())

    def test_warning_screen_exits_zero(self):
        code, out, _ = self.run_screen({"kind": "warning", "prompt_status": "unknown",
                                        "recovery": "可能已送达"})
        self.assertEqual(code, 0)
        self.assertIn("可能已送达", out.getvalue())

    def test_unreadable_result_screen(self):
        f = self.state / "popup-result-y.json"
        f.write_text("[broken", encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(cli, "_ask", return_value=""), \
                contextlib.redirect_stdout(out):
            code = cli._popup_result_screen(str(f), "zh")
        self.assertEqual(code, 1)
        self.assertIn("unreadable", out.getvalue())
        self.assertFalse(f.exists())


class PopupBackgroundFlowTest(unittest.TestCase):
    """Inside a plugin popup, Enter must spawn the worker instead of dispatching inline."""

    def setUp(self):
        env = mock.patch.dict(os.environ, {"HERDR_PLUGIN_ENTRYPOINT_ID": "popup",
                                           "HERDR_PLUGIN_STATE_DIR": tempfile.mkdtemp()})
        env.start()
        self.addCleanup(env.stop)

    def test_enter_spawns_worker_and_skips_inline_dispatch(self):
        with mock.patch.object(cli, "_ask", side_effect=["rename a variable", ""]) as _, \
                mock.patch.object(cli, "route_task", return_value=decision()), \
                mock.patch.object(cli, "dispatch") as inline, \
                mock.patch.object(cli, "_spawn_dispatch") as spawn, \
                mock.patch.object(cli.signal, "signal"), mock.patch.object(cli.time, "sleep"):
            code = cli.run_popup(POLICY, "zh")
        self.assertEqual(code, 0)
        spawn.assert_called_once()
        inline.assert_not_called()

    def test_enter_falls_back_to_inline_dispatch_when_spawn_fails(self):
        outcome = SimpleNamespace(prompt_status="confirmed", session_warning=None, pane_id="p",
                                  agent="a", stage="prompt", dispatch_id="id", session_id="s",
                                  workspace=None)
        with mock.patch.object(cli, "_ask", side_effect=["rename a variable", ""]) as _, \
                mock.patch.object(cli, "route_task", return_value=decision()), \
                mock.patch.object(cli, "dispatch", return_value=outcome) as inline, \
                mock.patch.object(cli, "_spawn_dispatch", side_effect=OSError("no exec")), \
                mock.patch.object(cli.signal, "signal"), mock.patch.object(cli.time, "sleep"):
            code = cli.run_popup(POLICY, "zh")
        self.assertEqual((code, inline.call_count), (0, 1))


class EscClosesPopupTest(unittest.TestCase):
    """Esc (or Ctrl-C) anywhere in the popup session closes it cleanly."""

    def test_esc_from_the_task_prompt_closes_with_zero(self):
        with mock.patch.object(cli, "_ask", side_effect=cli._PopupExit), \
                mock.patch.object(cli.signal, "signal"):
            self.assertEqual(cli.run_popup(POLICY, "zh"), 0)

    def test_ctrl_c_is_treated_like_esc(self):
        with mock.patch.object(cli, "_ask", side_effect=KeyboardInterrupt), \
                mock.patch.object(cli.signal, "signal"):
            self.assertEqual(cli.run_popup(POLICY, "zh"), 0)

    def test_idle_alarm_closes_cleanly(self):
        with mock.patch.object(cli, "_ask", side_effect=SystemExit(0)), \
                mock.patch.object(cli.signal, "signal"):
            self.assertEqual(cli.run_popup(POLICY, "zh"), 0)

    def test_q_at_the_menu_closes_with_zero(self):
        with mock.patch.object(cli, "_ask", side_effect=["rename a variable", "q"]) as _, \
                mock.patch.object(cli, "route_task", return_value=decision()), \
                mock.patch.object(cli, "dispatch") as inline, \
                mock.patch.object(cli.signal, "signal"):
            self.assertEqual(cli.run_popup(POLICY, "zh"), 0)
        inline.assert_not_called()


if __name__ == "__main__":
    unittest.main()
