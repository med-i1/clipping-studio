import unittest
from types import SimpleNamespace

from clipper.gui import ClipperGUI


class ImmediateRoot:
    def after(self, _delay, callback):
        callback()


class Button:
    def __init__(self, state):
        self.state = state

    def config(self, **kwargs):
        self.state = kwargs.get("state", self.state)


class ClipperGUIErrorRecoveryTests(unittest.TestCase):
    def test_worker_exception_reenables_start_and_disables_stop(self):
        def fail(_url, _timings):
            raise RuntimeError("download failed")

        log = []
        gui = SimpleNamespace(
            engine=SimpleNamespace(process=fail),
            root=ImmediateRoot(),
            log_message=log.append,
            start_btn=Button("disabled"),
            stop_btn=Button("normal"),
            running=True,
        )
        gui.on_engine_failed = lambda: ClipperGUI.on_engine_failed(gui)

        ClipperGUI.run_engine(gui, "url", "timings.txt")

        self.assertEqual(gui.start_btn.state, "normal")
        self.assertEqual(gui.stop_btn.state, "disabled")
        self.assertFalse(gui.running)
        self.assertTrue(any("ERROR: download failed" in message for message in log))


if __name__ == "__main__":
    unittest.main()
