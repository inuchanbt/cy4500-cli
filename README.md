# cy4500-cli

English | [日本語](README.ja.md)

A Python command-line controller and capture tool for the **Infineon/Cypress CY4500-EPR USB-PD analyzer**.

Capture USB Power Delivery traffic, record voltage/current telemetry, configure hardware triggers, and analyze EPR Adjustable Voltage Supply (AVS) transitions from saved measurements.

The current implementation comes from the v13 controller and v10 protocol definitions. The maintained files are now `cy4500_cli.py` and `ezpd_protocol.py`; revisions are tracked in Git.

## Features

- Capture and decode EP81 USB-PD records, with CSV, JSONL, raw binary, and semantic session summaries.
- Record EP83 scope telemetry at approximately 1 kS/s, including VBUS, IBUS, CC1, and CC2.
- Capture PD traffic and scope telemetry in one START/STOP session.
- Read live voltage/current status using command `0x11`.
- Configure SOM/EOM/MTR hardware triggers and arm the measurement engine.
- Configure CC1/CC2 terminations explicitly.
- Analyze AVS requests, ACCEPT/PS_RDY timing, voltage movement, and settling from saved PD/scope CSV files.
- Summarize scope/live CSV measurements into statistics, PNG plots and Japanese/English reports with `analyze_sweep_csv.py`.

## Offline sweep/waveform summaries

`analyze_sweep_csv.py` provides an ASD-PD31-style analysis workflow for CY4500
scope CSV and `live-status --csv` output. Passing a PD CSV loads its sibling
`.scope.csv`. It does not open hardware.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-analysis.txt
.\.venv\Scripts\python.exe analyze_sweep_csv.py captures/session01.scope.csv
.\.venv\Scripts\python.exe analyze_sweep_csv.py captures/session01.csv --group-by request --settle-seconds 0.05
.\.venv\Scripts\python.exe analyze_sweep_csv.py captures/session01.scope.csv --no-plots
.\.venv\Scripts\python.exe analyze_sweep_csv.py captures/session01.scope.csv --report-lang ja --out captures/session01_ja
```

The default groups samples into one-second windows. Outputs use the waveform
stem plus `_analysis`: `_normalized.csv`, `_summary.csv`, `_human_report.txt`,
and PNG plots for voltage, current, power and voltage peak-to-peak. CSV/report
analysis uses only the Python standard library; matplotlib is optional for PNGs.
Use `--out PREFIX` to change the prefix; existing outputs require `--force`.
Reports default to English. `--report-lang ja` writes a Japanese
`_human_report.txt`; `--report-lang both` writes `_human_report_ja.txt` and
`_human_report_en.txt`. `--no-report` suppresses reports in either language.

Options include `--window-seconds`, `--start`/`--end` (seconds from the first
sample, end exclusive), `--discard-first` (per segment), `--min-samples`
(default 2), and `--target-voltage` for a known fixed target. Time windows stay
anchored to the first input sample. Settling time exclusion is applied before
sample exclusion. Unknown measurements/targets remain blank; signed current
is retained. Plots show segment statistics rather than every raw sample.

Request grouping uses successful SOP AVS EPR_REQUEST / ACCEPT / PS_RDY
contracts. Repeated identical contracts share a segment; a return to an earlier
voltage remains separate. Unsupported contracts and unestablished intervals
are excluded. Use `--pd-csv PATH` if the same-session PD file has another name.
Request current is a PD current limit, not a load-current setpoint. EP81/EP83
clock alignment is unestablished; no offset correction is applied. Live-status
host timestamps cannot be used for request grouping.

**Voltage peak-to-peak is the maximum minus minimum in a segment. It includes
transitions and drift and is not directly comparable to ASD-PD31 ripple.**
Select steady intervals for evaluation. The report also records sample gaps.

## Requirements

- A CY4500-EPR analyzer with USB VID:PID **`04B4:FDEF`**, interface **0**.
- Windows with the **WinUSB** function driver for the analyzer interface.
- Python **3.10 or newer**. Software checks for this repository use Python 3.12.
- [`python-libusb1`](https://github.com/vpelletier/python-libusb1), installed as `libusb1` and imported as `usb1`.

The implementation targets CY4500-EPR. Legacy CY4500 devices and other operating systems have not been validated here. Keep **EZ-PD Protocol Analyzer Utility closed** while the CLI owns the device.

## Quick start (Windows PowerShell)

```powershell
git clone https://github.com/inuchanbt/cy4500-cli.git
cd cy4500-cli
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe cy4500_cli.py --help
```

The examples below call the virtual environment's Python directly, so activating it is optional. If your Python installation uses the `py` launcher, use `py -3 -m venv .venv` to create the environment.

With the analyzer connected, inspect its USB descriptors and firmware version:

```powershell
.\.venv\Scripts\python.exe cy4500_cli.py usb-info
.\.venv\Scripts\python.exe cy4500_cli.py version
```

## Usage

### Capture USB-PD traffic and scope telemetry

Record a 10-second session with raw EP83 transfers:

```powershell
.\.venv\Scripts\python.exe cy4500_cli.py capture --seconds 10 --scope --scope-raw --out-prefix captures/session01
```

Capture now runs until Ctrl+C by default, with scope and ccgx3 enabled. Omit `--out-prefix` for a timestamped `captures/cy4500_YYYYMMDD_HHMMSS_ffffff` prefix in local time. Valid GOODCRC console lines are hidden by default, while all packets are saved and error lines remain visible. Use `--show-goodcrc` to display them. `--hide-goodcrc`, `--scope`, `--ccgx3`, and `--until-ctrl-c` still explicitly select the default behavior.

AVS transition analysis is off by default. Add `--analyze-transitions` to run it after capture. Use `--no-scope` for PD-only capture and `--no-ccgx3` to omit the GUI archive. `--status-interval N` changes the default 1-second status interval. Raw EP83 transfer output (`--scope-raw`) and overwriting (`--force`) remain off by default. Analysis and raw scope output cannot be combined with `--no-scope`.

| Setting | Default | Override |
| --- | --- | --- |
| Duration | Until Ctrl+C | `--seconds N` |
| Prefix | Timestamped path in captures | `--out-prefix PREFIX` |
| Outputs | CSV, ccgx3, PD raw/hex/JSONL, scope CSV, summary | `--no-ccgx3` |
| Scope | On | `--no-scope` |
| Valid GoodCRC console lines | Hidden; all saved | `--show-goodcrc` |
| Status | Every 1 second | `--status-interval N` / `--quiet` |
| AVS analysis | Off | `--analyze-transitions` |
| Raw EP83 USB transfers | Off | `--scope-raw` |
| Overwrite | Off | `--force` |

For continuous capture, stop with **Ctrl+C**:

```powershell
.\.venv\Scripts\python.exe cy4500_cli.py capture --until-ctrl-c --scope --out-prefix captures/session02
# The same capture defaults, with an automatic timestamped prefix:
.\.venv\Scripts\python.exe cy4500_cli.py capture
# Analyze after stopping:
.\.venv\Scripts\python.exe cy4500_cli.py capture --analyze-transitions
```

Output directories are created automatically. Use a new prefix for each session: capture checks for collisions before writing files or opening the analyzer if any capture or analysis output already exists for that prefix, including optional outputs. Add `--force` to overwrite the files generated by the current command. Use a prefix without a file extension, because output suffixes replace any existing extension.

### Export Analyzer Utility CSV and ccgx3

CSV and ccgx3 are both saved by default. `--ccgx3` remains an explicit alias for the default; `--no-ccgx3` disables the archive:

```powershell
.\.venv\Scripts\python.exe cy4500_cli.py capture --seconds 10 --scope --ccgx3 --out-prefix captures/session01
```

- `.csv` uses Utility-style values: sequential row numbers, textual status, integer millivolts under the original `Vbus(V)` heading, and hexadecimal header/data fields. Sno is sequential, including voltage events. Original hardware values remain in `.records.bin` and `.records.jsonl`.
- `.ccgx3` is a ZIP containing Java-serialized `USBPacketData` and `GraphData` lists. With `--scope`, it includes captured waveform samples; otherwise the waveform list is empty. No Java installation is needed to export.
- Output is finalized when capture ends, including Ctrl+C. Temporary packet and scope data are spooled to disk during capture.

Convert an existing CLI capture without hardware (PD packets only):

```powershell
.\.venv\Scripts\python.exe cy4500_cli.py export-gui --records captures/session01.records.bin --out-prefix captures/converted01
```

This writes both `.csv` and `.ccgx3`. The input must be a CLI file of fixed 64-byte records, not `.xfers.bin` or a GUI ccgx3 file. Existing outputs cause the command to stop; add `--force` to overwrite them.

Compatibility was checked against the supplied September 12, 2026 Utility capture: all 38 CSV rows match, including extended messages, and the installed Utility 4.2 Java classes deserialize the generated packets and waveform data. CSV and ccgx3 from a fresh hardware capture were also verified to open normally in the GUI. This exports raw packets and table values; it does not reproduce user annotations or GUI session settings.

Analysis accepts both old CLI and Utility CSV formats, converting voltage units and payload encoding. `--gui-csv` is now a no-op compatibility alias and creates no second CSV. `--csv-bug-compatible` has been removed.

Regression tests use artificial packets; captured measurement files are not included in the repository. Run regression checks with `python -m unittest discover -s tests -v`. The Java helpers in `tests` are optional interoperability checks and require a JDK plus the installed Utility's plugins on the classpath.

### Read live status

Read 20 samples, 100 ms apart, and save them to CSV:

```powershell
.\.venv\Scripts\python.exe cy4500_cli.py live-status --count 20 --interval 0.1 --csv captures/live-status.csv
```

`volt-amp` is an alias for `live-status`. Omit `--count` to run until Ctrl+C. `--median` controls the rolling median window for displayed current; the default is 5 samples.

### Record scope telemetry

```powershell
.\.venv\Scripts\python.exe cy4500_cli.py scope --seconds 5 --csv captures/scope01.csv --raw captures/scope01.xfers.bin
```

Scope mode drains EP81 while recording EP83. Use `capture --scope` when you also need decoded PD records.

### Analyze a saved session

Re-run AVS analysis without opening the analyzer:

```powershell
.\.venv\Scripts\python.exe cy4500_cli.py analyze-sync --pd-csv captures/session01.csv --scope-csv captures/session01.scope.csv --out-prefix captures/session01_analysis
```

Use PD and scope CSV files from the same session. Analysis reports the requested target voltage and the observed final plateau separately, including whether the requested target band was reached. A trace can settle around an observed plateau without reaching the requested voltage band.

Movement direction follows the first sustained measured voltage change, rather than nominal target minus measured baseline. This handles a downward sweep whose voltage offset exceeds its requested step. An unobserved movement retains the nominal direction with `direction_from_target_fallback`. Already being beyond the target is flagged as `target_already_beyond_at_request`, not a target crossing at zero time. A slew with a sign opposing the measured movement is omitted and flagged (`observed_slew_opposes_movement` / `absolute_slew_opposes_movement`); this can otherwise report overshoot recovery as ramp speed. `observed_slew_unresolved` marks endpoints observed at the same sample. Raw PD/scope and settling results remain unchanged. The October 6 test08 regression confirms 34 upward and 33 downward AVS movements, correcting 28 descending requests previously marked upward.

Existing analysis outputs cause the command to stop; add `--force` to overwrite them. A capture prefix can be reused here if it has no analysis outputs yet.

Use `analyze-sync --help` for baseline, movement, settling, and plateau thresholds. The USB library must still be installed because it is imported when the CLI starts.

### Configure and arm a hardware trigger

List supported DATA message names or inspect an EPR request trigger packet without USB access:

```powershell
.\.venv\Scripts\python.exe cy4500_cli.py trigger --list-types DATA
.\.venv\Scripts\python.exe cy4500_cli.py trigger --msg-class DATA --msg-type EPR_REQUEST --dry-run
```

Configure the trigger, arm it for 30 seconds, then stop and clear the criteria:

```powershell
.\.venv\Scripts\python.exe cy4500_cli.py trigger --msg-class DATA --msg-type EPR_REQUEST --arm-seconds 30 --clear-on-exit
```

Enabled criteria are combined with **AND**. Setting a trigger only configures it; evaluation requires the measurement engine to be running. `--arm` runs until Ctrl+C, while `--arm-seconds` sets a duration. During arming, EP81/EP83 are drained; this command does not export a capture.

To arm previously configured criteria, use `trigger-arm --seconds 30`. To clear them explicitly, use `trigger-clear`. Without `--clear-on-exit`, arming does not explicitly clear the criteria when it stops.

### Configure CC terminations

Both CC lines must be specified because the device provides no readback for preserving an unspecified side. Inspect a packet first:

```powershell
.\.venv\Scripts\python.exe cy4500_cli.py termination --cc1 NONE --cc2 NONE --dry-run
```

To set both terminations to `NONE`:

```powershell
.\.venv\Scripts\python.exe cy4500_cli.py termination --clear
```

Available values are `RP`, `RA`, `RD`, and `NONE`. These settings physically change the CC network; choose them for the intended electrical setup. A successful USB write does not provide semantic acknowledgment or readback of the setting.

## Commands at a glance

Append `--help` to any command for its options.

| Command | Purpose |
| --- | --- |
| `usb-info` | Enumerate matching USB descriptors |
| `version` | Read analyzer firmware version |
| `live-status` / `volt-amp` | Poll VBUS, IBUS, CC1, and CC2 |
| `capture` | Capture USB-PD records, optionally with scope telemetry |
| `export-gui` | Convert saved `.records.bin` to Utility CSV and ccgx3 without hardware (PD only) |
| `scope` | Record scope telemetry while draining PD traffic |
| `analyze-sync` | Analyze saved PD and scope CSV files |
| `trigger` | Configure, inspect, clear, or arm hardware trigger criteria |
| `trigger-arm` | Arm previously configured criteria |
| `trigger-epr-request` | Configure an EPR_REQUEST trigger without arming it |
| `trigger-clear` | Clear hardware trigger criteria |
| `termination` | Configure or inspect CC1/CC2 termination settings |
| `termination-clear` | Set both CC terminations to NONE |

## Capture output

For `--out-prefix captures/session01`, the following suffixes are added to `captures/session01`:

| Suffix | Contents | Created when |
| --- | --- | --- |
| `.csv` | Decoded PD records in Analyzer Utility CSV schema | `capture` |
| `.ccgx3` | GUI session file; includes waveform samples when `--scope` is enabled | `capture --ccgx3` |
| `.records.jsonl` | Decoded records with semantic details | `capture` |
| `.records.bin` | Concatenated raw 64-byte PD records | `capture` |
| `.records.hex.txt` | Hexadecimal PD record dump | `capture` |
| `.xfers.bin` | Data-bearing EP81 transfers, each prefixed by a 4-byte little-endian length | `capture` |
| `.summary.txt` | Semantic session summary and capture clock metadata | `capture` |
| `.scope.csv` | Decoded EP83 samples | `capture --scope` |
| `.scope.xfers.bin` | Length-prefixed raw EP83 transfers | `capture --scope --scope-raw` |
| `.transitions.csv` / `.transitions.txt` | Detailed AVS transition analysis | `capture --scope --analyze-transitions`, or `analyze-sync` |
| `.transition_summary.csv` / `.transition_summary.txt` | Compact per-transition summary | `capture --scope --analyze-transitions`, or `analyze-sync` |

PD CSV uses UTF-8 and the Utility format. Scope and analysis CSV files use UTF-8 with a BOM. PD end times reflect the actual captured end time.

## Repository layout

```text
cy4500-cli/
├── cy4500_cli.py       # Command-line interface and device/capture control
├── ezpd_protocol.py    # Protocol definitions, decoders, and AVS analysis
├── utility_export.py  # Utility CSV/ccgx3 serialization
├── requirements.txt   # Python dependencies
├── tests/             # Synthetic regression and optional Java checks
├── docs/              # Development and validation notes
├── README.md          # English
├── README.ja.md       # Japanese
└── LICENSE            # MIT
```

Local work can be kept in these Git-ignored directories:

- `captures/` — measurements and generated analysis results.
- `archive/legacy/` — historical controller/protocol versions.
- `archive/probes/` — exploratory hardware probe scripts.
- `notes/` — investigation logs and working notes.

These directories are local working material and are not included in a fresh clone. Use `captures/` in output paths to keep measurements out of Git.

## Implementation notes

- Protocol definitions are based on CY4500-EPR investigation and EZ-PD Protocol Analyzer Utility 4.2.0 behavior. Unknown semantics are marked in the source.
- The endpoints are `0x02` (commands), `0x81` (PD capture), `0x83` (scope), and `0x84` (command responses).
- Device timestamps are preserved, and no host timestamp offset is applied. A common EP81/EP83 clock has not been established; cross-stream timing results should be interpreted with that limitation in mind.
- Voltage/current conversion follows the utility's CY4500-EPR formulas. Live-status polling and EP83 scope capture are separate measurement paths.
- This tool observes and analyzes EPR/AVS negotiations. It does not command a connected USB-PD source to negotiate a requested output voltage.

## Troubleshooting

- **`python-libusb1 is required`**: install `requirements.txt` with the same Python executable used to launch the CLI.
- **`ezpd_protocol.py must be in the same directory or on PYTHONPATH`**: keep cy4500_cli.py, ezpd_protocol.py and utility_export.py together.
- **Device not found or access denied**: check the USB connection, the `04B4:FDEF` device/interface driver, and whether another analyzer application has the device open.
- **No AVS transitions found**: verify that the capture includes AVS EPR_REQUEST traffic and use PD/scope CSV files from the same session. A recording started after the relevant negotiation may lack the needed context.

## License

[MIT](LICENSE), copyright (c) 2026 inuchanbt.


### Timestamp export correction (2026-10-06)

Utility CSV and ccgx3 export now select the nearest 32-bit timestamp epoch rather than adding a full epoch on every backward start time. Voltage events and idle-error records can have an earlier start than the preceding record; those intervals remain visible, including negative Delta values. Actual wraps still unwrap correctly, including delayed records from the previous epoch. The nearest-epoch rule requires adjacent observed timestamps to be less than 2^31 microseconds apart (about 35.8 minutes). Raw records, reported packet duration and scope timestamps are preserved. Paired TI/CY captures `cy_ti_test01` through `03` verified the fix. Original captures were retained; corrected CSV/ccgx3 files, including the original scope samples, were retained locally under `captures/comparison/` and are not distributed.

## Development and paired measurements

See [development and verification](docs/DEVELOPMENT.md). GitHub Actions runs synthetic regression tests on Windows with Python 3.10 and 3.12. Private measurements and the proprietary Utility runtime are not needed for these tests.

Paired TI/CY sweep recordings verified 1,622 common valid PD messages and all 67 AVS request/Accept/PS_RDY sequences. Movement direction now comes from sustained measured voltage change; reversed overshoot recovery is not reported as ramp slew, and unresolved sample intervals are flagged. Hardware idle-error packets and VBUS events remain in saved output. The same test had 163,035 CY versus 9,016 TI scope samples; local analysis times were about 23.54 versus 0.67 seconds. Repeated plateau/settling searches contribute to the difference; no decimation or clock calibration is applied. See the [TI comparison reports (Japanese)](https://github.com/inuchanbt/TI-PD-ANALYZER-CLI/tree/main/docs/reports) for measured limitations.
