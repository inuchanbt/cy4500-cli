# cy4500-cli

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

AVS transition analysis runs automatically when `--scope` is enabled. Add `--no-analyze-transitions` to skip it. Omit `--scope` for PD-only capture.

For continuous capture, stop with **Ctrl+C**:

```powershell
.\.venv\Scripts\python.exe cy4500_cli.py capture --until-ctrl-c --scope --out-prefix captures/session02
```

Output directories are created automatically. Use a new prefix for each session: existing output files with the same names are overwritten. Use a prefix without a file extension, because output suffixes replace any existing extension.

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
| `.records.jsonl` | Decoded records with semantic details | `capture` |
| `.records.bin` | Concatenated raw 64-byte PD records | `capture` |
| `.records.hex.txt` | Hexadecimal PD record dump | `capture` |
| `.xfers.bin` | Data-bearing EP81 transfers, each prefixed by a 4-byte little-endian length | `capture` |
| `.summary.txt` | Semantic session summary and capture clock metadata | `capture` |
| `.scope.csv` | Decoded EP83 samples | `capture --scope` |
| `.scope.xfers.bin` | Length-prefixed raw EP83 transfers | `capture --scope --scope-raw` |
| `.transitions.csv` / `.transitions.txt` | Detailed AVS transition analysis | Scope capture with analysis, or `analyze-sync` |
| `.transition_summary.csv` / `.transition_summary.txt` | Compact per-transition summary | Scope capture with analysis, or `analyze-sync` |

CSV files use UTF-8 with a BOM for convenient opening in spreadsheet applications. By default, PD CSV end times reflect the decoded record. `--csv-bug-compatible` reproduces Analyzer Utility 4.2.0's behavior where End Time duplicates Start Time.

## Repository layout

```text
cy4500-cli/
├── cy4500_cli.py       # Command-line interface and device/capture control
├── ezpd_protocol.py    # Protocol definitions, decoders, and AVS analysis
├── requirements.txt   # Python dependencies
├── README.md
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
- **`ezpd_protocol.py must be in the same directory or on PYTHONPATH`**: keep the two maintained Python files together.
- **Device not found or access denied**: check the USB connection, the `04B4:FDEF` device/interface driver, and whether another analyzer application has the device open.
- **No AVS transitions found**: verify that the capture includes AVS EPR_REQUEST traffic and use PD/scope CSV files from the same session. A recording started after the relevant negotiation may lack the needed context.

## License

[MIT](LICENSE), copyright (c) 2026 inuchanbt.
