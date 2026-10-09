#!/usr/bin/env python3
"""Offline CY4500 scope/live CSV summaries, plots and Japanese/English reports.

Inspired by the ASD-PD31 analyzer workflow. Only PNG output needs matplotlib;
CSV analysis never imports the USB controller or opens hardware.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
import csv
from dataclasses import dataclass
import math
from pathlib import Path
import statistics
import sys

__version__ = "1.2.0"


@dataclass
class Sample:
    row: int
    time_us: float
    voltage: float
    current: float | None
    power: float | None
    cc1: float | None
    cc2: float | None
    elapsed: float = 0.0
    segment: int = 0
    target: float | None = None
    request_current: float | None = None
    request_mode: str | None = None
    object_position: int | None = None


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def load_samples(path: Path):
    """Use explicit header units, never the ambiguous PD Vbus(V) column."""
    samples = []
    skipped = 0
    with path.open(encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        headers = set(reader.fieldnames or [])
        if {"Timestamp(us)", "Vbus(V)"} <= headers:
            kind, time_col, scale = "scope", "Timestamp(us)", 1
            voltage_col, current_col = "Vbus(V)", "Ibus(A)"
            power_col, cc1_col, cc2_col = "Power(W)", "CC1(V)", "CC2(V)"
        elif {"monotonic_s", "vbus_V"} <= headers:
            kind, time_col, scale = "live-status", "monotonic_s", 1_000_000
            voltage_col, current_col = "vbus_V", "ibus_A"
            power_col, cc1_col, cc2_col = "power_instant_W", "cc1_V", "cc2_V"
        else:
            raise ValueError("CY4500 scope CSV / live-status CSV が必要です。PD CSVの場合は同名の .scope.csv を置いてください。")
        for index, row in enumerate(reader, 1):
            stamp, voltage = number(row.get(time_col)), number(row.get(voltage_col))
            if stamp is None or voltage is None:
                skipped += 1
                continue
            current, power = number(row.get(current_col)), number(row.get(power_col))
            if power is None and current is not None:
                power = voltage * current
            samples.append(Sample(index, stamp * scale, voltage, current, power,
                                  number(row.get(cc1_col)), number(row.get(cc2_col))))
    if not samples:
        raise ValueError("有効な電圧・時刻データがありません。")
    samples.sort(key=lambda sample: sample.time_us)
    origin = samples[0].time_us
    for sample in samples:
        sample.elapsed = (sample.time_us - origin) / 1_000_000
    return samples, kind, skipped


def resolve_input(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as fp:
        headers = set(next(csv.reader(fp), []))
    if {"Message", "Start Time", "Data"} <= headers:
        scope = path.with_suffix(".scope.csv")
        if not scope.is_file():
            raise ValueError(f"PD CSV単独では波形を集計できません。同セッションの波形CSVが必要です: {scope}")
        return scope, path
    pd_path = Path(str(path)[:-len(".scope.csv")] + ".csv") if str(path).endswith(".scope.csv") else None
    return path, pd_path


def request_events(path: Path, include_metadata=False):
    """Successful SPR PPS / SPR AVS / EPR AVS contracts, without clock fitting.

    Repeated identical contracts share one segment. Missing source PDO context
    and unsupported contracts invalidate the active programmable contract.
    """
    from ezpd_protocol import PDRequestTracker, is_pd_session_reset
    from pd_capture import load_pd_capture_csv

    tracker = PDRequestTracker()
    pending = None
    accepted = False
    active = None
    events = []
    for packet in load_pd_capture_csv(path):
        decoded = tracker.observe(packet)
        stamp, message = packet.start_us, packet.message
        if is_pd_session_reset(packet):
            pending, accepted, active = None, False, None
            events.append((stamp, None, None, None, None))
            continue
        if not packet.ok or packet.sop != "SOP":
            continue
        if message in {"REQUEST", "EPR_REQUEST"}:
            pending = ((None, None, None, None) if decoded is None else
                       (decoded.target_voltage_V, decoded.requested_current_A, decoded.mode, decoded.object_position))
            accepted = False
        elif message == "ACCEPT" and pending is not None:
            accepted = True
        elif message == "PS_RDY" and pending is not None and accepted:
            if pending != active:
                events.append((stamp, *pending))
                active = pending
            pending, accepted = None, False
        elif message in {"REJECT", "WAIT"}:
            pending, accepted = None, False
    events.sort(key=lambda event: event[0])
    return events if include_metadata else [event[:3] for event in events]


def group_samples(samples, args, events):
    grouped = {}
    event_times = [event[0] for event in events]
    for sample in samples:
        if args.start is not None and sample.elapsed < args.start:
            continue
        if args.end is not None and sample.elapsed >= args.end:
            continue
        if args.group_by == "request":
            event_index = bisect_right(event_times, sample.time_us) - 1
            if event_index < 0 or events[event_index][1] is None:
                continue
            event = events[event_index]
            stamp, sample.target, sample.request_current = event[:3]
            if len(event) > 3:
                sample.request_mode, sample.object_position = event[3:5]
            request_mode = getattr(args, "request_mode", "all")
            if request_mode != "all" and sample.request_mode != request_mode.upper().replace("-", "_"):
                continue
            sample.segment = event_index + 1
            since_start = (sample.time_us - stamp) / 1_000_000
        else:
            sample.segment = math.floor(sample.elapsed / args.window_seconds) + 1
            sample.target = args.target_voltage
            since_start = sample.elapsed - (sample.segment - 1) * args.window_seconds
        if since_start < args.settle_seconds:
            continue
        grouped.setdefault(sample.segment, []).append(sample)
    kept = []
    summaries = []
    for segment, group in grouped.items():
        group = group[args.discard_first:]
        if len(group) < args.min_samples:
            continue
        kept.extend(group)
        row = {"segment": segment, "elapsed_s": group[0].elapsed,
               "end_elapsed_s": group[-1].elapsed, "samples": len(group),
               "target_voltage_v": group[0].target,
               "request_current_a": group[0].request_current,
               "request_mode": group[0].request_mode,
               "pdo_object_position": group[0].object_position}
        for name, attribute in (("voltage", "voltage"), ("current", "current"),
                                ("power", "power"), ("cc1", "cc1"), ("cc2", "cc2")):
            values = [value for sample in group if (value := getattr(sample, attribute)) is not None]
            row[name + "_samples"] = len(values)
            row[name + "_mean"] = statistics.fmean(values) if values else None
            row[name + "_min"] = min(values) if values else None
            row[name + "_max"] = max(values) if values else None
            row[name + "_std"] = statistics.pstdev(values) if values else None
        row["voltage_pp_mv"] = (row["voltage_max"] - row["voltage_min"]) * 1000
        row["voltage_error_v"] = row["voltage_mean"] - group[0].target if group[0].target is not None else None
        summaries.append(row)
    if not summaries:
        raise ValueError("条件に合う測定区間がありません。時刻範囲・settle・min-samples・PD要求を確認してください。")
    leg, direction, previous = 0, 0, None
    for row in summaries:
        identity = (row["request_mode"], row["pdo_object_position"])
        if previous is None or identity != (previous["request_mode"], previous["pdo_object_position"]):
            leg, direction = leg + 1, 0
        elif row["target_voltage_v"] is not None and previous["target_voltage_v"] is not None:
            delta = row["target_voltage_v"] - previous["target_voltage_v"]
            sign = 1 if delta > 0 else -1 if delta < 0 else 0
            if sign and direction and sign != direction:
                leg += 1
            if sign:
                direction = sign
        row["sweep_leg"] = leg
        previous = row
    return kept, summaries


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def normalized_rows(samples):
    return [{"source_row": s.row, "timestamp_us": s.time_us, "elapsed_s": s.elapsed,
             "segment": s.segment, "target_voltage_v": s.target,
             "request_current_a": s.request_current, "actual_voltage_v": s.voltage,
             "request_mode": s.request_mode, "pdo_object_position": s.object_position,
             "actual_current_a": s.current, "actual_power_w": s.power,
             "cc1_v": s.cc1, "cc2_v": s.cc2} for s in samples]


def human_report(path, kind, all_samples, kept, summaries, skipped, args, language="en"):
    def tr(ja, en):
        return en if language == "en" else ja

    gaps = [b.time_us - a.time_us for a, b in zip(all_samples, all_samples[1:]) if b.time_us > a.time_us]
    grouping = (tr("SPR PPS / SPR AVS / EPR AVSのAccept / PS_RDY成立ごと", "Successful SPR PPS / SPR AVS / EPR AVS Accept / PS_RDY contracts")
                if args.group_by == "request" else tr(f"{args.window_seconds}秒の時間窓", f"{args.window_seconds}-second time windows"))
    lines = [tr("CY4500 CSV かんたんレポート", "CY4500 CSV Measurement Report"), "=" * 46,
             tr(f"入力ファイル: {path}", f"Input file: {path}"),
             tr(f"入力形式: {kind}", f"Input format: {kind}"),
             tr(f"有効行数: {len(all_samples)} / 不正な時刻・電圧による除外: {skipped}",
                f"Valid rows: {len(all_samples)} / excluded for invalid time or voltage: {skipped}"),
             tr(f"解析行数: {len(kept)} / 集計区間数: {len(summaries)}",
                f"Analyzed rows: {len(kept)} / segments: {len(summaries)}"),
             tr(f"解析範囲: {kept[0].elapsed:.6f} ～ {kept[-1].elapsed:.6f} s",
                f"Analyzed range: {kept[0].elapsed:.6f} to {kept[-1].elapsed:.6f} s"),
             tr(f"集計方法: {grouping}", f"Grouping: {grouping}"),
             tr(f"各区間の先頭除外: {args.settle_seconds:g}秒 + {args.discard_first}サンプル",
                f"Excluded at each segment start: {args.settle_seconds:g} s + {args.discard_first} samples"),
             tr(f"各区間の最低サンプル数: {args.min_samples}", f"Minimum samples per segment: {args.min_samples}")]
    if gaps:
        lines += [tr(f"入力のサンプル間隔: 中央値 {statistics.median(gaps)/1000:.4f} ms / 最大 {max(gaps)/1000:.4f} ms",
                     f"Input sample interval: median {statistics.median(gaps)/1000:.4f} ms / maximum {max(gaps)/1000:.4f} ms")]
    voltage = [s.voltage for s in kept]
    current = [s.current for s in kept if s.current is not None]
    power = [s.power for s in kept if s.power is not None]
    lines += ["", tr("実測値", "Measurements"),
              tr(f"電圧: 平均 {statistics.fmean(voltage):.4f} V / 最小 {min(voltage):.4f} V / 最大 {max(voltage):.4f} V",
                 f"Voltage: mean {statistics.fmean(voltage):.4f} V / minimum {min(voltage):.4f} V / maximum {max(voltage):.4f} V")]
    if current:
        lines += [tr(f"電流: 平均 {statistics.fmean(current):.4f} A / 最小 {min(current):.4f} A / 最大 {max(current):.4f} A",
                     f"Current: mean {statistics.fmean(current):.4f} A / minimum {min(current):.4f} A / maximum {max(current):.4f} A")]
    if power:
        lines += [tr(f"電力: 平均 {statistics.fmean(power):.3f} W / 最大 {max(power):.3f} W",
                     f"Power: mean {statistics.fmean(power):.3f} W / maximum {max(power):.3f} W")]
    largest = max(summaries, key=lambda row: row["voltage_pp_mv"])
    lines += ["", tr(f"最大の区間内電圧p-p: {largest['voltage_pp_mv']:.3f} mV（区間 {largest['segment']}, t={largest['elapsed_s']:.4f}s）",
                     f"Largest segment voltage peak-to-peak: {largest['voltage_pp_mv']:.3f} mV (segment {largest['segment']}, t={largest['elapsed_s']:.4f}s)"),
              tr("電圧p-pは区間内の最大値−最小値です。電圧遷移・ドリフト・外れ値も含みます。",
                 "Voltage peak-to-peak is the segment maximum minus minimum. It includes transitions, drift and outliers."),
              tr("ASD-PD31のripple測定値とは測定方法・帯域が異なるため、直接比較できません。",
                 "Measurement method and bandwidth differ from ASD-PD31 ripple measurements; the values are not directly comparable."),
              tr("短い区間や欠測の多い区間はsamplesとサンプル間隔も確認してください。",
                 "For short segments or segments with missing data, also check sample counts and sample intervals.")]
    if args.target_voltage is None and args.group_by == "time":
        lines += [tr("設定電圧が不明のため、電圧誤差は空欄です。固定電圧測定では --target-voltage で指定できます。",
                     "Voltage error is blank because the target voltage is unknown. Use --target-voltage for a known fixed target.")]
    if args.group_by == "request":
        counts = Counter(row["request_mode"] for row in summaries)
        lines += ["", tr("方式別の集計区間数: ", "Segments by request mode: ") + ", ".join(f"{mode}={count}" for mode, count in sorted(counts.items())),
                  tr("要求区間はPPS/AVS契約のPS_RDYから次の契約変更までです。遷移を含む場合があります。",
                     "Request segments run from a PPS/AVS contract's PS_RDY to the next contract change. They may include transitions."),
                  tr("固定契約・能力情報不足で解釈できない要求・成立前の区間は除外します。",
                     "Fixed contracts, requests without sufficient capability context, and intervals before contract establishment are excluded."),
                  tr("EP81とEP83の共通時計は未確立です。取得時刻をそのまま照合し、時計補正は行いません。",
                     "A shared EP81/EP83 clock has not been established. Captured timestamps are compared as exported, without clock correction."),
                  tr("request_current_aはPD要求の電流上限で、設定負荷電流ではありません。",
                     "request_current_a is the PD request current limit, not a load-current setpoint.")]
    lines += ["", tr("出力", "Outputs"),
              tr("*_normalized.csv: 解析対象の全サンプル（追加の間引きなし）", "*_normalized.csv: all analyzed samples, without additional downsampling"),
              tr("*_summary.csv: 各区間の平均・最小・最大・標準偏差・電圧p-p", "*_summary.csv: segment means, minima, maxima, standard deviations and voltage peak-to-peak"),
              tr("*_voltage_actual/current/power/voltage_pp.png: 区間ごとの時間推移", "*_voltage_actual/current/power/voltage_pp.png: segment statistics over time"),
              tr("*_voltage_error.png: 設定電圧が分かる場合の誤差", "*_voltage_error.png: error when the target voltage is known"),
              tr("*_voltage_pp_vs_voltage.png: 実測電圧に対する区間内電圧p-p", "*_voltage_pp_vs_voltage.png: segment voltage peak-to-peak versus measured voltage")]
    if args.group_by == "request":
        lines += [tr("*_sweep_*.png: 要求電圧に対する統計値（方式・往路/復路を分離）",
                     "*_sweep_*.png: statistics versus requested voltage, separating modes and outbound/return legs")]
    if args.no_plots:
        lines += [tr("今回は --no-plots のためPNGは作成していません。", "PNG files were not generated because --no-plots was specified.")]
    return "\n".join(lines) + "\n"


PLOTS = [("voltage_mean", "voltage_actual", "Voltage (V)"),
         ("current_mean", "current", "Current (A)"),
         ("power_mean", "power", "Power (W)"),
         ("voltage_pp_mv", "voltage_pp", "Voltage peak-to-peak (mV)"),
         ("voltage_error_v", "voltage_error", "Voltage error (V)")]


def plot_specs(summaries):
    return [(col, suffix, label) for col, suffix, label in PLOTS if any(row[col] is not None for row in summaries)]


def sweep_plot_specs(summaries):
    return plot_specs(summaries) if any(row["request_mode"] is not None for row in summaries) else []


def sweep_series(summaries):
    """Retain chronology and share the turnaround point between return legs."""
    curves = []
    for row in summaries:
        if not curves or curves[-1][-1]["sweep_leg"] != row["sweep_leg"]:
            curve = []
            if curves:
                previous = curves[-1][-1]
                if (previous["request_mode"], previous["pdo_object_position"]) == (row["request_mode"], row["pdo_object_position"]):
                    curve.append(previous)
            curves.append(curve)
        curves[-1].append(row)
    return curves


def generate_plots(summaries, prefix, plt):
    for col, suffix, label in plot_specs(summaries):
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot([r["elapsed_s"] for r in summaries], [r[col] for r in summaries], ".-", linewidth=1)
        if col == "voltage_mean" and any(r["target_voltage_v"] is not None for r in summaries):
            ax.plot([r["elapsed_s"] for r in summaries], [r["target_voltage_v"] for r in summaries], "--", label="Requested / specified voltage")
            ax.legend()
        ax.set(xlabel="Elapsed time (s)", ylabel=label, title="CY4500: " + label)
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(f"{prefix}_{suffix}.png", dpi=150)
        plt.close(fig)
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter([r["voltage_mean"] for r in summaries], [r["voltage_pp_mv"] for r in summaries], s=15)
    ax.set(xlabel="Measured voltage mean (V)", ylabel="Voltage peak-to-peak (mV)", title="CY4500: voltage variation by measured voltage")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(f"{prefix}_voltage_pp_vs_voltage.png", dpi=150)
    plt.close(fig)
    for col, suffix, label in sweep_plot_specs(summaries):
        fig, ax = plt.subplots(figsize=(10, 5))
        for curve in sweep_series(summaries):
            last = curve[-1]
            ax.plot([row["target_voltage_v"] for row in curve], [row[col] for row in curve], ".-",
                    label=f"{last['request_mode']} / PDO {last['pdo_object_position']} / leg {last['sweep_leg']}")
        ax.set(xlabel="Requested voltage (V)", ylabel=label, title="CY4500 sweep: " + label)
        ax.grid(alpha=0.3)
        ax.legend(fontsize="small")
        fig.tight_layout()
        fig.savefig(f"{prefix}_sweep_{suffix}.png", dpi=150)
        plt.close(fig)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Analyze CY4500 scope/live CSV into summaries, PNG plots and Japanese/English reports.")
    parser.add_argument("csv", type=Path, help="Scope/live CSV, or PD CSV with a sibling .scope.csv")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--out", type=Path, help="Output prefix (default: input stem + _analysis)")
    parser.add_argument("--group-by", choices=("time", "request"), default="time")
    parser.add_argument("--window-seconds", type=float, default=1.0)
    parser.add_argument("--pd-csv", type=Path, help="Same-session PD CSV for SPR PPS / SPR AVS / EPR AVS request grouping")
    parser.add_argument("--request-mode", choices=("all", "spr-pps", "spr-avs", "epr-avs"), default="all",
                        help="Filter request-grouped segments by APDO family (default: all)")
    parser.add_argument("--target-voltage", type=float, help="Known fixed target voltage; time grouping only")
    parser.add_argument("--settle-seconds", type=float, default=0.0, help="Exclude first N seconds of each window/contract")
    parser.add_argument("--discard-first", type=int, default=0, help="Then exclude first N samples per window/contract")
    parser.add_argument("--min-samples", type=int, default=2)
    parser.add_argument("--start", type=float, help="Start elapsed time in seconds, inclusive")
    parser.add_argument("--end", type=float, help="End elapsed time in seconds, exclusive")
    parser.add_argument("--no-plots", action="store_true")
    parser.add_argument("--no-print", action="store_true")
    parser.add_argument("--no-report", action="store_true")
    parser.add_argument("--report-lang", choices=("ja", "en", "both"), default="en",
                        help="Report language (default: en). Both writes separate _human_report_ja/en.txt files.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing analysis outputs")
    args = parser.parse_args(argv)
    if not math.isfinite(args.window_seconds) or args.window_seconds <= 0:
        parser.error("--window-seconds must be finite and > 0")
    if not math.isfinite(args.settle_seconds) or args.settle_seconds < 0 or args.discard_first < 0 or args.min_samples < 1:
        parser.error("settle/discard must be nonnegative; min-samples must be >= 1")
    for name in ("start", "end", "target_voltage"):
        value = getattr(args, name)
        if value is not None and (not math.isfinite(value) or value < 0):
            parser.error(f"--{name.replace('_', '-')} must be finite and nonnegative")
    if args.start is not None and args.end is not None and args.end <= args.start:
        parser.error("--end must be greater than --start")
    if args.group_by == "request" and args.target_voltage is not None:
        parser.error("--target-voltage requires --group-by time")
    if args.group_by == "time" and args.pd_csv is not None:
        parser.error("--pd-csv requires --group-by request")
    if args.group_by != "request" and args.request_mode != "all":
        parser.error("--request-mode requires --group-by request")
    try:
        source, sibling_pd = resolve_input(args.csv)
        samples, kind, skipped = load_samples(source)
        events = []
        if args.group_by == "request":
            if kind != "scope":
                raise ValueError("要求区間集計には機器時刻付きscope CSVが必要です。live-statusのホスト時刻は使用できません。")
            pd_path = args.pd_csv or sibling_pd
            if pd_path is None or not pd_path.is_file():
                raise ValueError("--group-by request には同セッションの --pd-csv が必要です。")
            events = request_events(pd_path, include_metadata=True)
        kept, summaries = group_samples(samples, args, events)
        prefix = args.out or source.with_name(source.stem + "_analysis")
        outputs = [Path(f"{prefix}_normalized.csv"), Path(f"{prefix}_summary.csv")]
        report_paths = {}
        if not args.no_report:
            languages = ("ja", "en") if args.report_lang == "both" else (args.report_lang,)
            for language in languages:
                suffix = f"_{language}" if args.report_lang == "both" else ""
                report_paths[language] = Path(f"{prefix}_human_report{suffix}.txt")
            outputs.extend(report_paths.values())
        plt = None
        if not args.no_plots:
            try:
                import matplotlib
                matplotlib.use("Agg")
                import matplotlib.pyplot as plt
            except ImportError as exc:
                raise ValueError("PNG作成にはmatplotlibが必要です: python -m pip install -r requirements-analysis.txt（CSVのみなら --no-plots）") from exc
            outputs += [Path(f"{prefix}_{suffix}.png") for _, suffix, _ in plot_specs(summaries)]
            outputs += [Path(f"{prefix}_sweep_{suffix}.png") for _, suffix, _ in sweep_plot_specs(summaries)]
            outputs.append(Path(f"{prefix}_voltage_pp_vs_voltage.png"))
        input_paths = {args.csv.resolve(), source.resolve()}
        if args.pd_csv or sibling_pd:
            input_paths.add((args.pd_csv or sibling_pd).resolve())
        if any(path.resolve() in input_paths for path in outputs):
            raise ValueError("出力先が入力CSVと重なっています。--out を変更してください。")
        collisions = [str(path) for path in outputs if path.exists()]
        if collisions and not args.force:
            raise ValueError("出力が既に存在します。別の --out または --force を指定してください: " + ", ".join(collisions))
        prefix.parent.mkdir(parents=True, exist_ok=True)
        write_csv(outputs[0], normalized_rows(kept))
        write_csv(outputs[1], summaries)
        for language, report_path in report_paths.items():
            report_path.write_text(human_report(source, kind, samples, kept, summaries, skipped, args, language), encoding="utf-8")
        if plt is not None:
            generate_plots(summaries, prefix, plt)
        print(f"Loaded: {len(samples)} / analyzed: {len(kept)} / segments: {len(summaries)} / invalid: {skipped}")
        if not args.no_print:
            print("segment   elapsed_s     n    V_mean    I_mean    P_mean    V_pp_mV")
            for row in summaries[:40]:
                values = [row[k] for k in ("segment", "elapsed_s", "samples", "voltage_mean", "current_mean", "power_mean", "voltage_pp_mv")]
                print(" ".join(f"{v:10.4f}" if isinstance(v, float) else f"{str(v) if v is not None else '-':>10}" for v in values))
            if len(summaries) > 40:
                print(f"... {len(summaries)} segments; see summary CSV for all rows.")
        for output in outputs:
            print(f"Saved: {output}")
        return 0
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
