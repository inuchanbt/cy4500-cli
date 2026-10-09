import csv
import io
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import analyze_sweep_csv as analyzer


class SweepAnalyzerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def write(self, name, rows):
        path = self.folder / name
        analyzer.write_csv(path, rows)
        return path

    def run_cli(self, *args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return analyzer.main(list(map(str, args)))

    def scope(self, name="run.scope.csv"):
        return self.write(name, [
            {"Timestamp(us)": 100 + t * 1_000_000, "Vbus(V)": v, "Ibus(A)": i}
            for t, v, i in [(0, 24, -0.1), (0.1, 24.02, 1), (0.2, 24.04, 1.1),
                            (1, 28, 2), (1.1, 28.02, 2.1)]])

    def read(self, name):
        with (self.folder / name).open(encoding="utf-8-sig", newline="") as fp:
            return list(csv.DictReader(fp))

    def test_scope_units_statistics_and_absent_targets(self):
        path = self.scope()
        self.assertEqual(self.run_cli(path, "--no-plots", "--no-print"), 0)
        rows = self.read("run.scope_analysis_summary.csv")
        self.assertEqual([r["samples"] for r in rows], ["3", "2"])
        self.assertAlmostEqual(float(rows[0]["voltage_mean"]), 24.02)
        self.assertAlmostEqual(float(rows[0]["voltage_pp_mv"]), 40)
        self.assertEqual(rows[0]["target_voltage_v"], "")
        self.assertEqual(rows[0]["voltage_error_v"], "")
        samples = self.read("run.scope_analysis_normalized.csv")
        self.assertEqual(samples[0]["actual_current_a"], "-0.1")
        self.assertAlmostEqual(float(samples[0]["actual_power_w"]), -2.4)

    def test_filter_then_discard_and_explicit_fixed_target(self):
        path = self.scope()
        self.assertEqual(self.run_cli(path, "--no-plots", "--settle-seconds", 0.05,
                                      "--discard-first", 1, "--min-samples", 1,
                                      "--target-voltage", 24, "--end", 1), 0)
        row = self.read("run.scope_analysis_summary.csv")[0]
        self.assertEqual(row["samples"], "1")
        self.assertAlmostEqual(float(row["voltage_error_v"]), 0.04)

    def test_live_uses_seconds_and_instant_current(self):
        path = self.write("live.csv", [
            {"monotonic_s": t, "vbus_V": "5", "ibus_A": "-0.1",
             "ibus_median_A": "99"} for t in (500, 500.1)])
        self.assertEqual(self.run_cli(path, "--no-plots"), 0)
        row = self.read("live_analysis_summary.csv")[0]
        self.assertAlmostEqual(float(row["end_elapsed_s"]), 0.1)
        self.assertEqual(float(row["current_mean"]), -0.1)
        self.assertAlmostEqual(float(row["power_mean"]), -0.5)

    def pd_row(self, stamp, message, data="", ok="OK", sop="SOP"):
        return {"Start Time": stamp, "Message": message, "Data": data, "Ok": ok, "SOP": sop}

    def avs_data(self, voltage, legacy=False):
        # EPR AVS APDO: 15..48 V, 140 W; request in 25 mV / 50 mA units.
        pdo = (3 << 30) | (1 << 28) | (480 << 17) | (150 << 8) | 140
        rdo = (8 << 28) | (round(voltage / 0.025) << 9) | 100
        if legacy:
            return (rdo.to_bytes(4, "little") + pdo.to_bytes(4, "little")).hex(" ")
        return f"0x2089 0x{rdo:X} 0x{pdo:X}"

    def test_avs_success_retries_unsupported_contract_and_return_leg(self):
        rows = []
        for t, v in [(100, 24), (200, 24), (300, 28), (400, 24)]:
            rows += [self.pd_row(t, "EPR_REQUEST", self.avs_data(v)),
                     self.pd_row(t + 10, "ACCEPT"), self.pd_row(t + 20, "PS_RDY")]
        # A rejected request and SOP' traffic must not change the active target.
        rows += [self.pd_row(430, "EPR_REQUEST", self.avs_data(36)),
                 self.pd_row(435, "REJECT"), self.pd_row(440, "PS_RDY"),
                 self.pd_row(450, "EPR_REQUEST", self.avs_data(36), sop="SOP'"),
                 self.pd_row(460, "REQUEST"), self.pd_row(470, "ACCEPT"),
                 self.pd_row(480, "PS_RDY")]
        path = self.write("pd.csv", rows)
        events = analyzer.request_events(path)
        self.assertEqual([(e[0], e[1]) for e in events], [(120, 24), (320, 28), (420, 24), (480, None)])
        self.assertEqual(events[0][2], 5)

    def test_legacy_byte_payload_and_invalid_packets(self):
        path = self.write("pd.csv", [
            self.pd_row(100, "EPR_REQUEST", self.avs_data(28, legacy=True), ok="1"),
            self.pd_row(110, "ACCEPT", ok="1"), self.pd_row(120, "PS_RDY", ok="1"),
            self.pd_row(200, "EPR_REQUEST", self.avs_data(36), ok="ER_CRC"),
            self.pd_row(210, "ACCEPT"), self.pd_row(220, "PS_RDY")])
        self.assertEqual(analyzer.request_events(path), [(120, 28, 5)])

    def test_pd_input_resolves_waveform_without_using_millivolts_as_volts(self):
        self.scope()
        path = self.write("run.csv", [self.pd_row(0, "EPR_REQUEST", self.avs_data(24))])
        self.assertEqual(self.run_cli(path, "--no-plots"), 0)
        self.assertAlmostEqual(float(self.read("run.scope_analysis_summary.csv")[0]["voltage_mean"]), 24.02)

    def test_request_grouping_keeps_same_voltage_return_separate(self):
        scope = self.write("avs.scope.csv", [
            {"Timestamp(us)": t, "Vbus(V)": v}
            for t, v in [(0, 5), (150, 24), (160, 24), (350, 28), (360, 28), (550, 24), (560, 24)]])
        rows = []
        for t, v in [(100, 24), (300, 28), (500, 24)]:
            rows += [self.pd_row(t, "EPR_REQUEST", self.avs_data(v)),
                     self.pd_row(t + 10, "ACCEPT"), self.pd_row(t + 20, "PS_RDY")]
        self.write("avs.csv", rows)
        self.assertEqual(self.run_cli(scope, "--group-by", "request", "--no-plots", "--report-lang", "en"), 0)
        summary = self.read("avs.scope_analysis_summary.csv")
        self.assertEqual([float(r["target_voltage_v"]) for r in summary], [24, 28, 24])
        self.assertEqual([r["samples"] for r in summary], ["2", "2", "2"])
        self.assertEqual(summary[0]["current_mean"], "")
        report = (self.folder / "avs.scope_analysis_human_report.txt").read_text(encoding="utf-8")
        self.assertIn("Successful AVS Accept / PS_RDY contracts", report)
        self.assertIn("without clock correction", report)
        self.assertIn("not a load-current setpoint", report)
        self.assertNotIn("Current: mean", report)
        self.assertTrue(report.isascii())

    def test_existing_outputs_and_input_are_protected_even_with_force(self):
        path = self.scope()
        before = path.read_bytes()
        self.assertEqual(self.run_cli(path, "--no-plots"), 0)
        output = self.folder / "run.scope_analysis_summary.csv"
        output.write_text("keep me", encoding="utf-8")
        self.assertEqual(self.run_cli(path, "--no-plots"), 1)
        self.assertEqual(output.read_text(), "keep me")
        self.assertEqual(self.run_cli(path, "--no-plots", "--force"), 0)
        self.assertEqual(path.read_bytes(), before)
        # Name the input exactly like one of the intended output files.
        protected = self.scope("protected_normalized.csv")
        protected_before = protected.read_bytes()
        self.assertEqual(self.run_cli(protected, "--out", self.folder / "protected", "--force", "--no-plots"), 1)
        self.assertEqual(protected.read_bytes(), protected_before)

    def test_invalid_and_missing_values_are_not_zero_filled(self):
        path = self.write("bad.scope.csv", [
            {"Timestamp(us)": "nan", "Vbus(V)": 100},
            {"Timestamp(us)": 0, "Vbus(V)": "inf"},
            {"Timestamp(us)": 1, "Vbus(V)": 5},
            {"Timestamp(us)": 2, "Vbus(V)": 5.01}])
        samples, _, skipped = analyzer.load_samples(path)
        self.assertEqual(skipped, 2)
        self.assertEqual(len(samples), 2)
        self.assertIsNone(samples[0].current)

    def test_pd_without_scope_and_empty_filters_fail_before_output(self):
        path = self.write("run.csv", [self.pd_row(0, "PS_RDY")])
        self.assertEqual(self.run_cli(path, "--no-plots"), 1)
        scope = self.scope()
        self.assertEqual(self.run_cli(scope, "--start", 100, "--no-plots"), 1)
        self.assertFalse((self.folder / "run.scope_analysis_summary.csv").exists())

    def test_report_language_preserves_measurement_results(self):
        scope = self.scope()
        for language, options in (("en", []), ("ja", ["--report-lang", "ja"])):
            self.assertEqual(self.run_cli(scope, "--no-plots", "--out", self.folder / language, *options), 0)
        japanese = (self.folder / "ja_human_report.txt").read_text(encoding="utf-8")
        english = (self.folder / "en_human_report.txt").read_text(encoding="utf-8")
        self.assertIn("CY4500 CSV かんたんレポート", japanese)
        self.assertIn("CY4500 CSV Measurement Report", english)
        self.assertIn("Input sample interval: median 100.0000 ms / maximum 800.0000 ms", english)
        self.assertIn("Voltage: mean 25.6160 V / minimum 24.0000 V / maximum 28.0200 V", english)
        self.assertIn("target voltage is unknown", english)
        self.assertIn("not directly comparable", english)
        self.assertIn("PNG files were not generated", english)
        self.assertTrue(english.isascii())
        self.assertEqual((self.folder / "ja_summary.csv").read_bytes(), (self.folder / "en_summary.csv").read_bytes())
        self.assertEqual((self.folder / "ja_normalized.csv").read_bytes(), (self.folder / "en_normalized.csv").read_bytes())

    def test_both_reports_and_no_report_override(self):
        scope = self.scope()
        self.assertEqual(self.run_cli(scope, "--no-plots", "--report-lang", "both"), 0)
        prefix = self.folder / "run.scope_analysis"
        self.assertIn("かんたんレポート", Path(f"{prefix}_human_report_ja.txt").read_text(encoding="utf-8"))
        self.assertIn("Measurement Report", Path(f"{prefix}_human_report_en.txt").read_text(encoding="utf-8"))
        self.assertFalse(Path(f"{prefix}_human_report.txt").exists())
        self.assertEqual(self.run_cli(scope, "--no-plots", "--report-lang", "both", "--no-report", "--out", self.folder / "silent"), 0)
        self.assertEqual(list(self.folder.glob("silent_human_report*.txt")), [])
        # Either language's existing report protects the full output set.
        Path(f"{prefix}_normalized.csv").unlink()
        Path(f"{prefix}_summary.csv").unlink()
        self.assertEqual(self.run_cli(scope, "--no-plots", "--report-lang", "both"), 1)
        self.assertFalse(Path(f"{prefix}_summary.csv").exists())


if __name__ == "__main__":
    unittest.main()
