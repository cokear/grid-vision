import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import recaptcha_yolo as cli
from result import SolveStatus


class Tab:
    def __init__(self, tab_id, title, url):
        self.tab_id, self.title, self.url = tab_id, title, url


class Browser:
    def __init__(self):
        self._tabs = [Tab("a", "Dashboard", "https://one.test"), Tab("b", "Login", "https://two.test/auth")]
        self.latest_tab = self._tabs[-1]
        self.tab_ids = [tab.tab_id for tab in self._tabs]

    def get_tabs(self):
        return self._tabs

    def get_tab(self, tab_id):
        return next(tab for tab in self._tabs if tab.tab_id == tab_id)


class CliTests(unittest.TestCase):
    def test_parse_cdp(self):
        self.assertEqual(cli.parse_cdp("127.0.0.1:9222"), ("127.0.0.1", 9222))
        self.assertEqual(cli.parse_cdp("http://localhost:9333"), ("localhost", 9333))
        with self.assertRaises(Exception):
            cli.parse_cdp("localhost")

    def test_select_tab(self):
        browser = Browser()
        self.assertEqual(cli.select_tab(browser, None, "log", None).tab_id, "b")
        self.assertEqual(cli.select_tab(browser, None, None, "one.test").tab_id, "a")
        self.assertEqual(cli.select_tab(browser, "a", None, None).title, "Dashboard")
        with self.assertRaises(LookupError):
            cli.select_tab(browser, None, "missing", None)

    def test_solve_emits_json_and_exit_code(self):
        args = cli.build_parser().parse_args(["solve", "--title", "login"])
        fake_solver = mock.Mock(return_value=SolveStatus.SUCCESS)
        fake_module = mock.Mock(handle_recaptcha_yolo=fake_solver)
        out = io.StringIO()
        with mock.patch.object(cli, "attach", return_value=Browser()), mock.patch.dict(
            sys.modules, {"solver": fake_module}
        ), contextlib.redirect_stdout(out):
            code = cli.run_solve(args)
        payload = json.loads(out.getvalue())
        self.assertEqual(code, cli.EXIT_OK)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["tab"]["id"], "b")

    def test_doctor_without_api_is_machine_readable(self):
        args = cli.build_parser().parse_args(["doctor"])
        out = io.StringIO()
        with mock.patch.object(cli, "dependency_probe", return_value={"ok": True, "missing": []}), mock.patch.dict(
            os.environ, {}, clear=True
        ), contextlib.redirect_stdout(out):
            code = cli.run_doctor(args)
        payload = json.loads(out.getvalue())
        self.assertEqual(code, cli.EXIT_FAILED)
        self.assertFalse(payload["api"]["ok"])
        self.assertIsNone(payload["cdp"]["ok"])


if __name__ == "__main__":
    unittest.main()