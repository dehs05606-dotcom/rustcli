import io
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

from fullagent.agent import ToolEvent, Turn
from fullagent.tui import UI


class StreamingTests(unittest.TestCase):
    def make_ui(self, render=False):
        ui = UI.__new__(UI)
        ui.cfg = SimpleNamespace(extra={"render_markdown": render}, show_reasoning=False)
        ui._busy = False
        ui._cancel_flag = threading.Event()
        ui._focus_remaining = 0
        ui._status_text = ""
        ui._width = lambda: 80
        ui._invalidate = lambda: None
        ui._start_spinner = lambda: None
        ui._stop_spinner = lambda: None
        ui._emit_user = lambda text: None
        ui._set_flash = lambda *args: None
        ui._print_turn_stats = lambda turn: None
        ui.output = io.StringIO()
        ui.console = Console(file=ui.output, width=80, color_system=None)
        ui.agent = SimpleNamespace(save_session=lambda: None)
        return ui

    def test_first_partial_token_renders_before_second_token(self):
        ui = self.make_ui()
        ui._approve_request = None
        ui._spinner_i = 0
        frames = []
        def run(text, on_token, *args, **kwargs):
            on_token("first ")
            frames.append("".join(text for _, text in ui._bottom_fragments()))
            on_token("second ")
            frames.append("".join(text for _, text in ui._bottom_fragments()))
            return Turn(text, assistant_text="first second ")
        ui.agent.run_turn = run
        with patch("fullagent.tui.time.time", return_value=100):
            ui._run_turn_thread("hello")
        self.assertIn("first", frames[0])
        self.assertNotIn("second", frames[0])
        self.assertIn("first second", frames[1])

    def test_burst_line_is_visible_before_next_token(self):
        ui = self.make_ui()
        snapshots = []
        def run(text, on_token, *args, **kwargs):
            on_token("first ")
            on_token("line\n")
            snapshots.append(ui.output.getvalue())
            on_token("last")
            snapshots.append(ui._status_text)
            return Turn(text, assistant_text="first line\nlast")
        ui.agent.run_turn = run
        with patch("fullagent.tui.time.time", return_value=100):
            ui._run_turn_thread("hello")
        self.assertIn("first line\n", snapshots[0])
        self.assertIn("last", snapshots[1])
        self.assertEqual(ui.output.getvalue().count("first line"), 1)
        self.assertEqual(ui.output.getvalue().count("last"), 1)

    def test_markdown_burst_preview_updates_without_another_token(self):
        ui = self.make_ui(render=True)
        snapshots = []
        def run(text, on_token, *args, **kwargs):
            on_token("first ")
            on_token("last")
            snapshots.extend([ui._status_text, ui.output.getvalue()])
            return Turn(text, assistant_text="first last")
        ui.agent.run_turn = run
        with patch("fullagent.tui.time.time", return_value=100):
            ui._run_turn_thread("hello")
        self.assertIn("last", snapshots[0])
        self.assertNotIn("first last", snapshots[1])
        self.assertEqual(ui.output.getvalue().count("first last"), 1)

    def test_rapid_submit_starts_only_one_worker(self):
        ui = self.make_ui()
        with patch("fullagent.tui.threading.Thread") as worker:
            ui._dispatch("one")
            ui._dispatch("two")
        self.assertEqual(worker.return_value.start.call_count, 1)

    def test_busy_until_session_is_saved(self):
        ui = self.make_ui()
        ui.agent.run_turn = lambda text, *args, **kwargs: Turn(text)
        def save():
            ui._dispatch("overlapping request")
        ui.agent.save_session = save
        with patch("fullagent.tui.threading.Thread") as worker:
            ui._run_turn_thread("hello")
            worker.return_value.start.assert_not_called()
            ui._dispatch("next request")
            self.assertEqual(worker.return_value.start.call_count, 1)

    def test_completed_edit_does_not_sleep(self):
        ui = self.make_ui()
        ui.console = Console(file=ui.output, force_terminal=True, width=80)
        ui.agent.run_turn = lambda text, token, reason, call, update, *a, **kw: (
            update(ToolEvent("write_file", {"path": "demo.py", "content": "a\nb\nc\n"},
                             result="OK: created demo.py", status="done")) or Turn(text))
        with patch("fullagent.tui.time.sleep") as sleep:
            ui._run_turn_thread("hello")
        sleep.assert_not_called()

    def test_failed_save_releases_turn(self):
        ui = self.make_ui()
        ui.agent.run_turn = lambda text, *args, **kwargs: Turn(text)
        def save():
            raise OSError("disk full")
        ui.agent.save_session = save
        with self.assertRaises(OSError):
            ui._run_turn_thread("hello")
        with patch("fullagent.tui.threading.Thread") as worker:
            ui._dispatch("next request")
            worker.return_value.start.assert_called_once()

    def test_cancel_before_worker_start_is_preserved(self):
        ui = self.make_ui()
        observed = []
        def run(text, *args, should_cancel, **kwargs):
            observed.append(should_cancel())
            return Turn(text, error="cancelled")
        ui.agent.run_turn = run
        with patch("fullagent.tui.threading.Thread"):
            ui._dispatch("hello")
        ui._cancel_flag.set()
        ui._run_turn_thread("hello")
        self.assertEqual(observed, [True])


if __name__ == "__main__":
    unittest.main()
