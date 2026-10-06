import csv
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import cy4500_cli as cli
from ezpd_protocol import decode_capture_record


class CaptureOptionsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.prefix = Path(folder.name) / "session"
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        # Keep command output out of the test runner while asserting messages.
        self.stdout_redirect = redirect_stdout(self.stdout)
        self.stderr_redirect = redirect_stderr(self.stderr)
        self.stdout_redirect.__enter__()
        self.stderr_redirect.__enter__()
        self.addCleanup(self.stdout_redirect.__exit__, None, None, None)
        self.addCleanup(self.stderr_redirect.__exit__, None, None, None)
        device_patch = patch.object(cli, "CY4500EPR")
        self.device = device_patch.start()
        self.addCleanup(device_patch.stop)
        self.device.return_value.__enter__.return_value.capture.side_effect = self.capture
        analysis_patch = patch.object(cli, "_run_sync_analysis", return_value=[])
        self.analysis = analysis_patch.start()
        self.addCleanup(analysis_patch.stop)
        self.raw_records = []
        for index, message_type in enumerate((1, 3), start=1):
            raw = bytearray(64)
            struct.pack_into("<IIIII", raw, 0, index, 400, index * 100,
                             index * 100 + 20, (1 << 31) | message_type)
            self.raw_records.append(bytes(raw))

    def capture(self, seconds, **callbacks):
        for index, raw in enumerate(self.raw_records, start=1):
            callbacks["record_callback"](
                cli.CaptureRecord(index, raw, decode_capture_record(raw))
            )
        stats = cli.CaptureStats(seconds, records=len(self.raw_records))
        callbacks["status_callback"](stats)
        return stats

    def run_capture(self, *options):
        return cli.main(["capture", "--out-prefix", str(self.prefix), *options])

    def test_scope_analysis_is_opt_in(self):
        self.assertEqual(self.run_capture("--scope"), 0)
        self.analysis.assert_not_called()
        self.assertEqual(self.run_capture("--scope", "--analyze-transitions", "--force"), 0)
        self.analysis.assert_called_once()

    def test_analysis_requires_scope_before_writing_or_opening_device(self):
        self.assertEqual(self.run_capture("--analyze-transitions"), 2)
        self.assertIn("requires --scope", self.stderr.getvalue())
        self.device.assert_not_called()
        self.assertEqual(list(self.prefix.parent.iterdir()), [])

    def test_existing_session_outputs_are_preserved_before_device_access(self):
        # Optional outputs reserve a session prefix even when their flags are off.
        for suffix in (".xfers.bin", ".records.bin", ".records.hex.txt", ".records.jsonl",
                       ".csv", ".summary.txt", ".ccgx3", ".scope.csv", ".scope.xfers.bin",
                       ".transitions.csv", ".transitions.txt",
                       ".transition_summary.csv", ".transition_summary.txt"):
            with self.subTest(suffix=suffix):
                existing = self.prefix.with_suffix(suffix)
                existing.write_bytes(b"previous session")
                self.assertEqual(self.run_capture(), 2)
                self.assertEqual(existing.read_bytes(), b"previous session")
                self.assertEqual(list(self.prefix.parent.iterdir()), [existing])
                self.device.assert_not_called()
                existing.unlink()
        self.assertIn("--force", self.stderr.getvalue())

    def test_force_overwrites_capture_outputs(self):
        for suffix in (".records.bin", ".csv", ".ccgx3", ".scope.csv", ".scope.xfers.bin"):
            self.prefix.with_suffix(suffix).write_bytes(b"previous session")
        self.assertEqual(self.run_capture("--force", "--scope", "--scope-raw", "--ccgx3"), 0)
        self.device.assert_called_once()
        self.assertEqual(self.prefix.with_suffix(".records.bin").read_bytes(),
                         b"".join(self.raw_records))
        self.assertTrue(self.prefix.with_suffix(".ccgx3").read_bytes().startswith(b"PK"))
        self.assertNotEqual(self.prefix.with_suffix(".scope.csv").read_bytes(), b"previous session")
        self.assertEqual(self.prefix.with_suffix(".scope.xfers.bin").read_bytes(), b"")

    def test_hide_goodcrc_preserves_records_and_other_console_output(self):
        self.assertEqual(self.run_capture(), 0)
        self.assertIn("GOODCRC", self.stdout.getvalue())
        self.stdout.seek(0)
        self.stdout.truncate()
        self.assertEqual(self.run_capture("--hide-goodcrc", "--force", "--ccgx3"), 0)
        output = self.stdout.getvalue()
        self.assertNotIn("GOODCRC", output)
        self.assertIn("ACCEPT", output)
        self.assertIn("[status]", output)
        self.assertIn("CAPTURE SUMMARY", output)
        self.assertEqual(self.prefix.with_suffix(".records.bin").read_bytes(),
                         b"".join(self.raw_records))
        rows = [json.loads(line) for line in
                self.prefix.with_suffix(".records.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["decoded"]["message_name"] for row in rows], ["GOODCRC", "ACCEPT"])
        with self.prefix.with_suffix(".csv").open(encoding="utf-8", newline="") as fp:
            self.assertEqual(len(list(csv.reader(fp))), 3)

    def test_quiet_still_suppresses_all_record_and_status_lines(self):
        self.assertEqual(self.run_capture("--quiet", "--hide-goodcrc"), 0)
        self.assertNotIn("REC #", self.stdout.getvalue())
        self.assertNotIn("[status]", self.stdout.getvalue())

    def test_analysis_outputs_require_force_but_input_capture_can_share_prefix(self):
        self.prefix.with_suffix(".csv").write_text("input", encoding="utf-8")
        arguments = ["analyze-sync", "--pd-csv", str(self.prefix.with_suffix(".csv")),
                     "--scope-csv", str(self.prefix.with_suffix(".scope.csv")),
                     "--out-prefix", str(self.prefix)]
        self.assertEqual(cli.main(arguments), 0)
        self.analysis.reset_mock()
        for suffix in (".transitions.csv", ".transitions.txt",
                       ".transition_summary.csv", ".transition_summary.txt"):
            with self.subTest(suffix=suffix):
                existing = self.prefix.with_suffix(suffix)
                existing.write_bytes(b"previous analysis")
                self.assertEqual(cli.main(arguments), 2)
                self.analysis.assert_not_called()
                self.assertEqual(existing.read_bytes(), b"previous analysis")
                self.assertEqual(cli.main([*arguments, "--force"]), 0)
                self.analysis.assert_called_once()
                self.analysis.reset_mock()
                existing.unlink()

    def test_export_outputs_require_force_and_input_remains_protected(self):
        source = self.prefix.parent / "input.records.bin"
        source.write_bytes(b"".join(self.raw_records))
        arguments = ["export-gui", "--records", str(source),
                     "--out-prefix", str(self.prefix)]
        for suffix in (".csv", ".ccgx3"):
            with self.subTest(suffix=suffix):
                existing = self.prefix.with_suffix(suffix)
                existing.write_bytes(b"previous export")
                self.assertEqual(cli.main(arguments), 2)
                self.assertEqual(existing.read_bytes(), b"previous export")
                self.assertEqual(len(list(self.prefix.parent.iterdir())), 2)
                existing.unlink()
        self.prefix.with_suffix(".csv").write_bytes(b"previous export")
        self.assertEqual(cli.main([*arguments, "--force"]), 0)
        self.assertNotEqual(self.prefix.with_suffix(".csv").read_bytes(), b"previous export")
        self.assertTrue(self.prefix.with_suffix(".ccgx3").exists())
        protected = self.prefix.parent / "protected.csv"
        protected.write_bytes(self.raw_records[0])
        self.assertEqual(cli.main(["export-gui", "--records", str(protected),
                                   "--out-prefix", str(protected), "--force"]), 2)
        self.assertEqual(protected.read_bytes(), self.raw_records[0])


if __name__ == "__main__":
    unittest.main()
