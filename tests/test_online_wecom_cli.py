import importlib
import importlib.util
import subprocess
import unittest


def load_module(name):
    if importlib.util.find_spec(name) is None:
        raise AssertionError(f"{name} module should exist")
    return importlib.import_module(name)


class OnlineWecomCliTests(unittest.TestCase):
    def test_cli_generation_switches_at_1_1(self):
        wecom_cli = load_module("online.wecom_cli")

        self.assertEqual(wecom_cli.cli_generation("wecom-cli 0.1.9"), "legacy")
        self.assertEqual(wecom_cli.cli_generation("wecom-cli 1.1.0 (npm build)"), "current")
        self.assertEqual(wecom_cli.cli_generation("1.2.0"), "current")

    def test_cli_generation_rejects_unknown_version_text(self):
        wecom_cli = load_module("online.wecom_cli")

        with self.assertRaises(ValueError):
            wecom_cli.cli_generation("development build")

    def test_auth_status_uses_current_flag(self):
        wecom_cli = load_module("online.wecom_cli")
        calls = []

        def fake_runner(args):
            calls.append(list(args))
            return "authorized"

        result = wecom_cli.auth_status(runner=fake_runner, cli_version="1.2.0")

        self.assertEqual(result, "authorized")
        self.assertEqual(calls, [["auth", "show", "--status"]])

    def test_current_target_extracts_docid_from_wecom_url(self):
        wecom_cli = load_module("online.wecom_cli")

        result = wecom_cli.current_target_payload(
            url="https://doc.weixin.qq.com/sheet/DOCID?scode=share"
        )

        self.assertEqual(result, {"docid": "DOCID"})

    def test_current_target_rejects_non_wecom_url(self):
        wecom_cli = load_module("online.wecom_cli")

        with self.assertRaises(ValueError):
            wecom_cli.current_target_payload(url="https://example.com/sheet/DOCID")

    def test_run_json_passes_payload_as_json_argument(self):
        wecom_cli = load_module("online.wecom_cli")
        calls = []

        def fake_run(argv, capture_output, encoding, timeout, check):
            self.assertEqual(encoding, "utf-8")
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout='{"errcode":0}', stderr="")

        result = wecom_cli.run_json(
            ["doc", "create_doc"],
            {"doc_name": "Demo", "doc_type": 3},
            run=fake_run,
        )

        self.assertEqual(result, {"errcode": 0})
        self.assertEqual(calls[0][:4], ["wecom-cli", "doc", "create_doc", "--json"])
        self.assertIn('"doc_type":3', calls[0][4])

    def test_run_json_raises_structured_error_on_cli_failure(self):
        wecom_cli = load_module("online.wecom_cli")

        def fake_run(argv, capture_output, encoding, timeout, check):
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="unsupported")

        with self.assertRaises(wecom_cli.WecomCliError) as ctx:
            wecom_cli.run_json(["doc", "missing"], run=fake_run)

        self.assertEqual(ctx.exception.returncode, 2)
        self.assertIn("unsupported", str(ctx.exception))

    def test_run_json_prefers_current_structured_error(self):
        wecom_cli = load_module("online.wecom_cli")

        def fake_run(argv, capture_output, encoding, timeout, check):
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout='{"error":{"type":"AuthError","code":893201,"message":"unauthorized"}}',
                stderr="request failed",
            )

        with self.assertRaises(wecom_cli.WecomCliError) as ctx:
            wecom_cli.run_json(["doc", "contents", "get"], run=fake_run)

        self.assertIn("unauthorized", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
