import http.client
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard.server import DashboardError, Handler, JOBS, ROOT, ThreadingHTTPServer, _run_job, build_command


class Dashboard(unittest.TestCase):
    def test_analysis_tasks_map_to_fixed_tools(self):
        cases = {
            "scanner-daily": ("trader-scanner", "scan.py"),
            "scanner-orb": ("trader-scanner", "scan.py"),
            "news": ("trader-news", "news.py"),
            "entry": ("trader-entry", "entry.py"),
            "exit-plan": ("trader-exit", "exit.py"),
            "timemachine": ("trader-timemachine", "timemachine.py"),
            "budget": ("trader-budget", "size.py"),
            "risk": ("trader-risk", "check.py"),
            "tickets": ("trader-broker", "ticket.py"),
            "alerts-check": ("trader-alerts", "alerts.py"),
            "alerts-clock": ("trader-alerts", "alerts.py"),
            "alerts-list": ("trader-alerts", "alerts.py"),
            "analyst": ("trader-analyst", "facts.py"),
            "pelosi": ("trader-pelosi", "ptr.py"),
            "pelosi-new": ("trader-pelosi", "ptr.py"),
            "quote": ("trader-alerts", "alerts.py"),
            "alerts-plan": ("trader-alerts", "alerts.py"),
            "universe": ("trader-scanner", "scan.py"),
        }
        for task, expected in cases.items():
            with self.subTest(task=task):
                command = build_command({"task": task, "symbol": "TSLA"})
                self.assertEqual(command[1], str(ROOT / expected[0] / expected[1]))
                self.assertTrue(Path(command[0]).name.lower().startswith("python"))

    def test_ticker_arguments_are_normalized_and_checked(self):
        command = build_command({"task": "entry", "symbol": " tsla "})
        self.assertIn("TSLA", command)
        with self.assertRaises(DashboardError):
            build_command({"task": "news", "symbol": "TSLA; whoami"})

    def test_scan_symbols_are_checked_and_joined(self):
        command = build_command({"task": "scanner-orb", "symbols": "amd, nvda tsla,amd"})
        self.assertEqual(command[-2:], ["--symbols", "AMD,NVDA,TSLA"])
        self.assertNotIn("--symbols", build_command({"task": "scanner-daily", "symbols": ""}))
        self.assertEqual(build_command({"task": "scanner-orb", "at": "10:15"})[-2:], ["--at", "10:15"])
        with self.assertRaises(DashboardError):
            build_command({"task": "scanner-orb", "at": "25:00"})
        for bad in ("AMD;rm", ",".join("A%d" % i for i in range(101)), ["AMD"]):
            with self.subTest(symbols=bad):
                with self.assertRaises(DashboardError):
                    build_command({"task": "scanner-daily", "symbols": bad})

    def test_plan_and_replay_options_are_allowlisted(self):
        command = build_command({"task": "exit-plan", "stop": "pct:1.5", "target": "3R"})
        self.assertEqual(command[-4:], ["--stop", "pct:1.5", "--target", "3R"])
        self.assertEqual(build_command({"task": "timemachine", "seed": "7"})[-2:], ["--seed", "7"])
        for payload in ({"task": "exit-plan", "stop": "--write=/etc/x"}, {"task": "exit-plan", "target": "2R; x"},
                        {"task": "timemachine", "seed": "-1"}, {"task": "timemachine", "seed": True}):
            with self.subTest(payload=payload):
                with self.assertRaises(DashboardError):
                    build_command(payload)

    def test_unknown_and_non_string_tasks_are_rejected(self):
        for payload in ({"task": "anything"}, {"task": []}, []):
            with self.subTest(payload=payload):
                with self.assertRaises(DashboardError):
                    build_command(payload)

    def test_broker_route_is_read_only(self):
        command = build_command({"task": "tickets"})
        self.assertEqual(command[2:], ["show"])
        self.assertNotIn("approve", command)
        self.assertNotIn("result", command)
        self.assertNotIn("flatten", command)

    def test_nonzero_tool_exit_keeps_its_output_without_claiming_success(self):
        from types import SimpleNamespace

        with patch("dashboard.server.subprocess.run", return_value=SimpleNamespace(
            returncode=1, stdout="NYSE closed.", stderr=""
        )):
            JOBS["nonzero-test"] = {"status": "queued"}
            _run_job("nonzero-test", ["unused"])
        self.assertEqual(JOBS["nonzero-test"]["status"], "finished")
        self.assertEqual(JOBS["nonzero-test"]["output"], "NYSE closed.")


class DashboardHTTP(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_dashboard_origin_can_queue_only_allowlisted_task(self):
        with patch("dashboard.server.POOL.submit") as submit:
            connection = http.client.HTTPConnection("127.0.0.1", self.port)
            body = json.dumps({"task": "tickets"})
            connection.request(
                "POST",
                "/api/run",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Origin": "http://127.0.0.1:%d" % self.port,
                },
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
        self.assertEqual(response.status, 202)
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(submit.call_count, 1)

    def test_cross_origin_task_request_is_rejected(self):
        with patch("dashboard.server.POOL.submit") as submit:
            connection = http.client.HTTPConnection("127.0.0.1", self.port)
            body = json.dumps({"task": "tickets"})
            connection.request(
                "POST",
                "/api/run",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Origin": "http://evil.example",
                },
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
        self.assertEqual(response.status, 403)
        self.assertIn("local dashboard", payload["error"])
        submit.assert_not_called()

    def test_foreign_host_cannot_read_status(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port)
        connection.request("GET", "/api/status", headers={"Host": "rebind.example:%d" % self.port})
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 421)

        connection = http.client.HTTPConnection("127.0.0.1", self.port)
        connection.request("GET", "/api/status")
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 200)

    def test_duplicate_task_input_is_rejected_while_queued(self):
        with patch("dashboard.server.POOL.submit") as submit:
            responses = []
            for _ in range(2):
                connection = http.client.HTTPConnection("127.0.0.1", self.port)
                connection.request(
                    "POST",
                    "/api/run",
                    body=json.dumps({"task": "alerts-clock"}),
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "http://127.0.0.1:%d" % self.port,
                    },
                )
                response = connection.getresponse()
                responses.append(response.status)
                response.read()
                connection.close()
        self.assertEqual(responses, [202, 409])
        self.assertEqual(submit.call_count, 1)


if __name__ == "__main__":
    unittest.main()
