"""Probe helpers answer True/False even when the wecom-cli binary is missing."""
import unittest

from online import wecom_cli


def _missing_binary(argv):
    raise FileNotFoundError("No such file or directory: 'wecom-cli'")


class MissingCliProbeTests(unittest.TestCase):
    def test_service_available_is_false_without_binary(self):
        self.assertFalse(wecom_cli.service_available("doc", runner=_missing_binary))

    def test_doc_command_supported_is_false_without_binary(self):
        self.assertFalse(wecom_cli.doc_command_supported("contents", runner=_missing_binary))

    def test_probes_still_detect_cli_errors(self):
        def _failing(argv):
            raise wecom_cli.WecomCliError(argv, 2, "", "unsupported")

        self.assertFalse(wecom_cli.service_available("doc", runner=_failing))
        self.assertFalse(wecom_cli.doc_command_supported("contents", runner=_failing))

    def test_probes_still_report_available_commands(self):
        self.assertTrue(wecom_cli.service_available("doc", runner=lambda argv: "ok"))
        self.assertTrue(wecom_cli.doc_command_supported("contents", runner=lambda argv: "ok"))


if __name__ == "__main__":
    unittest.main()
