from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import test_engine as fixtures
from compile_fast.abi import extract, scan
from compile_fast.common import EngineError, cancellation, run, write_json
from compile_fast.gates import verify_build
from compile_fast.pipeline import execute, metrics
from compile_fast.server import Scheduler, make_server
from compile_fast.source import build_index, impact, parse


class PlatformTests(unittest.TestCase):
    setUp, tearDown = fixtures.EngineTests.setUp, fixtures.EngineTests.tearDown
    write, save, check = fixtures.EngineTests.write, fixtures.EngineTests.save, fixtures.EngineTests.check

    def test_source_parser_positions_and_method_body_classification(self):
        original = build_index(self.root, self.config)
        self.write("core/src/p/Api.java", "package p; public class Api { public int value() { return 7; } }")
        current = build_index(self.root, self.config)
        report = impact(original, current, self.config)
        self.assertEqual(report["changeLevels"]["core/src/p/Api.java"], "L0")
        self.assertEqual(report["affectedModules"], ["app", "core"])
        self.assertEqual(current["cacheHits"], 1)
        self.assertTrue(current["files"]["core/src/p/Api.java"]["declarations"])

    def test_syntax_error_is_not_claimed_semantic_failure(self):
        self.write("core/src/p/Api.java", "package p; public class Api { bad }")
        l1 = execute(self.root, self.model, level="L1", changed=[])
        self.assertEqual(l1["status"], "UNKNOWN", l1)
        final = execute(self.root, self.model, changed=[])
        self.assertEqual(final["status"], "FAIL", final)
        self.assertEqual(final["verificationLevel"], "L2")

    def test_constant_change_invalidates_downstream(self):
        self.write("core/src/p/Api.java", "package p; public class Api { public static final int N=1; public int value(){return N;} }")
        self.check()
        self.write("core/src/p/Api.java", "package p; public class Api { public static final int N=2; public int value(){return N;} }")
        result = self.check()
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["cacheHits"], 0)

    def test_generic_signature_change_detected(self):
        self.write("core/src/p/Api.java", "package p; public class Api<T extends Number> { public int value(){return 1;} }")
        self.check()
        self.write("core/src/p/Api.java", "package p; public class Api<T extends CharSequence> { public int value(){return 1;} }")
        result = self.check()
        self.assertEqual(result["cacheHits"], 0, result)

    def test_record_sealed_annotation_abi(self):
        self.write("core/src/p/Shape.java", "package p; public sealed interface Shape permits Circle {} record Circle(int radius) implements Shape {}")
        result = self.check()
        self.assertEqual(result["status"], "PASS", result)
        abi = json.loads((self.root / ".compile-fast/index/abi.json").read_text())
        self.assertEqual(abi["core"]["classes"]["p/Shape.class"]["attributes"]["PermittedSubclasses"], ["p/Circle"])
        self.assertEqual(abi["core"]["classes"]["p/Circle.class"]["attributes"]["Record"][0]["name"], "radius")

    def test_invalid_class_unknown(self):
        with self.assertRaises(EngineError):
            extract(b"broken")

    def test_build_pass_failure_and_no_authorization(self):
        gates = [{"name": "unit", "command": [sys.executable, "-c", "print('verified')"]}]
        write_json(self.root / ".compile-fast/gates.json", gates)
        denied = execute(self.root, self.model, level="L3", changed=[])
        self.assertEqual(denied["status"], "UNKNOWN")
        result = execute(self.root, self.model, level="L3", allow_build=True, changed=[])
        self.assertEqual(result["status"], "PASS", result)
        gates[0]["command"][-1] = "raise SystemExit(1)"
        self.assertEqual(verify_build(self.root, gates=gates)["status"], "FAIL")

    def test_unknown_context_automatic_build_fallback(self):
        self.config["contextComplete"] = False
        self.save()
        write_json(self.root / ".compile-fast/gates.json", [{"name": "full", "command": [sys.executable, "-c", "pass"]}])
        result = execute(self.root, self.model, allow_build=True, changed=[])
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual([s["verificationLevel"] for s in result["stages"]], ["L1", "L2", "L3"])

    def test_build_source_mutation_discards_pass(self):
        command = [sys.executable, "-c", "from pathlib import Path; Path('new.txt').write_text('changed')"]
        result = verify_build(self.root, gates=[{"name": "mutate", "command": command}])
        self.assertEqual(result["status"], "UNKNOWN", result)

    def test_process_timeout_and_cancellation(self):
        with self.assertRaises(EngineError) as raised:
            run([sys.executable, "-c", "import time; time.sleep(10)"], self.root, .15)
        self.assertEqual(raised.exception.code, "TIMEOUT")
        event = threading.Event()
        timer = threading.Timer(.15, event.set)
        timer.start()
        with cancellation(event), self.assertRaises(EngineError) as raised:
            run([sys.executable, "-c", "import time; time.sleep(10)"], self.root)
        timer.join()
        self.assertEqual(raised.exception.code, "CANCELLED")

    def test_repair_limit_and_metrics(self):
        self.write("core/src/p/Api.java", "package p; public class Api { broken }")
        for _ in range(3):
            result = execute(self.root, self.model, level="L2", changed=[], change_id="change", agent="a")
        self.assertEqual(result["repairPolicy"]["action"], "STOP_AND_REVIEW", result)
        self.assertEqual(metrics(self.root)["runs"], 3)

    def test_http_auth_submit_get_diagnostics_and_scope(self):
        scheduler = Scheduler({"test": self.root}, self.root / ".compile-fast/server", debounce=0)
        server = make_server(scheduler, "test-token-01234567890123456789", port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        address = f"http://127.0.0.1:{server.server_port}"
        headers = {"Authorization": "Bearer test-token-01234567890123456789", "Content-Type": "application/json"}
        try:
            with self.assertRaises(HTTPError) as raised:
                urlopen(address + "/health")
            self.assertEqual(raised.exception.code, 401)
            request = Request(address + "/api/verifications", data=json.dumps({"workspaceId": "test", "changedFiles": [], "verificationLevel": "L2"}).encode(), headers=headers)
            with urlopen(request) as response:
                task = json.load(response)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                with urlopen(Request(address + "/api/verifications/" + task["verificationId"], headers=headers)) as response:
                    task = json.load(response)
                if task["executionState"] == "COMPLETED":
                    break
                time.sleep(.05)
            self.assertEqual(task["result"]["status"], "PASS", task)
            with urlopen(Request(address + "/api/verifications/" + task["verificationId"] + "/diagnostics", headers=headers)) as response:
                self.assertEqual(json.load(response)["diagnostics"], [])
            with self.assertRaises(ValueError):
                scheduler.submit({"workspaceId": "outside"})
        finally:
            server.shutdown()
            worker.join()
            server.server_close()
            scheduler.close()

    def test_scheduler_supersedes_and_persists(self):
        state = self.root / ".compile-fast/server"
        scheduler = Scheduler({"test": self.root}, state, debounce=.25)
        try:
            one = scheduler.submit({"workspaceId": "test", "changedFiles": [], "verificationLevel": "L1"})
            two = scheduler.submit({"workspaceId": "test", "changedFiles": [], "verificationLevel": "L1"})
            self.assertEqual(scheduler.get(one["verificationId"])["executionState"], "SUPERSEDED")
            scheduler.cancel(two["verificationId"])
            self.assertEqual(scheduler.get(two["verificationId"])["executionState"], "CANCELLED")
        finally:
            scheduler.close()
        recovered = Scheduler({"test": self.root}, state)
        try:
            self.assertEqual(recovered.get(one["verificationId"])["executionState"], "SUPERSEDED")
        finally:
            recovered.close()


if __name__ == "__main__":
    unittest.main()
