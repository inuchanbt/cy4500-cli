"""Mixed SPR PPS / SPR AVS / EPR AVS regressions, without hardware."""
import csv
import importlib.util
import io
from pathlib import Path
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import analyze_sweep_csv as analyzer
from ezpd_protocol import (PDRequestTracker, SyncPDSample, SyncScopeSample,
                           analyze_programmable_transitions)
from pd_capture import load_pd_capture_csv

FIXED = (100 << 10) | 300
PPS = (3 << 30) | (210 << 17) | (33 << 8) | 100
SPR_AVS = (3 << 30) | (2 << 28) | (60 << 10) | 60
EPR_AVS = 0xD3C096F0


def packet(index, message, stamp, data=b"", **kwargs):
    return SyncPDSample(index, index + 1, message, stamp, stamp + 500, None, data, **kwargs)


def rdo(position, voltage, unit=0.025):
    return struct.pack("<I", (position << 28) | (round(voltage / unit) << 9) | 60)


def source_caps(index=0, stamp=0, objects=(FIXED, PPS, SPR_AVS), **kwargs):
    return packet(index, "SOURCE_CAPABILITIES", stamp, struct.pack("<" + "I" * len(objects), *objects), **kwargs)


def mixed_packets():
    rows = [source_caps()]
    for stamp, mode, target in [(100_000, "SPR_PPS", 9), (500_000, "SPR_AVS", 12),
                                (900_000, "EPR_AVS", 24), (1_300_000, "EPR_AVS", 28),
                                (1_700_000, "EPR_AVS", 24)]:
        data = rdo(2 if mode == "SPR_PPS" else 3 if mode == "SPR_AVS" else 8,
                   target, 0.02 if mode == "SPR_PPS" else 0.025)
        if mode == "EPR_AVS":
            data += struct.pack("<I", EPR_AVS)
        message = "EPR_REQUEST" if mode == "EPR_AVS" else "REQUEST"
        rows += [packet(len(rows), message, stamp, data),
                 packet(len(rows) + 1, "ACCEPT", stamp + 5_000),
                 packet(len(rows) + 2, "PS_RDY", stamp + 50_000)]
    return rows


def mixed_scope():
    rows = []
    steps = [(100_000, 9), (500_000, 12), (900_000, 24), (1_300_000, 28), (1_700_000, 24)]
    for stamp in range(0, 2_100_001, 1_000):
        voltage = 5
        for start, target in steps:
            if stamp < start:
                break
            voltage += (target - voltage) * min(1, (stamp - start) / 40_000)
        rows.append(SyncScopeSample(stamp, voltage, 2))
    return rows


def write_mixed_capture(folder, legacy=False):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    pd_path, scope_path = folder / "mixed.csv", folder / "mixed.scope.csv"
    rows = []
    for p in mixed_packets():
        data = p.data.hex(" ") if legacy else "0x0" + "".join(
            f" 0x{int.from_bytes(p.data[n:n+4], 'little'):X}" for n in range(0, len(p.data), 4))
        rows.append({"Sno": p.sno, "Ok": "1" if legacy else "OK", "SOP": "SOP",
                     "Message": p.message, "Start Time": p.start_us, "End Time": p.end_us,
                     "Data": data, "Vbus(V)": 5 if legacy else 5000})
    analyzer.write_csv(pd_path, rows)
    analyzer.write_csv(scope_path, [{"Timestamp(us)": s.timestamp_us, "Vbus(V)": s.vbus_V,
                                    "Ibus(A)": s.ibus_A} for s in mixed_scope()])
    return pd_path, scope_path


class ProgrammableAnalysisTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def run_cli(self, *args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return analyzer.main(list(map(str, args)))

    def test_request_units_and_latest_source_pdo_identity(self):
        tracker = PDRequestTracker()
        tracker.observe(source_caps())
        pps = tracker.observe(packet(1, "REQUEST", 100, rdo(2, 9, 0.02)))
        avs = tracker.observe(packet(2, "REQUEST", 200, rdo(3, 9)))
        self.assertEqual((pps.mode, pps.target_voltage_V, pps.requested_current_A), ("SPR_PPS", 9, 3))
        self.assertEqual((avs.mode, avs.target_voltage_V), ("SPR_AVS", 9))
        tracker.observe(source_caps(3, 300, (FIXED, SPR_AVS)))
        changed = tracker.observe(packet(4, "REQUEST", 400, rdo(2, 9)))
        self.assertEqual((changed.mode, changed.target_voltage_V), ("SPR_AVS", 9))

    def test_missing_malformed_reset_and_sop_context_is_never_guessed(self):
        tracker = PDRequestTracker()
        request = packet(1, "REQUEST", 100, rdo(2, 9, 0.02))
        self.assertIsNone(tracker.observe(request))
        tracker.observe(source_caps(sop="SOP'"))
        self.assertIsNone(tracker.observe(request))
        tracker.observe(source_caps(ok=False))
        self.assertIsNone(tracker.observe(request))
        tracker.observe(source_caps())
        self.assertIsNotNone(tracker.observe(request))
        tracker.observe(packet(2, "SOURCE_CAPABILITIES", 200, b"bad"))
        self.assertIsNone(tracker.observe(request))
        for reset in ("HARD_RESET", "SOFT_RESET", "DETACH", "EPR_MODE"):
            tracker.observe(source_caps())
            tracker.observe(packet(2, reset, 200))
            self.assertIsNone(tracker.observe(request))
        tracker.observe(source_caps())
        self.assertIsNone(tracker.observe(packet(3, "REQUEST", 300, rdo(7, 9))))
        self.assertIsNone(tracker.observe(packet(4, "REQUEST", 400, rdo(1, 9))))

    def test_epr_request_resolves_embedded_spr_and_epr_pdos_without_context(self):
        tracker = PDRequestTracker()
        for position, pdo, mode, unit in ((5, PPS, "SPR_PPS", 0.02),
                                          (6, SPR_AVS, "SPR_AVS", 0.025),
                                          (8, EPR_AVS, "EPR_AVS", 0.025)):
            with self.subTest(mode=mode):
                data = rdo(position, 12, unit) + struct.pack("<I", pdo)
                result = tracker.observe(packet(1, "EPR_REQUEST", 100, data))
                self.assertEqual((result.mode, result.target_voltage_V, result.object_position), (mode, 12, position))
                rows = [packet(1, "EPR_REQUEST", 100_000, data),
                        packet(2, "ACCEPT", 105_000), packet(3, "PS_RDY", 150_000)]
                self.assertEqual(analyze_programmable_transitions(rows, [])[0].request_mode, mode)
        for position, pdo in ((0, PPS), (8, PPS), (5, EPR_AVS), (12, EPR_AVS), (1, FIXED)):
            self.assertIsNone(tracker.observe(packet(1, "EPR_REQUEST", 100,
                rdo(position, 12) + struct.pack("<I", pdo))))

    def test_mixed_family_transition_analysis(self):
        results = analyze_programmable_transitions(mixed_packets(), mixed_scope())
        self.assertEqual([r.request_mode for r in results], ["SPR_PPS", "SPR_AVS", "EPR_AVS", "EPR_AVS", "EPR_AVS"])
        self.assertEqual([r.target_voltage_V for r in results], [9, 12, 24, 28, 24])
        self.assertEqual([r.direction for r in results], ["up", "up", "up", "up", "down"])
        for result in results:
            self.assertEqual(result.accept_latency_us, 5_000)
            self.assertEqual(result.ps_rdy_latency_us, 50_000)
            self.assertIsNotNone(result.settling_us)
            self.assertIsNotNone(result.observed_settling_us)

    def test_utility_reset_markers_end_context_contracts_and_response_matching(self):
        for reset in (packet(4, "DETACH", 200_000, sop=""),
                      packet(4, "C_RSVD0", 200_000, sop="HARD_RESET")):
            with self.subTest(reset=reset):
                rows = mixed_packets()[:4] + [reset]
                tracker = PDRequestTracker()
                for row in rows:
                    tracker.observe(row)
                self.assertIsNone(tracker.observe(packet(5, "REQUEST", 250_000, rdo(2, 9, 0.02))))
                interrupted = [rows[0], rows[1], reset,
                               packet(5, "ACCEPT", 205_000), packet(6, "PS_RDY", 250_000)]
                self.assertIsNone(analyze_programmable_transitions(interrupted, [])[0].ps_rdy_start_us)
                pd_path = self.folder / "reset.csv"
                analyzer.write_csv(pd_path, [{"Message": p.message, "Start Time": p.start_us,
                    "Data": p.data.hex(" "), "Ok": "DETACH" if p.message == "DETACH" else "1",
                    "SOP": p.sop} for p in rows])
                events = analyzer.request_events(pd_path, include_metadata=True)
                self.assertEqual(events[-1], (200_000, None, None, None, None))
                self.assertEqual(events[0][3], "SPR_PPS")

    def test_response_matching_stops_at_any_request_reset_reject_or_wait(self):
        for boundary in ("REQUEST", "EPR_REQUEST", "HARD_RESET", "DETACH", "REJECT", "WAIT"):
            with self.subTest(boundary=boundary):
                rows = [source_caps(), packet(1, "REQUEST", 100_000, rdo(2, 9, 0.02)),
                        packet(2, boundary, 101_000), packet(3, "ACCEPT", 105_000),
                        packet(4, "PS_RDY", 150_000)]
                result = analyze_programmable_transitions(rows, [])[0]
                self.assertIsNone(result.accept_start_us)
                self.assertIsNone(result.ps_rdy_start_us)
                if boundary in {"REJECT", "WAIT"}:
                    self.assertIn("request_" + boundary.lower(), result.flags)

    def test_error_and_cable_responses_are_not_matched(self):
        rows = [source_caps(), packet(1, "REQUEST", 100_000, rdo(2, 9, 0.02)),
                packet(2, "ACCEPT", 101_000, ok=False), packet(3, "PS_RDY", 102_000, sop="SOP'"),
                packet(4, "ACCEPT", 105_000), packet(5, "PS_RDY", 150_000)]
        result = analyze_programmable_transitions(rows, [])[0]
        self.assertEqual(result.accept_latency_us, 5_000)
        self.assertEqual(result.ps_rdy_latency_us, 50_000)

    def test_utility_and_legacy_csvs_resolve_all_families_identically(self):
        events = []
        for legacy in (False, True):
            pd_path, _ = write_mixed_capture(self.folder / str(legacy), legacy)
            events.append(analyzer.request_events(pd_path, include_metadata=True))
            self.assertEqual(load_pd_capture_csv(pd_path)[0].vbus_V, 5)
        self.assertEqual(events[0], events[1])
        self.assertEqual([event[3] for event in events[0]], ["SPR_PPS", "SPR_AVS", "EPR_AVS", "EPR_AVS", "EPR_AVS"])

    def test_family_filter_metadata_return_legs_and_bilingual_reports(self):
        pd_path, _ = write_mixed_capture(self.folder)
        for mode, count in (("all", 5), ("spr-pps", 1), ("spr-avs", 1), ("epr-avs", 3)):
            with self.subTest(mode=mode):
                prefix = self.folder / mode
                self.assertEqual(self.run_cli(pd_path, "--group-by", "request", "--request-mode", mode,
                                              "--no-plots", "--report-lang", "both", "--out", prefix), 0)
                with Path(f"{prefix}_summary.csv").open(encoding="utf-8-sig", newline="") as fp:
                    rows = list(csv.DictReader(fp))
                self.assertEqual(len(rows), count)
                if mode != "all":
                    self.assertEqual({row["request_mode"] for row in rows}, {mode.upper().replace("-", "_")})
                else:
                    self.assertEqual([int(row["sweep_leg"]) for row in rows], [1, 2, 3, 3, 4])
                en = Path(f"{prefix}_human_report_en.txt").read_text(encoding="utf-8")
                ja = Path(f"{prefix}_human_report_ja.txt").read_text(encoding="utf-8")
                self.assertIn("Segments by request mode:", en)
                self.assertIn("方式別の集計区間数:", ja)
                self.assertNotIn("Non-AVS contracts", en)

    def test_transition_exports_include_mode_and_pdo(self):
        from cy4500_cli import _write_transition_outputs
        results = analyze_programmable_transitions(mixed_packets(), [])
        _write_transition_outputs(results, csv_path=self.folder / "transitions.csv",
                                  summary_path=self.folder / "transitions.txt", settings={},
                                  human_csv_path=self.folder / "summary.csv", human_text_path=self.folder / "summary.txt")
        for name in ("transitions.csv", "summary.csv"):
            with (self.folder / name).open(encoding="utf-8-sig", newline="") as fp:
                rows = list(csv.DictReader(fp))
            self.assertEqual(rows[0]["request_mode"], "SPR_PPS")
            self.assertEqual(rows[1]["request_message"], "REQUEST")
            self.assertEqual(rows[2]["object_position"], "8")

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "Optional plotting dependency")
    def test_png_generation_for_mixed_families(self):
        pd_path, _ = write_mixed_capture(self.folder)
        self.assertEqual(self.run_cli(pd_path, "--group-by", "request", "--out", self.folder / "plots", "--no-print"), 0)
        from PIL import Image
        for suffix in ("voltage_actual", "current", "power", "voltage_pp", "voltage_error"):
            for prefix in ("plots_", "plots_sweep_"):
                with Image.open(self.folder / f"{prefix}{suffix}.png") as png:
                    self.assertGreater(png.width, 100)
                    png.verify()


if __name__ == "__main__":
    unittest.main()
