"""Unit tests that run on any OS without a microphone, GPU or desktop:
    .venv/bin/python -m unittest discover -s dictate/tests -p 'test_*.py' -v
The worker tests need faster-whisper installed (they are skipped otherwise); nothing loads a model."""
import importlib.util, json, os, sys, tempfile, threading, time, unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="dictate-test-"))
os.environ.update(DICTATE_STATE=str(TMP / "state"), XDG_CONFIG_HOME=str(TMP / "conf"), APPDATA=str(TMP / "conf"),
                  DICTATE_SOCK=str(TMP / "ctl.sock"),
                  DICTATE_CFG=json.dumps({"tray": False, "notify": False, "sounds": False, "save_audio": False,
                                          "type_settle_ms": 0}))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import profiles, learn, keys, platform_win  # noqa: E402


class Profiles(unittest.TestCase):
    P = profiles.load({"terminal": {"enter_phrases": ["send it"]}, "notes": {"apps": ["obsidian"], "final_period": "strip"}})

    def test_match(self):
        self.assertEqual(profiles.match("gnome-terminal-server", self.P)[0], "terminal")
        self.assertEqual(profiles.match("WindowsTerminal", self.P)[0], "terminal")
        self.assertEqual(profiles.match("Slack", self.P)[0], "chat")
        self.assertEqual(profiles.match("Obsidian", self.P)[0], "notes")
        self.assertEqual(profiles.match("Google_Chrome", self.P)[0], "default")
        self.assertEqual(profiles.match("", self.P)[0], "default")

    def test_terminal_format(self):
        t = self.P["terminal"]
        self.assertEqual(profiles.apply("Git status.", t), ("git status", False))
        self.assertEqual(profiles.apply("Fix it. Then run the tests.", t), ("fix it. Then run the tests.", False))
        self.assertEqual(profiles.apply("I think so.", t), ("I think so", False))
        self.assertEqual(profiles.apply("Kubernetes is down.", t, protect={"Kubernetes"}), ("Kubernetes is down", False))
        self.assertEqual(profiles.apply("Wait...", t), ("wait...", False))

    def test_enter_phrase(self):
        t = self.P["terminal"]
        self.assertEqual(profiles.apply("Run the tests, send it.", t), ("run the tests", True))
        self.assertEqual(profiles.apply("Send it.", t), ("", True))
        self.assertEqual(profiles.apply("Please send it to Bob.", t), ("please send it to Bob", False))

    def test_default_untouched(self):
        self.assertEqual(profiles.apply("Hello there.", self.P["default"]), ("Hello there.", False))


class Learn(unittest.TestCase):
    def test_suggest(self):
        self.assertEqual(learn.suggest("raise a TR now", "raise a PR now")[0]["line"], "tr => PR")
        s = learn.suggest("push to github", "push to GitHub")[0]
        self.assertEqual((s["kind"], s["line"]), ("vocab", "GitHub"))
        self.assertEqual(learn.suggest("use postgress ql", "use PostgreSQL")[0]["kind"], "vocab")
        self.assertEqual(learn.suggest("Yes it works.", "yes, it works"), [])        # punctuation / sentence case only

    def test_store_and_accept(self):
        learn.record_correction("t1", "deploy with cube control", "deploy with kubectl")
        learn.record_correction("t2", "cube control apply", "kubectl apply")
        items = learn.suggestions()
        self.assertEqual(len(items), 1); self.assertEqual(items[0]["count"], 2)
        learn.resolve(items[0]["id"], "accept")
        self.assertIn("cube control => kubectl", learn.list_lines("replacements.txt"))
        self.assertEqual(learn.suggestions(), [])
        self.assertEqual(learn.corrections()["t2"], "kubectl apply")


@unittest.skipUnless(importlib.util.find_spec("faster_whisper"), "faster-whisper not installed")
class LiveCut(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import worker
        cls.w = worker
        cls.S = worker.SR

    def spans(self, *secs):
        return [(int(a * self.S), int(b * self.S)) for a, b in secs]

    def test_waits_for_enough_audio(self):
        self.assertIsNone(self.w.live_cut(self.spans((0.5, 4)), 5 * self.S, 8, 0.6, 28))

    def test_cuts_at_latest_long_pause(self):
        c = self.w.live_cut(self.spans((0.5, 5), (6, 9), (10, 12.5)), 13 * self.S, 8, 0.6, 28)
        self.assertAlmostEqual(c / self.S, 9.5, places=1)

    def test_short_pause_after_twice_min_chunk(self):
        sp = self.spans((0.5, 9), (9.3, 17.5))                      # only a 0.3 s breath
        self.assertIsNone(self.w.live_cut(sp, 12 * self.S, 8, 0.6, 28))
        self.assertAlmostEqual(self.w.live_cut(sp, 18 * self.S, 8, 0.6, 28) / self.S, 9.15, places=1)

    def test_hard_cut_without_pauses(self):
        self.assertEqual(self.w.live_cut(self.spans((0, 30)), 30 * self.S, 8, 0.6, 28), 28 * self.S)

    def test_long_silence_dropped(self):
        self.assertEqual(self.w.live_cut([], 10 * self.S, 8, 0.6, 28), 9 * self.S)

    def test_plan_chunks(self):
        sp = self.spans((0, 10), (11, 25), (26, 40))
        plan = self.w.plan_chunks(sp, 41 * self.S, 28)
        self.assertTrue(all(b - a <= 28 * self.S for a, b in plan))
        self.assertEqual(plan[0][0], 0); self.assertEqual(plan[-1][1], 41 * self.S)

    def test_pad_spans_merges(self):
        self.assertEqual(self.w.pad_spans([(8000, 16000), (20000, 30000)], 40000), [(1600, 36400)])


class Keys(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(keys.EV_CODES["space"], 57)
        self.assertEqual(keys.EV_CODES["a"], 30); self.assertEqual(keys.EV_CODES["z"], 44)
        self.assertEqual(keys.EV_CODES["1"], 2); self.assertEqual(keys.EV_CODES["0"], 11)
        self.assertEqual(keys.EV_CODES["f1"], 59); self.assertEqual(keys.EV_CODES["f12"], 88)
        self.assertEqual(keys.EVIOCGKEY, 0x80604518)

    def test_gnome_binding(self):
        self.assertEqual(keys.parse_gnome_binding("<Control><Alt>space"), "space")
        self.assertEqual(keys.parse_gnome_binding("<Super>F9"), "f9")
        self.assertIsNone(keys.parse_gnome_binding(""))

    def test_win_hotkeys(self):
        self.assertEqual(platform_win.parse_hotkey("ctrl+alt+backspace"), (0x3, 0x08))
        self.assertEqual(platform_win.parse_hotkey("ctrl+alt+z"), (0x3, ord("Z")))
        self.assertEqual(platform_win.SPECIAL_KEYS["backspace"], 0x08)


# ----------------------------------------------------------------------------- daemon flows with fakes
class FakeTyper:
    def __init__(self):
        self.calls, self.ready, self.error, self.proc = [], threading.Event(), None, None

    def start(self): pass
    def restart(self, why=""): pass
    def type(self, text, delay_ms=None): self.calls.append(("type", text))
    def key(self, name, n=1): self.calls.append(("key", name, n))


class FakeKeys:
    def __init__(self, held_for):
        self.until = time.monotonic() + held_for
        self.available = True

    def main_held(self): return time.monotonic() < self.until
    def mods_held(self): return False
    def wait_released(self, timeout=1.5, include_main=True): return 0.0
    def close(self): pass


class DaemonFlows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import dictate
        cls.d = dictate

    def make(self, text, app="kitty"):
        dm = self.d.Daemon()
        dm.typer = FakeTyper()
        dm.worker.submit = lambda path, timeout=300, stream=None: {"text": text, "audio_s": 2.0, "asr_s": 0.1}
        dm.focus = app
        return dm

    def test_terminal_profile_and_undo(self):
        dm = self.make("Git status.")
        self.assertEqual(dm.run_job(b"\0" * 32000, time.perf_counter()), "git status")
        self.assertEqual(dm.typer.calls, [("type", "git status ")])
        self.d.focus_app = lambda timeout=2.0: "kitty"
        self.assertEqual(dm.undo(), "removed 11 characters")
        self.assertEqual(dm.typer.calls[-1], ("key", "backspace", 11))
        self.assertTrue(dm.undo().startswith("cannot undo"))                  # only once

    def test_undo_refused_in_other_app(self):
        dm = self.make("Hello world.", app="Google_Chrome")
        dm.run_job(b"\0" * 32000, time.perf_counter())
        self.d.focus_app = lambda timeout=2.0: "kitty"
        self.assertIn("focus moved", dm.undo())

    def test_send_it_presses_enter_and_blocks_undo(self):
        dm = self.make("Run the tests, send it.")
        dm.profiles["terminal"]["enter_phrases"] = ["send it"]
        dm.run_job(b"\0" * 32000, time.perf_counter())
        self.assertEqual(dm.typer.calls, [("type", "run the tests"), ("key", "enter", 1)])
        self.assertIn("Enter", dm.undo())

    def test_history_has_wpm(self):
        dm = self.make("one two three four")
        dm.run_job(b"\0" * 32000, time.perf_counter())
        last = json.loads((self.d.STATE / "history.jsonl").read_text().splitlines()[-1])
        self.assertEqual((last["words"], last["wpm"], last["profile"]), (4, 120.0, "terminal"))
        self.assertIn("stop_to_text_s", last); self.assertIn("eff_wpm", last)

    def test_cancel_while_busy(self):
        dm = self.make("should not be typed")
        orig = dm.worker.submit
        dm.worker.submit = lambda *a, **k: (dm.cancel(), orig(*a, **k))[1]
        self.assertEqual(dm.run_job(b"\0" * 32000, time.perf_counter()), "cancelled")
        self.assertEqual(dm.typer.calls, [])

    def test_hold_vs_tap(self):
        for held, expect_stop in ((0.6, True), (0.1, False)):
            dm = self.make("x")
            stopped = []
            dm.recording, dm.t_rec = True, 1.0
            dm.stop_rec = lambda dry=False: stopped.append(1)
            dm._hold_watch(1.0, FakeKeys(held))
            self.assertEqual(bool(stopped), expect_stop, f"held {held}s")
            self.assertFalse(dm.press_active)

    def test_toggle_ignored_while_key_still_down(self):
        dm = self.make("x")
        dm.recording, dm.press_active = True, True
        self.assertEqual(dm.handle("toggle"), "holding")


if __name__ == "__main__":
    unittest.main()
