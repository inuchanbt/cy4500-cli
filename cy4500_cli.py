"""
cy4500_cli.py
=============

Standalone direct controller/capture backend for Infineon/Cypress CY4500-EPR.

Transport:
    python-libusb1 (import name: usb1)
    Windows function driver: WinUSB
    VID:PID: 04B4:FDEF
    Interface: 0

Confirmed endpoints:
    0x02  BULK OUT  - command
    0x81  BULK IN   - USB-PD capture
    0x83  BULK IN   - scope/graph data
    0x84  BULK IN   - command response

v13 practical version: integrates EP81 USB-PD capture, EP83 ~1 kS/s scope
telemetry, CMD 0x11 live status, trigger, termination, CSV/raw export, and
semantic PD3.x/EPR session summaries.

Keep EZ-PD Protocol Analyzer Utility closed while this program owns the device.
"""

from __future__ import annotations

import argparse
import csv
import statistics
from collections import Counter
import json
import math
import re
import struct
import sys
import time
from contextlib import contextmanager
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional

try:
    import usb1
except ImportError as exc:
    raise SystemExit(
        "python-libusb1 is required.\n"
        "Install it with:\n"
        "    python -m pip install -U libusb1"
    ) from exc

try:
    from ezpd_protocol_v10 import (
        USB_VID_CYPRESS,
        USB_PID_CY4500_EPR,
        USB_INTERFACE,
        EP_CMD_OUT,
        EP_PD_IN,
        EP_CMD_RESP_IN,
        CMD_GET_VERSION,
        CMD_GET_VOLT_AMP,
        LIVE_STATUS_RESPONSE_SIZE,
        LIVE_CURRENT_DOC_TYPICAL_ACCURACY_A,
        decode_live_status_response,
        CMD_START_PAYLOAD,
        CMD_STOP_PAYLOAD,
        CAPTURE_RECORD_SIZE,
        EP81_TRANSFER_TERMINATOR,
        decode_capture_record,
        split_capture_transfer,
        build_trigger_packet,
        build_clear_trigger_packet,
        build_termination_packet,
        MESSAGE_TYPES_BY_CLASS,
        TRIGGER_SOP_TYPE,
        CSV_COLUMNS,
        capture_record_logical_message_bytes,
        u32_elapsed,
        vbus_epr_volts,
        EP_SCOPE_IN,
        MAX_SCOPE_READ_SIZE,
        SCOPE_PACKET_SIZE,
        SCOPE_SAMPLES_PER_PACKET,
        SCOPE_SAMPLE_SIZE,
        SCOPE_CSV_COLUMNS,
        ScopeTimestampUnwrapper,
        ScopeSample,
        ScopeTransferDecode,
        decode_scope_transfer,
        SyncPDSample,
        SyncScopeSample,
        AVSTransitionAnalysis,
        analyze_avs_transitions,
        TRANSITION_BASELINE_WINDOW_US,
        TRANSITION_BASELINE_GUARD_US,
        TRANSITION_MOVEMENT_MIN_THRESHOLD_V,
        TRANSITION_MOVEMENT_MAD_MULTIPLIER,
        TRANSITION_MOVEMENT_SUSTAIN_SAMPLES,
        TRANSITION_TARGET_BAND_FRACTION,
        TRANSITION_SETTLE_HOLD_US,
        TRANSITION_SETTLE_MAX_SAMPLE_GAP_US,
        TRANSITION_PLATEAU_LOOKBACK_US,
        TRANSITION_PLATEAU_MIN_SAMPLES,
        TRANSITION_RELATIVE_BAND_FRACTION,
        TRANSITION_RELATIVE_SETTLE_HOLD_US,
        TRANSITION_PLATEAU_STABILITY_MIN_SPAN_V,
        TRANSITION_PLATEAU_STABILITY_FRACTION,
        TRANSITION_PLATEAU_TARGET_GUARD_FRACTION,
        decode_rdo,
        decode_epr_source_capabilities_payload,
        format_pdo,
        format_rdo,
    )
except ImportError as exc:
    raise SystemExit(
        "ezpd_protocol_v10.py must be in the same directory or on PYTHONPATH."
    ) from exc


DEFAULT_IO_TIMEOUT_MS = 1000
DEFAULT_CAPTURE_READ_TIMEOUT_MS = 100
DEFAULT_CAPTURE_LOOP_SLEEP_SEC = 0.005
DEFAULT_CAPTURE_READ_SIZE = 65535
DEFAULT_SCOPE_READ_TIMEOUT_MS = 50
DEFAULT_SCOPE_READ_SIZE = MAX_SCOPE_READ_SIZE


@dataclass
class CaptureStats:
    requested_seconds: Optional[float]
    elapsed_seconds: float = 0.0
    usb_reads: int = 0
    idle_transfers: int = 0
    data_transfers: int = 0
    records: int = 0
    framing_errors: int = 0
    data_transfer_bytes: int = 0
    scope_reads: int = 0
    scope_short_transfers: int = 0
    scope_data_transfers: int = 0
    scope_transfer_bytes: int = 0
    scope_packets: int = 0
    scope_samples: int = 0
    scope_residual_bytes: int = 0
    scope_nonzero_padding_packets: int = 0
    interrupted: bool = False


@dataclass
class CaptureRecord:
    index: int
    raw: bytes
    decoded: dict[str, object]


RecordCallback = Callable[[CaptureRecord], None]
TransferCallback = Callable[[int, bytes], None]
ScopeSampleCallback = Callable[[int, ScopeSample], None]
ScopeTransferCallback = Callable[[int, bytes, ScopeTransferDecode], None]
StatusCallback = Callable[[CaptureStats], None]


class CY4500Error(RuntimeError):
    pass


class CY4500NotFoundError(CY4500Error):
    pass


class CY4500FramingError(CY4500Error):
    pass


class CY4500EPR:
    """
    Direct CY4500-EPR controller.

    The USB context/device handle stay open for the object lifetime, while
    interface 0 is claimed only around an operation. This is conservative and
    close to Analyzer Utility's claim/use/release behavior.
    """

    def __init__(
        self,
        *,
        vid: int = USB_VID_CYPRESS,
        pid: int = USB_PID_CY4500_EPR,
        io_timeout_ms: int = DEFAULT_IO_TIMEOUT_MS,
    ) -> None:
        self.vid = int(vid)
        self.pid = int(pid)
        self.io_timeout_ms = int(io_timeout_ms)
        self.context: Optional[usb1.USBContext] = None
        self.handle = None

    def open(self) -> "CY4500EPR":
        if self.handle is not None:
            return self

        self.context = usb1.USBContext()
        self.handle = self.context.openByVendorIDAndProductID(
            self.vid,
            self.pid,
            skip_on_error=True,
        )

        if self.handle is None:
            self.context.close()
            self.context = None
            raise CY4500NotFoundError(
                f"CY4500-EPR {self.vid:04X}:{self.pid:04X} "
                "not found or could not be opened"
            )

        return self

    def close(self) -> None:
        handle, context = self.handle, self.context
        self.handle = None
        self.context = None

        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

        if context is not None:
            try:
                context.close()
            except Exception:
                pass

    def __enter__(self) -> "CY4500EPR":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _require_open(self):
        if self.handle is None:
            raise CY4500Error("device is not open")
        return self.handle

    @contextmanager
    def _claimed(self):
        handle = self._require_open()
        try:
            with handle.claimInterface(USB_INTERFACE):
                yield handle
        except usb1.USBError as exc:
            raise CY4500Error(
                "could not claim/use interface 0. "
                "Make sure EZ-PD Protocol Analyzer Utility is closed."
            ) from exc

    @staticmethod
    def _cmd4(command: int) -> bytes:
        command = int(command)
        if not 0 <= command <= 0xFF:
            raise ValueError("command must fit one byte")
        return bytes((command, 0, 0, 0))

    @staticmethod
    def format_version(raw: bytes) -> str:
        raw = bytes(raw)
        if len(raw) != 4:
            raise ValueError("version response must be exactly 4 bytes")
        # Analyzer Utility displays the returned bytes in reverse order.
        return ".".join(str(value) for value in reversed(raw))

    def get_version_raw(self) -> bytes:
        """
        Send CMD_GET_VERSION (04 00 00 00) to EP02 and read 4 bytes from EP84.
        """
        request = self._cmd4(CMD_GET_VERSION)

        with self._claimed() as handle:
            written = handle.bulkWrite(
                EP_CMD_OUT,
                request,
                timeout=self.io_timeout_ms,
            )
            if written != len(request):
                raise CY4500Error(
                    f"short GET_VERSION write: {written}/{len(request)} bytes"
                )

            response = bytes(
                handle.bulkRead(
                    EP_CMD_RESP_IN,
                    4,
                    timeout=self.io_timeout_ms,
                )
            )

        if len(response) != 4:
            raise CY4500Error(
                f"unexpected GET_VERSION response length: {len(response)}"
            )

        return response

    def get_version(self) -> str:
        return self.format_version(self.get_version_raw())


    def get_volt_amp_raw(self) -> bytes:
        """
        Send read-only CMD_GET_VOLT_AMP (11 00 00 00) and read the confirmed
        8-byte EP84 response.
        """
        request = self._cmd4(CMD_GET_VOLT_AMP)

        with self._claimed() as handle:
            written = handle.bulkWrite(
                EP_CMD_OUT,
                request,
                timeout=self.io_timeout_ms,
            )
            if written != len(request):
                raise CY4500Error(
                    f"short GET_VOLT_AMP write: {written}/{len(request)} bytes"
                )

            response = bytes(
                handle.bulkRead(
                    EP_CMD_RESP_IN,
                    LIVE_STATUS_RESPONSE_SIZE,
                    timeout=self.io_timeout_ms,
                )
            )

        if len(response) != LIVE_STATUS_RESPONSE_SIZE:
            raise CY4500Error(
                f"unexpected GET_VOLT_AMP response length: {len(response)}"
            )

        return response

    def get_volt_amp(self) -> dict[str, object]:
        return decode_live_status_response(self.get_volt_amp_raw())

    def send_trigger(self, **conditions) -> int:
        """
        Build and send CMD_TRIGGER.

        Conditions use ezpd_protocol_v10.build_trigger_packet(), e.g.:
            msg_class="DATA", msg_type="EPR_REQUEST"
        """
        packet = build_trigger_packet(**conditions)
        with self._claimed() as handle:
            written = handle.bulkWrite(
                EP_CMD_OUT,
                packet,
                timeout=self.io_timeout_ms,
            )
        if written != len(packet):
            raise CY4500Error(
                f"short trigger write: {written}/{len(packet)} bytes"
            )
        return written

    def set_epr_request_trigger(self) -> int:
        return self.send_trigger(
            msg_class="DATA",
            msg_type="EPR_REQUEST",
        )

    def clear_trigger(self) -> int:
        packet = build_clear_trigger_packet()
        with self._claimed() as handle:
            written = handle.bulkWrite(
                EP_CMD_OUT,
                packet,
                timeout=self.io_timeout_ms,
            )
        if written != len(packet):
            raise CY4500Error(
                f"short trigger-clear write: {written}/{len(packet)} bytes"
            )
        return written

    def set_terminations(self, cc1="NONE", cc2="NONE") -> int:
        packet = build_termination_packet(cc1, cc2)
        with self._claimed() as handle:
            written = handle.bulkWrite(
                EP_CMD_OUT,
                packet,
                timeout=self.io_timeout_ms,
            )
        if written != len(packet):
            raise CY4500Error(
                f"short termination write: {written}/{len(packet)} bytes"
            )
        return written

    def clear_terminations(self) -> int:
        return self.set_terminations("NONE", "NONE")

    def start_capture(self) -> int:
        """
        Send the confirmed 8-byte CMD_START payload.

        For sustained capture prefer capture(), which keeps interface 0 claimed
        for START -> read loop -> STOP.
        """
        with self._claimed() as handle:
            written = handle.bulkWrite(
                EP_CMD_OUT,
                CMD_START_PAYLOAD,
                timeout=self.io_timeout_ms,
            )
        if written != len(CMD_START_PAYLOAD):
            raise CY4500Error(
                f"short START write: {written}/{len(CMD_START_PAYLOAD)} bytes"
            )
        return written

    def stop_capture(self) -> int:
        with self._claimed() as handle:
            written = handle.bulkWrite(
                EP_CMD_OUT,
                CMD_STOP_PAYLOAD,
                timeout=self.io_timeout_ms,
            )
        if written != len(CMD_STOP_PAYLOAD):
            raise CY4500Error(
                f"short STOP write: {written}/{len(CMD_STOP_PAYLOAD)} bytes"
            )
        return written

    def capture(
        self,
        seconds: Optional[float],
        *,
        record_callback: Optional[RecordCallback] = None,
        transfer_callback: Optional[TransferCallback] = None,
        status_callback: Optional[StatusCallback] = None,
        read_size: int = DEFAULT_CAPTURE_READ_SIZE,
        read_timeout_ms: int = DEFAULT_CAPTURE_READ_TIMEOUT_MS,
        loop_sleep_sec: float = DEFAULT_CAPTURE_LOOP_SLEEP_SEC,
        status_interval_sec: float = 1.0,
        strict_framing: bool = True,
        decode_ep81: bool = True,
        scope_enabled: bool = False,
        scope_sample_callback: Optional[ScopeSampleCallback] = None,
        scope_transfer_callback: Optional[ScopeTransferCallback] = None,
        scope_read_size: int = DEFAULT_SCOPE_READ_SIZE,
        scope_read_timeout_ms: int = DEFAULT_SCOPE_READ_TIMEOUT_MS,
        scope_timestamp_state: Optional[ScopeTimestampUnwrapper] = None,
        start_callback: Optional[Callable[[], None]] = None,
    ) -> CaptureStats:
        """
        Capture EP81, optionally servicing EP83 in the same START/STOP session.

        seconds:
            positive float -> fixed-duration capture
            None           -> continuous capture until Ctrl+C

        scope_enabled:
            False -> original EP81 behavior
            True  -> after each EP81 service, also read EP83 and decode the
                     confirmed 64-byte / 5x12-byte scope format.

        No host-side synchronization or timestamp offset is applied between
        EP81 and EP83. Their device timestamps are preserved for later testing.

        STOP is always sent from finally. Ctrl+C returns normal CaptureStats
        with interrupted=True instead of leaving the analyzer active.
        """
        if seconds is not None:
            seconds = float(seconds)
            if seconds <= 0:
                raise ValueError("seconds must be > 0 or None")

        if scope_timestamp_state is None:
            scope_timestamp_state = ScopeTimestampUnwrapper()

        stats = CaptureStats(requested_seconds=seconds)
        started_at = time.monotonic()
        deadline = None if seconds is None else started_at + seconds
        next_status = started_at + max(float(status_interval_sec), 0.05)

        with self._claimed() as handle:
            written = handle.bulkWrite(
                EP_CMD_OUT,
                CMD_START_PAYLOAD,
                timeout=self.io_timeout_ms,
            )
            if written != len(CMD_START_PAYLOAD):
                raise CY4500Error(
                    f"short START write: {written}/{len(CMD_START_PAYLOAD)} bytes"
                )

            if start_callback is not None:
                start_callback()

            original_error = None

            try:
                while deadline is None or time.monotonic() < deadline:
                    # EP81: same primary capture path used before v7.
                    try:
                        data = bytes(
                            handle.bulkRead(
                                EP_PD_IN,
                                int(read_size),
                                timeout=int(read_timeout_ms),
                            )
                        )
                    except usb1.USBErrorTimeout:
                        data = b""
                    except KeyboardInterrupt:
                        stats.interrupted = True
                        break

                    if data:
                        stats.usb_reads += 1

                        if data == EP81_TRANSFER_TERMINATOR:
                            stats.idle_transfers += 1
                        else:
                            stats.data_transfers += 1
                            stats.data_transfer_bytes += len(data)

                            if transfer_callback is not None:
                                transfer_callback(stats.data_transfers, data)

                            if decode_ep81:
                                try:
                                    records = split_capture_transfer(data)
                                except ValueError as exc:
                                    stats.framing_errors += 1
                                    if strict_framing:
                                        raise CY4500FramingError(
                                            f"EP81 framing error on data transfer "
                                            f"#{stats.data_transfers}: {exc}; "
                                            f"raw={data.hex(' ')}"
                                        ) from exc
                                    records = []

                                for raw_record in records:
                                    stats.records += 1
                                    decoded = decode_capture_record(raw_record)

                                    if record_callback is not None:
                                        record_callback(
                                            CaptureRecord(
                                                index=stats.records,
                                                raw=raw_record,
                                                decoded=decoded,
                                            )
                                        )

                    # EP83: stock Utility services this only while graph data
                    # is enabled. It ignores transfers <= 8 bytes and processes
                    # only complete 64-byte blocks from longer transfers.
                    if scope_enabled:
                        try:
                            scope_raw = bytes(
                                handle.bulkRead(
                                    EP_SCOPE_IN,
                                    int(scope_read_size),
                                    timeout=int(scope_read_timeout_ms),
                                )
                            )
                        except usb1.USBErrorTimeout:
                            scope_raw = b""
                        except KeyboardInterrupt:
                            stats.interrupted = True
                            break

                        if scope_raw:
                            # scope_reads is the host EP83 transfer index.
                            # Preserve every non-empty transfer boundary,
                            # including short transfers that Utility ignores.
                            stats.scope_reads += 1
                            scope_transfer_index = stats.scope_reads

                            scope_decoded = decode_scope_transfer(
                                scope_raw,
                                scope_timestamp_state,
                                is_epr=True,
                                product_id=self.pid,
                            )

                            if len(scope_raw) <= 8:
                                stats.scope_short_transfers += 1
                            else:
                                stats.scope_data_transfers += 1
                                stats.scope_transfer_bytes += len(scope_raw)

                                stats.scope_packets += (
                                    scope_decoded.complete_packets
                                )
                                stats.scope_samples += len(
                                    scope_decoded.samples
                                )
                                stats.scope_residual_bytes += len(
                                    scope_decoded.residual
                                )
                                stats.scope_nonzero_padding_packets += sum(
                                    1 for padding in scope_decoded.paddings
                                    if any(padding)
                                )

                            if scope_transfer_callback is not None:
                                scope_transfer_callback(
                                    scope_transfer_index,
                                    scope_raw,
                                    scope_decoded,
                                )

                            if scope_sample_callback is not None:
                                for sample in scope_decoded.samples:
                                    scope_sample_callback(
                                        scope_transfer_index,
                                        sample,
                                    )

                    if loop_sleep_sec:
                        try:
                            time.sleep(loop_sleep_sec)
                        except KeyboardInterrupt:
                            stats.interrupted = True
                            break

                    now = time.monotonic()
                    if status_callback is not None and now >= next_status:
                        stats.elapsed_seconds = now - started_at
                        status_callback(stats)
                        next_status = now + status_interval_sec

            except KeyboardInterrupt:
                stats.interrupted = True
            except BaseException as exc:
                original_error = exc
                raise
            finally:
                try:
                    stop_written = handle.bulkWrite(
                        EP_CMD_OUT,
                        CMD_STOP_PAYLOAD,
                        timeout=self.io_timeout_ms,
                    )
                    if stop_written != len(CMD_STOP_PAYLOAD):
                        raise CY4500Error(
                            f"short STOP write: "
                            f"{stop_written}/{len(CMD_STOP_PAYLOAD)} bytes"
                        )
                except Exception:
                    if original_error is None:
                        raise

        stats.elapsed_seconds = time.monotonic() - started_at
        return stats


def enumerate_matching_devices() -> list[dict[str, object]]:
    """
    Read-only USB descriptor enumeration for 04B4:FDEF.
    """
    rows: list[dict[str, object]] = []

    with usb1.USBContext() as ctx:
        for dev in ctx.getDeviceList(skip_on_error=True):
            if (
                dev.getVendorID() != USB_VID_CYPRESS
                or dev.getProductID() != USB_PID_CY4500_EPR
            ):
                continue

            interfaces = []

            for alt in dev.iterSettings():
                endpoints = []

                for ep in alt:
                    address = ep.getAddress()
                    attr = ep.getAttributes()
                    endpoints.append(
                        {
                            "address": address,
                            "direction": "IN" if address & 0x80 else "OUT",
                            "transfer_type": attr & 0x03,
                            "max_packet_size": ep.getMaxPacketSize(),
                        }
                    )

                interfaces.append(
                    {
                        "interface": alt.getNumber(),
                        "alt": alt.getAlternateSetting(),
                        "endpoints": endpoints,
                    }
                )

            rows.append(
                {
                    "vid": dev.getVendorID(),
                    "pid": dev.getProductID(),
                    "bus": dev.getBusNumber(),
                    "address": dev.getDeviceAddress(),
                    "configurations": dev.getNumConfigurations(),
                    "interfaces": interfaces,
                }
            )

    return rows


def _jsonable(value):
    if isinstance(value, bytes):
        return value.hex(" ")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value



def _role_strings(decoded: dict[str, object]) -> tuple[str, str]:
    """Return simple SOP Data Role / Power Role labels."""
    fields = decoded["fields"]

    if fields["PKT_TYPE"] != 0 or fields["SOP_TYPE"] != 0:
        return "", ""

    data_role = "DFP" if fields["DATA_ROLE"] else "UFP"
    power_role = "Source" if fields["POWER_ROLE"] else "Sink"
    return data_role, power_role


class CaptureSemanticTracker:
    """
    Stateful semantic layer over pure 64-byte record decoding.

    It intentionally lives in the controller rather than ezpd_protocol_v10:
      * remembers the latest SOURCE_CAPABILITIES for later REQUEST decoding
      * reassembles EPR_SOURCE_CAPABILITIES chunks
      * measures EPR KeepAlive cadence
      * summarizes request / voltage transitions

    Pure bitfield/packet decoders remain in ezpd_protocol_v10.py.
    """

    def __init__(self) -> None:
        self.source_pdos: list[dict[str, object]] = []
        self.epr_chunks: dict[int, bytes] = {}
        self.epr_expected_size: Optional[int] = None
        self.epr_capabilities: Optional[dict[str, object]] = None

        self.message_counts = Counter()
        self.keepalive_times_us: list[int] = []
        self.keepalive_ack_times_us: list[int] = []
        self.pd_vbus_values: list[float] = []
        self.volt_packets: list[tuple[int, float, int]] = []
        self.request_events: list[tuple[int, str, str, float]] = []
        self.ps_rdy_events: list[tuple[int, float, int]] = []

    def _try_finish_epr_caps(self) -> Optional[dict[str, object]]:
        if not self.epr_chunks or not self.epr_expected_size:
            return None

        assembled = bytearray()
        chunk_no = 0
        while chunk_no in self.epr_chunks:
            assembled.extend(self.epr_chunks[chunk_no])
            if len(assembled) >= self.epr_expected_size:
                payload = bytes(assembled[:self.epr_expected_size])
                result = decode_epr_source_capabilities_payload(payload)
                self.epr_capabilities = result
                return result
            chunk_no += 1

        return None

    def process(self, record: CaptureRecord) -> list[str]:
        d = record.decoded
        fields = d["fields"]
        effective = (
            d.get("effective_message_name")
            or d.get("message_name")
            or d.get("pkt_type_name")
            or "UNKNOWN"
        )
        self.message_counts[effective] += 1

        vbus = float(d.get("vbus_V", vbus_epr_volts(d["vbus_raw"])))
        details: list[str] = []

        if fields["PKT_TYPE"] == 0:
            self.pd_vbus_values.append(vbus)
        elif d.get("pkt_type_name") == "VOLT_PKT":
            self.volt_packets.append((d["sno"], vbus, d["start_time"]))

        semantic = d.get("semantic")

        if effective == "SOURCE_CAPABILITIES" and semantic:
            self.source_pdos = list(semantic.get("pdos") or [])
            if self.source_pdos:
                details.append(
                    "Source PDOs: "
                    + " | ".join(format_pdo(pdo) for pdo in self.source_pdos)
                )

        elif effective == "REQUEST" and semantic:
            request = semantic.get("request") or {}
            raw = request.get("raw")
            pos = int(request.get("object_position") or 0)
            selected = None
            if 1 <= pos <= len(self.source_pdos):
                selected = self.source_pdos[pos - 1]

            if raw is not None:
                decoded = decode_rdo(raw, selected_pdo=selected)
                semantic["request"] = decoded
                semantic["summary"] = format_rdo(decoded)
                summary = semantic["summary"]
                details.append("Request: " + summary)
                self.request_events.append(
                    (d["sno"], "REQUEST", summary, vbus)
                )

        elif effective == "EPR_REQUEST" and semantic:
            summary = semantic.get("summary")
            if summary:
                details.append("EPR Request: " + summary)
                self.request_events.append(
                    (d["sno"], "EPR_REQUEST", str(summary), vbus)
                )

        elif d.get("message_name") == "EPR_SOURCE_CAPABILITIES" and semantic:
            request_chunk = int(semantic.get("request_chunk") or 0)
            chunk_no = int(semantic.get("chunk_no") or 0)

            if request_chunk:
                details.append(f"EPR ext: Request Chunk #{chunk_no}")
            else:
                if chunk_no == 0:
                    self.epr_chunks = {}
                    self.epr_expected_size = None

                fragment = bytes(semantic.get("payload_fragment") or b"")
                self.epr_chunks[chunk_no] = fragment

                data_size = int(semantic.get("data_size") or 0)
                if data_size:
                    self.epr_expected_size = data_size

                details.append(
                    f"EPR ext: Chunk #{chunk_no}, "
                    f"fragment={len(fragment)}B, total={self.epr_expected_size}"
                )

                completed = self._try_finish_epr_caps()
                if completed is not None:
                    active = completed["active_pdos"]
                    details.append(
                        "EPR Source PDOs: "
                        + " | ".join(
                            format_pdo(pdo) for pdo in active
                        )
                    )

        elif effective == "EPR_KEEPALIVE":
            self.keepalive_times_us.append(int(d["start_time"]))

        elif effective == "EPR_KEEPALIVE_ACK":
            self.keepalive_ack_times_us.append(int(d["start_time"]))

        elif effective == "PS_RDY":
            self.ps_rdy_events.append(
                (d["sno"], vbus, int(d["start_time"]))
            )

        return details

    @staticmethod
    def _interval_stats_us(values: list[int]) -> Optional[tuple[float, float, float, float]]:
        if len(values) < 2:
            return None
        diffs = [
            (values[i] - values[i - 1]) & 0xFFFFFFFF
            for i in range(1, len(values))
        ]
        return (
            statistics.mean(diffs),
            statistics.median(diffs),
            min(diffs),
            max(diffs),
        )

    def summary_lines(self) -> list[str]:
        lines = [
            "CY4500-EPR semantic capture summary",
            "===================================",
        ]

        if self.pd_vbus_values:
            lines.append(
                "PD-packet VBUS range: "
                f"{min(self.pd_vbus_values):.4f} .. "
                f"{max(self.pd_vbus_values):.4f} V"
            )

        if self.volt_packets:
            first = self.volt_packets[0]
            last = self.volt_packets[-1]
            lines.append(
                f"VOLT_PKT: count={len(self.volt_packets)}, "
                f"first={first[1]:.4f} V, last={last[1]:.4f} V"
            )

        lines.append(
            f"EPR KeepAlive: {len(self.keepalive_times_us)} "
            f"/ Ack: {len(self.keepalive_ack_times_us)}"
        )

        ka = self._interval_stats_us(self.keepalive_times_us)
        if ka is not None:
            mean_us, median_us, min_us, max_us = ka

            # A long gap can occur between separate EPR sessions.  Report the
            # global median, then derive steady-state cadence by excluding
            # intervals > 1.5x that median.
            diffs = [
                (self.keepalive_times_us[i] - self.keepalive_times_us[i - 1])
                & 0xFFFFFFFF
                for i in range(1, len(self.keepalive_times_us))
            ]
            steady = [
                value for value in diffs
                if value <= median_us * 1.5
            ]

            lines.append(
                "EPR KeepAlive interval: "
                f"median={median_us / 1000:.3f} ms, "
                f"all-range={min_us / 1000:.3f}..{max_us / 1000:.3f} ms"
            )

            if steady:
                lines.append(
                    "EPR KeepAlive steady cadence: "
                    f"mean={statistics.mean(steady) / 1000:.3f} ms, "
                    f"min={min(steady) / 1000:.3f} ms, "
                    f"max={max(steady) / 1000:.3f} ms, "
                    f"session-gaps-excluded={len(diffs) - len(steady)}"
                )

        if self.source_pdos:
            lines.append("")
            lines.append("Latest SOURCE_CAPABILITIES:")
            for pdo in self.source_pdos:
                lines.append("  " + format_pdo(pdo))

        if self.epr_capabilities:
            lines.append("")
            lines.append("Latest reassembled EPR_SOURCE_CAPABILITIES:")
            for pdo in self.epr_capabilities["active_pdos"]:
                domain = pdo.get("domain", "")
                lines.append(f"  [{domain}] {format_pdo(pdo)}")

        if self.request_events:
            lines.append("")
            lines.append("Request sequence:")
            for sno, kind, summary, vbus in self.request_events:
                lines.append(
                    f"  SNo={sno} {kind}: {summary} "
                    f"(capture VBUS={vbus:.4f} V)"
                )

        if self.ps_rdy_events:
            lines.append("")
            lines.append("PS_RDY VBUS samples:")
            for sno, vbus, t_us in self.ps_rdy_events:
                lines.append(
                    f"  SNo={sno}: {vbus:.4f} V @ {t_us} us"
                )

        major = [
            "SOURCE_CAPABILITIES",
            "REQUEST",
            "EPR_MODE",
            "EPR_SOURCE_CAPABILITIES",
            "EPR_REQUEST",
            "EPR_KEEPALIVE",
            "EPR_KEEPALIVE_ACK",
            "PS_RDY",
        ]
        lines.append("")
        lines.append("Selected message counts:")
        for name in major:
            count = self.message_counts.get(name, 0)
            if count:
                lines.append(f"  {name}: {count}")

        return lines


class ScopeCSV:
    """CSV writer for real EP83 device samples only."""

    def __init__(self, fp):
        self.writer = csv.writer(fp)
        self.writer.writerow(SCOPE_CSV_COLUMNS)

    def write_sample(
        self,
        transfer_index: int,
        sample: ScopeSample,
        padding: bytes,
    ) -> None:
        self.writer.writerow(
            (
                transfer_index,
                sample.packet_index,
                sample.sample_index,
                sample.timestamp_raw,
                sample.timestamp_us,
                sample.vbus_raw,
                sample.vbus_mV,
                f"{sample.vbus_V:.9f}",
                sample.cc1_raw,
                sample.cc1_mV,
                f"{sample.cc1_V:.9f}",
                sample.cc2_raw,
                sample.cc2_mV,
                f"{sample.cc2_V:.9f}",
                sample.ibus_raw,
                sample.ibus_mA,
                f"{sample.ibus_A:.9f}",
                f"{sample.power_W:.9f}",
                padding.hex(" ").upper(),
            )
        )


class AnalyzerSchemaCSV:
    """
    CSV using Analyzer Utility 4.2.0's confirmed 15-column order.

    Direct capture + legacy Utility documentation confirm the analyzer
    time counters are microseconds. Duration/Delta/Start/End are emitted
    in microseconds while preserving the Utility-compatible column names.

    By default End Time is the real captured end counter.  bug_compatible=True
    reproduces Utility 4.2.0's export bug (End Time == Start Time).
    """

    def __init__(self, fp, *, bug_compatible_end_time: bool = False):
        self.writer = csv.writer(fp, lineterminator="\n")
        self.writer.writerow(CSV_COLUMNS)
        self.previous_end_time: Optional[int] = None
        self.bug_compatible_end_time = bool(bug_compatible_end_time)

    def write_record(self, record: CaptureRecord) -> None:
        d = record.decoded
        fields = d["fields"]
        is_pd = fields["PKT_TYPE"] == 0

        duration = u32_elapsed(d["end_time"], d["start_time"])
        delta = (
            ""
            if self.previous_end_time is None
            else u32_elapsed(d["start_time"], self.previous_end_time)
        )
        self.previous_end_time = d["end_time"]

        data_role, power_role = _role_strings(d)

        if is_pd:
            sop = d["sop_name"]
            message = d["message_name"] or ""
            msg_id = fields["MSG_ID"]
            obj_count = fields["OBJ_COUNT"]
            rev = d["spec_rev_name"]
            data_text = capture_record_logical_message_bytes(record.raw).hex(" ")
        else:
            sop = ""
            message = d["pkt_type_name"]
            msg_id = ""
            obj_count = ""
            rev = ""
            data_role = ""
            power_role = ""
            data_text = ""

        end_time = (
            d["start_time"]
            if self.bug_compatible_end_time
            else d["end_time"]
        )

        self.writer.writerow(
            (
                d["sno"],
                fields["OK"],
                sop,
                message,
                msg_id,
                data_role,
                power_role,
                obj_count,
                rev,
                duration,
                delta,
                f"{vbus_epr_volts(d['vbus_raw']):.6f}",
                data_text,
                d["start_time"],
                end_time,
            )
        )

def _compact_record(record: CaptureRecord) -> str:
    d = record.decoded
    fields = d["fields"]

    extra = ""
    ext = d.get("extended_header")
    if ext:
        extra = (
            f" XHDR=0x{ext['raw']:04X}"
            f" Size={ext['data_size']}"
            f" ReqChunk={ext['request_chunk']}"
            f" Chunk={ext['chunk_no']}"
            f" Chunked={ext['chunked']}"
            f" TChunk={fields['CHUNK_NO']}"
        )

    return (
        f"REC #{record.index:04d} "
        f"SNo={d['sno']} "
        f"PKT={d['pkt_type_name']} "
        f"SOP={d['sop_name']} "
        f"{d['message_class'] or '-':8} "
        f"{(d.get('effective_message_name') or d.get('message_name') or '-'):28} "
        f"ID={fields['MSG_ID']} "
        f"NDO={fields['OBJ_COUNT']} "
        f"Rev={d['spec_rev_name']} "
        f"OK={fields['OK']} "
        f"CRC={fields['CRC_ERROR']} "
        f"EOP={fields['EOP_ERROR']} "
        f"IDLE={fields['IDLE_ERROR']} "
        f"VBUS={vbus_epr_volts(d['vbus_raw']):.4f}V"
        f"{extra}"
    )


def _cmd_usb_info(_args) -> int:
    rows = enumerate_matching_devices()

    if not rows:
        print("CY4500-EPR 04B4:FDEF not found")
        return 1

    type_names = {
        0: "CONTROL",
        1: "ISOCHRONOUS",
        2: "BULK",
        3: "INTERRUPT",
    }

    for row in rows:
        print("CY4500-EPR FOUND")
        print(f"VID:PID        = {row['vid']:04X}:{row['pid']:04X}")
        print(f"Bus            = {row['bus']}")
        print(f"Address        = {row['address']}")
        print(f"Configurations = {row['configurations']}")

        for intf in row["interfaces"]:
            print(
                f"Interface={intf['interface']} "
                f"Alt={intf['alt']}"
            )
            for ep in intf["endpoints"]:
                print(
                    f"  EP=0x{ep['address']:02X} "
                    f"{ep['direction']:3} "
                    f"{type_names.get(ep['transfer_type'], 'UNKNOWN'):11} "
                    f"MaxPacket={ep['max_packet_size']}"
                )

    return 0


def _cmd_version(_args) -> int:
    with CY4500EPR() as dev:
        raw = dev.get_version_raw()
        print(f"Raw     : {raw.hex(' ')}")
        print(f"Version : {dev.format_version(raw)}")
    return 0



def _parse_trigger_msg_type(value: str):
    """
    CLI helper: accept either a symbolic PD message name or a numeric
    message-type index (decimal or 0x-prefixed hex).
    """
    s = str(value).strip()
    if not s:
        raise ValueError("empty --msg-type")

    if re.fullmatch(r"[0-9]+", s):
        return int(s, 10)
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", s):
        return int(s, 16)
    return s.upper()


def _trigger_conditions_from_args(args) -> dict[str, object]:
    conditions: dict[str, object] = {}

    if args.start_sno is not None:
        conditions["start_sno"] = args.start_sno
    if args.end_sno is not None:
        conditions["end_sno"] = args.end_sno
    if args.sop is not None:
        conditions["sop"] = args.sop
    if args.msg_class is not None:
        conditions["msg_class"] = args.msg_class
    if args.msg_type is not None:
        conditions["msg_type"] = _parse_trigger_msg_type(args.msg_type)
    if args.obj_count is not None:
        conditions["obj_count"] = args.obj_count
    if args.msg_id is not None:
        conditions["msg_id"] = args.msg_id

    return conditions


def _print_trigger_types(msg_class: str) -> None:
    classes = (
        ("CONTROL", "DATA", "EXTENDED")
        if msg_class == "ALL"
        else (msg_class,)
    )
    for class_name in classes:
        table = MESSAGE_TYPES_BY_CLASS[class_name]
        print(f"{class_name}:")
        for index, name in enumerate(table):
            print(f"  {index:2d}  {name}")
        if class_name != classes[-1]:
            print()


def _cmd_trigger(args) -> int:
    arm_requested = bool(
        getattr(args, "arm", False)
        or getattr(args, "arm_seconds", None) is not None
    )

    if getattr(args, "arm_seconds", None) is not None:
        if args.arm_seconds <= 0:
            raise ValueError("--arm-seconds must be > 0")

    if getattr(args, "clear_on_exit", False) and not arm_requested:
        raise ValueError("--clear-on-exit requires --arm or --arm-seconds")

    if args.list_types is not None:
        if args.clear:
            raise ValueError("--list-types cannot be combined with --clear")
        if arm_requested:
            raise ValueError("--list-types cannot be combined with --arm")
        if getattr(args, "clear_on_exit", False):
            raise ValueError(
                "--list-types cannot be combined with --clear-on-exit"
            )
        conditions = _trigger_conditions_from_args(args)
        if conditions:
            raise ValueError(
                "--list-types cannot be combined with trigger conditions"
            )
        _print_trigger_types(args.list_types)
        return 0

    conditions = _trigger_conditions_from_args(args)

    if args.clear:
        if conditions:
            raise ValueError(
                "--clear cannot be combined with trigger conditions"
            )
        if arm_requested:
            raise ValueError("--clear cannot be combined with --arm")
        packet = build_clear_trigger_packet()
        action = "CLEAR all trigger conditions"
    else:
        if not conditions:
            raise ValueError(
                "trigger requires at least one condition, --clear, "
                "or --list-types"
            )
        packet = build_trigger_packet(**conditions)
        action = "SET trigger"

    print(action)
    if conditions:
        print("Conditions (combined with AND by firmware):")
        for key, value in conditions.items():
            print(f"  {key}={value}")
    print(f"TX ({len(packet)} B): {packet.hex(' ')}")

    if arm_requested:
        seconds = getattr(args, "arm_seconds", None)
        print()
        print("ARM sequence:")
        print(
            f"  1. CMD_TRIGGER  ({len(packet)} B) - configure trigger"
        )
        print(
            f"  2. CMD_START    ({len(CMD_START_PAYLOAD)} B) - "
            "enter active/green measurement mode"
        )
        print("  3. Drain EP81 + EP83 while trigger evaluator is active")
        if seconds is None:
            print("  4. Wait until Ctrl+C")
        else:
            print(f"  4. Wait {seconds:.3f} s")
        print(
            f"  5. CMD_STOP     ({len(CMD_STOP_PAYLOAD)} B) - "
            "leave active measurement mode"
        )
        if getattr(args, "clear_on_exit", False):
            print("  6. CMD_TRIGGER clear - disable all trigger criteria")

        if args.dry_run:
            print()
            print(
                f"START TX ({len(CMD_START_PAYLOAD)} B): "
                f"{CMD_START_PAYLOAD.hex(' ')}"
            )
            print(
                f"STOP  TX ({len(CMD_STOP_PAYLOAD)} B): "
                f"{CMD_STOP_PAYLOAD.hex(' ')}"
            )
            if getattr(args, "clear_on_exit", False):
                clear_packet = build_clear_trigger_packet()
                print(
                    f"CLEAR TX ({len(clear_packet)} B): "
                    f"{clear_packet.hex(' ')}"
                )
            print("DRY RUN: no USB access performed.")
            return 0
    elif args.dry_run:
        print("DRY RUN: no USB write performed.")
        return 0

    with CY4500EPR() as dev:
        if args.clear:
            written = dev.clear_trigger()
            print(f"USB write accepted: {written} bytes")
            print(
                "Note: trigger clear has no semantic ACK/readback; success "
                "here means the USB OUT transfer completed."
            )
            return 0

        written = dev.send_trigger(**conditions)
        print(f"USB write accepted: {written} bytes")
        print(
            "Trigger configured. Hardware testing shows trigger evaluation "
            "requires the CMD_START active/green measurement mode."
        )

        if not arm_requested:
            print(
                "Trigger is CONFIGURED but not armed. "
                "Use --arm to start the measurement engine."
            )
            return 0

        quiet = bool(getattr(args, "quiet", False))

        def on_started() -> None:
            print(
                "Measurement engine STARTED; trigger ARMED "
                "(green LED expected)."
            )
            if seconds is None:
                print("Press Ctrl+C to disarm/STOP.")
            else:
                print(f"Armed for {seconds:.3f} s.")

        def on_arm_status(stats: CaptureStats) -> None:
            if quiet:
                return
            print(
                f"[armed] elapsed={stats.elapsed_seconds:.1f}s "
                f"EP81 reads={stats.usb_reads} "
                f"data={stats.data_transfers} "
                f"EP83 xfers={stats.scope_reads} "
                f"samples={stats.scope_samples}"
            )

        stats = None
        capture_error = None
        try:
            # Trigger evaluation is device-side. Host capture data is not
            # decoded or saved here, but both streaming IN endpoints are
            # continuously serviced so START-mode buffers do not accumulate.
            stats = dev.capture(
                seconds,
                decode_ep81=False,
                strict_framing=False,
                scope_enabled=True,
                status_callback=on_arm_status,
                start_callback=on_started,
            )
        except BaseException as exc:
            capture_error = exc
            raise
        finally:
            # capture() sends CMD_STOP in its own finally. Optional trigger
            # clear happens only after that STOP, and must not mask a capture
            # exception.
            if getattr(args, "clear_on_exit", False):
                try:
                    cleared = dev.clear_trigger()
                    print(f"Trigger cleared on exit ({cleared} bytes).")
                except Exception:
                    if capture_error is None:
                        raise

        print("Measurement engine STOPPED; trigger evaluator disarmed.")
        if stats is not None:
            print(
                "Drain summary: "
                f"elapsed={stats.elapsed_seconds:.3f}s, "
                f"EP81 reads={stats.usb_reads}, "
                f"EP81 data transfers={stats.data_transfers}, "
                f"EP83 transfers={stats.scope_reads}, "
                f"EP83 samples={stats.scope_samples}"
            )

        if not getattr(args, "clear_on_exit", False):
            print(
                "Trigger criteria were not cleared; only CMD_STOP was sent. "
                "Re-arm with --arm, or disable them with trigger --clear."
            )

    return 0


def _cmd_trigger_arm(args) -> int:
    """
    Arm the measurement/trigger evaluator WITHOUT sending CMD_TRIGGER.

    Purpose:
      Verify whether previously configured trigger criteria survive CMD_STOP
      and become active again on a later CMD_START.

    Sequence:
      CMD_START -> drain EP81/EP83 -> CMD_STOP
    Optional:
      --clear-on-exit sends the normal trigger-clear packet only after STOP.
    """
    seconds = args.seconds
    if seconds is not None and seconds <= 0:
        raise ValueError("--seconds must be > 0")

    print("ARM existing trigger criteria")
    print("IMPORTANT: no CMD_TRIGGER packet will be sent.")
    print()
    print("ARM sequence:")
    print(
        f"  1. CMD_START    ({len(CMD_START_PAYLOAD)} B) - "
        "enter active/green measurement mode"
    )
    print("  2. Drain EP81 + EP83 while trigger evaluator is active")
    if seconds is None:
        print("  3. Wait until Ctrl+C")
    else:
        print(f"  3. Wait {seconds:.3f} s")
    print(
        f"  4. CMD_STOP     ({len(CMD_STOP_PAYLOAD)} B) - "
        "leave active measurement mode"
    )
    if args.clear_on_exit:
        print("  5. CMD_TRIGGER clear - disable all trigger criteria")

    if args.dry_run:
        print()
        print(
            f"START TX ({len(CMD_START_PAYLOAD)} B): "
            f"{CMD_START_PAYLOAD.hex(' ')}"
        )
        print(
            f"STOP  TX ({len(CMD_STOP_PAYLOAD)} B): "
            f"{CMD_STOP_PAYLOAD.hex(' ')}"
        )
        if args.clear_on_exit:
            clear_packet = build_clear_trigger_packet()
            print(
                f"CLEAR TX ({len(clear_packet)} B): "
                f"{clear_packet.hex(' ')}"
            )
        print("DRY RUN: no USB access performed.")
        return 0

    with CY4500EPR() as dev:
        def on_started() -> None:
            print(
                "Measurement engine STARTED; existing trigger criteria "
                "should now be armed (green LED expected)."
            )
            if seconds is None:
                print("Press Ctrl+C to disarm/STOP.")
            else:
                print(f"Armed for {seconds:.3f} s.")

        def on_arm_status(stats: CaptureStats) -> None:
            if args.quiet:
                return
            print(
                f"[armed-existing] elapsed={stats.elapsed_seconds:.1f}s "
                f"EP81 reads={stats.usb_reads} "
                f"data={stats.data_transfers} "
                f"EP83 xfers={stats.scope_reads} "
                f"samples={stats.scope_samples}"
            )

        stats = None
        capture_error = None
        try:
            # Intentionally DO NOT call send_trigger()/clear_trigger() here.
            # The whole point is to test whether device-side trigger criteria
            # persist across a previous CMD_STOP.
            stats = dev.capture(
                seconds,
                decode_ep81=False,
                strict_framing=False,
                scope_enabled=True,
                status_callback=on_arm_status,
                start_callback=on_started,
            )
        except BaseException as exc:
            capture_error = exc
            raise
        finally:
            # capture() sends CMD_STOP in its own finally.
            if args.clear_on_exit:
                try:
                    cleared = dev.clear_trigger()
                    print(f"Trigger cleared on exit ({cleared} bytes).")
                except Exception:
                    if capture_error is None:
                        raise

        print("Measurement engine STOPPED; trigger evaluator disarmed.")
        if stats is not None:
            print(
                "Drain summary: "
                f"elapsed={stats.elapsed_seconds:.3f}s, "
                f"EP81 reads={stats.usb_reads}, "
                f"EP81 data transfers={stats.data_transfers}, "
                f"EP83 transfers={stats.scope_reads}, "
                f"EP83 samples={stats.scope_samples}"
            )

        if not args.clear_on_exit:
            print(
                "No trigger configuration packet was sent before or after ARM. "
                "Any MTR/SOM/EOM activity therefore comes from criteria retained "
                "inside the CY4500-EPR."
            )

    return 0

def _cmd_termination_v11(args) -> int:
    if args.clear:
        if args.cc1 is not None or args.cc2 is not None:
            raise ValueError("--clear cannot be combined with --cc1/--cc2")
        cc1 = "NONE"
        cc2 = "NONE"
        action = "CLEAR CC terminations"
    else:
        if args.cc1 is None and args.cc2 is None:
            raise ValueError(
                "termination requires both --cc1 and --cc2, or --clear"
            )
        if args.cc1 is None or args.cc2 is None:
            raise ValueError(
                "specify both --cc1 and --cc2 explicitly; "
                "there is no device readback to preserve an unspecified side"
            )
        cc1 = args.cc1.upper()
        cc2 = args.cc2.upper()
        action = "SET CC terminations"

    packet = build_termination_packet(cc1, cc2)

    print(action)
    print(f"  CC1={cc1}")
    print(f"  CC2={cc2}")
    print(f"TX ({len(packet)} B): {packet.hex(' ')}")

    if cc1 != "NONE" or cc2 != "NONE":
        print(
            "WARNING: CC terminations physically alter the CC network. "
            "Use RP/RD/RA only when that electrical condition is intentional."
        )

    if args.dry_run:
        print("DRY RUN: no USB write performed.")
        return 0

    with CY4500EPR() as dev:
        written = dev.set_terminations(cc1, cc2)

    print(f"USB write accepted: {written} bytes")
    print(
        "Note: this command has no semantic ACK/readback; success here means "
        "the USB OUT transfer completed."
    )
    return 0


def _cmd_termination_clear(_args) -> int:
    packet = build_termination_packet("NONE", "NONE")
    print("CLEAR CC terminations")
    print(f"TX ({len(packet)} B): {packet.hex(' ')}")
    with CY4500EPR() as dev:
        written = dev.clear_terminations()
    print(f"USB write accepted: {written} bytes")
    return 0

def _cmd_trigger_epr_request(_args) -> int:
    with CY4500EPR() as dev:
        written = dev.set_epr_request_trigger()
    print(f"EPR_REQUEST trigger set ({written} bytes)")
    return 0


def _cmd_trigger_clear(_args) -> int:
    with CY4500EPR() as dev:
        written = dev.clear_trigger()
    print(f"Trigger cleared ({written} bytes)")
    return 0


def _cmd_termination(args) -> int:
    # Backward-compatible function name used by older parser wiring.
    return _cmd_termination_v11(args)



def _cmd_live_status(args) -> int:
    """
    Poll read-only live VBUS / current / CC1 / CC2 status.

    Conversion exactly reproduces EZ-PD Protocol Analyzer Utility 4.2.0
    VoltAmpUpdater for CY4500-EPR PID 0xFDEF.
    """
    count = args.count
    interval = float(args.interval)
    median_n = int(args.median)

    if count is not None and count <= 0:
        raise ValueError("--count must be > 0")
    if interval < 0:
        raise ValueError("--interval must be >= 0")
    if median_n <= 0:
        raise ValueError("--median must be > 0")

    history = deque(maxlen=median_n)

    csv_fp = None
    writer = None
    if args.csv:
        csv_path = Path(args.csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_fp = csv_path.open("w", encoding="utf-8-sig", newline="")
        writer = csv.writer(csv_fp)
        writer.writerow(
            (
                "sample",
                "monotonic_s",
                "raw_hex",
                "raw0_vbus",
                "raw1_ibus",
                "raw2_cc1",
                "raw3_cc2",
                "vbus_mV",
                "vbus_V",
                "ibus_mA",
                "ibus_A",
                "ibus_median_A",
                "cc1_mV",
                "cc1_V",
                "cc2_mV",
                "cc2_V",
                "power_instant_W",
                "power_median_W",
            )
        )

    print("Opening CY4500-EPR...")
    print(
        "Live status: CMD_GET_VOLT_AMP 0x11 "
        "(Analyzer Utility 4.2.0 exact conversion)"
    )
    print(
        f"Documented typical live-current accuracy: "
        f"+/-{LIVE_CURRENT_DOC_TYPICAL_ACCURACY_A:.2f} A"
    )
    print(
        "word0=VBUS, word1=IBUS, word2=CC1, word3=CC2"
    )
    print()

    sample_no = 0

    try:
        with CY4500EPR() as dev:
            while count is None or sample_no < count:
                sample_no += 1
                t = time.monotonic()
                result = dev.get_volt_amp()

                ibus = float(result["ibus_A"])
                history.append(ibus)
                median_ibus = statistics.median(history)

                display_ibus = max(0.0, median_ibus)
                vbus_v = float(result["vbus_V"])
                power_instant_w = vbus_v * ibus
                power_median_w = vbus_v * display_ibus

                print(
                    f"[{sample_no:05d}] "
                    f"VBUS={vbus_v:7.3f} V "
                    f"IBUS={display_ibus:6.3f} A "
                    f"P={power_median_w:7.2f} W "
                    f"CC1={float(result['cc1_V']):5.3f} V "
                    f"CC2={float(result['cc2_V']):5.3f} V "
                    f"(instant I={ibus:6.3f} A, "
                    f"P={power_instant_w:7.2f} W; "
                    f"raw={result['raw0']}/{result['raw1']}/"
                    f"{result['raw2']}/{result['raw3']}, "
                    f"median{len(history)})"
                )

                if writer is not None:
                    writer.writerow(
                        (
                            sample_no,
                            f"{t:.9f}",
                            result["raw"].hex(" "),
                            result["raw0"],
                            result["raw1"],
                            result["raw2"],
                            result["raw3"],
                            result["vbus_mV"],
                            f"{float(result['vbus_V']):.9f}",
                            result["ibus_mA"],
                            f"{ibus:.9f}",
                            f"{median_ibus:.9f}",
                            result["cc1_mV"],
                            f"{float(result['cc1_V']):.9f}",
                            result["cc2_mV"],
                            f"{float(result['cc2_V']):.9f}",
                            f"{power_instant_w:.9f}",
                            f"{power_median_w:.9f}",
                        )
                    )
                    csv_fp.flush()

                if count is None or sample_no < count:
                    if interval:
                        time.sleep(interval)

    except KeyboardInterrupt:
        print()
        print("Stopped by Ctrl+C.")
    finally:
        if csv_fp is not None:
            csv_fp.close()

    return 0




TRANSITION_CSV_COLUMNS = (
    "request_row_index",
    "request_sno",
    "direction",
    "target_voltage_V",
    "requested_current_A",
    "request_start_us",
    "request_end_us",
    "accept_start_us",
    "accept_latency_us",
    "ps_rdy_start_us",
    "ps_rdy_latency_us",
    "request_scope_timestamp_us",
    "request_scope_vbus_V",
    "ps_rdy_scope_timestamp_us",
    "ps_rdy_scope_vbus_V",
    "baseline_vbus_V",
    "baseline_noise_mad_V",
    "movement_threshold_V",
    "movement_start_us",
    "movement_latency_us",
    "target_crossing_us",
    "target_crossing_latency_us",
    "target_band_first_entry_us",
    "target_band_first_entry_latency_us",
    "settling_us",
    "settling_latency_us",
    "settling_hold_us",
    "target_band_fraction",
    "average_slew_V_per_s",
    "observed_plateau_V",
    "observed_plateau_mad_V",
    "observed_plateau_sample_count",
    "observed_band_fraction",
    "observed_band_first_entry_us",
    "observed_band_first_entry_latency_us",
    "observed_settling_us",
    "observed_settling_latency_us",
    "observed_average_slew_V_per_s",
    "flags",
)


def _optional_int(value):
    if value is None or value == "":
        return None
    try:
        if math.isnan(float(value)):
            return None
    except Exception:
        pass
    return int(float(value))


def _optional_float(value):
    if value is None or value == "":
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else value


def _load_pd_capture_csv(path: Path) -> list[SyncPDSample]:
    rows: list[SyncPDSample] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        for row_index, row in enumerate(reader):
            start_us = _optional_int(row.get("Start Time"))
            end_us = _optional_int(row.get("End Time"))
            if start_us is None or end_us is None:
                continue
            data_text = (row.get("Data") or "").strip()
            try:
                data = bytes.fromhex(data_text) if data_text else b""
            except ValueError:
                data = b""
            rows.append(
                SyncPDSample(
                    row_index=row_index,
                    sno=_optional_int(row.get("Sno")),
                    message=(row.get("Message") or "").strip(),
                    start_us=start_us,
                    end_us=end_us,
                    vbus_V=_optional_float(row.get("Vbus(V)")),
                    data=data,
                )
            )
    return rows


def _load_scope_csv(path: Path) -> list[SyncScopeSample]:
    samples: list[SyncScopeSample] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            timestamp_us = _optional_int(row.get("Timestamp(us)"))
            vbus_v = _optional_float(row.get("Vbus(V)"))
            if timestamp_us is None or vbus_v is None:
                continue
            samples.append(
                SyncScopeSample(
                    timestamp_us=timestamp_us,
                    vbus_V=vbus_v,
                    ibus_A=_optional_float(row.get("Ibus(A)")),
                    cc1_V=_optional_float(row.get("CC1(V)")),
                    cc2_V=_optional_float(row.get("CC2(V)")),
                )
            )
    samples.sort(key=lambda s: s.timestamp_us)
    return samples


def _transition_row(a: AVSTransitionAnalysis) -> dict[str, object]:
    row = {}
    for name in TRANSITION_CSV_COLUMNS:
        if name == "flags":
            row[name] = ";".join(a.flags)
        else:
            row[name] = getattr(a, name)
    return row



HUMAN_TRANSITION_SUMMARY_COLUMNS = (
    "transition",
    "request_sno",
    "direction",
    "from_V",
    "target_V",
    "observed_plateau_V",
    "target_minus_plateau_mV",
    "accept_ms",
    "movement_start_ms",
    "ps_rdy_ms",
    "vbus_at_ps_rdy_V",
    "ps_rdy_vs_plateau_percent",
    "ps_rdy_error_from_plateau_mV",
    "absolute_settle_status",
    "absolute_settle_ms",
    "ps_rdy_to_absolute_settle_ms",
    "absolute_slew_V_per_s",
    "observed_settle_status",
    "observed_settle_ms",
    "ps_rdy_to_observed_settle_ms",
    "movement_to_observed_settle_ms",
    "observed_slew_V_per_s",
    "flags",
)


def _ms_from_us(value):
    return None if value is None else float(value) / 1000.0


def _human_summary_row(
    transition_no: int,
    a: AVSTransitionAnalysis,
) -> dict[str, object]:
    plateau = a.observed_plateau_V
    ps_v = a.ps_rdy_scope_vbus_V

    target_minus_plateau_mv = (
        None
        if plateau is None
        else (a.target_voltage_V - plateau) * 1000.0
    )

    ps_rdy_vs_plateau_percent = (
        None
        if plateau in (None, 0.0) or ps_v is None
        else ps_v / plateau * 100.0
    )

    ps_rdy_error_from_plateau_mv = (
        None
        if plateau is None or ps_v is None
        else (ps_v - plateau) * 1000.0
    )

    ps_to_abs_ms = (
        None
        if a.ps_rdy_start_us is None or a.settling_us is None
        else (a.settling_us - a.ps_rdy_start_us) / 1000.0
    )
    ps_to_obs_ms = (
        None
        if a.ps_rdy_start_us is None or a.observed_settling_us is None
        else (a.observed_settling_us - a.ps_rdy_start_us) / 1000.0
    )
    move_to_obs_ms = (
        None
        if a.movement_start_us is None or a.observed_settling_us is None
        else (a.observed_settling_us - a.movement_start_us) / 1000.0
    )

    if a.settling_us is not None:
        abs_status = "SETTLED"
    elif "target_band_not_reached" in a.flags:
        abs_status = "TARGET_BAND_NOT_REACHED"
    elif "settling_not_observed" in a.flags:
        abs_status = "NOT_SETTLED"
    else:
        abs_status = "UNAVAILABLE"

    if a.observed_settling_us is not None:
        obs_status = "SETTLED"
    elif "observed_plateau_not_found" in a.flags:
        obs_status = "PLATEAU_NOT_FOUND"
    elif "observed_settling_not_observed" in a.flags:
        obs_status = "NOT_SETTLED"
    else:
        obs_status = "UNAVAILABLE"

    return {
        "transition": transition_no,
        "request_sno": a.request_sno,
        "direction": a.direction,
        "from_V": a.baseline_vbus_V,
        "target_V": a.target_voltage_V,
        "observed_plateau_V": plateau,
        "target_minus_plateau_mV": target_minus_plateau_mv,
        "accept_ms": _ms_from_us(a.accept_latency_us),
        "movement_start_ms": _ms_from_us(a.movement_latency_us),
        "ps_rdy_ms": _ms_from_us(a.ps_rdy_latency_us),
        "vbus_at_ps_rdy_V": ps_v,
        "ps_rdy_vs_plateau_percent": ps_rdy_vs_plateau_percent,
        "ps_rdy_error_from_plateau_mV": ps_rdy_error_from_plateau_mv,
        "absolute_settle_status": abs_status,
        "absolute_settle_ms": _ms_from_us(a.settling_latency_us),
        "ps_rdy_to_absolute_settle_ms": ps_to_abs_ms,
        "absolute_slew_V_per_s": a.average_slew_V_per_s,
        "observed_settle_status": obs_status,
        "observed_settle_ms": _ms_from_us(a.observed_settling_latency_us),
        "ps_rdy_to_observed_settle_ms": ps_to_obs_ms,
        "movement_to_observed_settle_ms": move_to_obs_ms,
        "observed_slew_V_per_s": a.observed_average_slew_V_per_s,
        "flags": ";".join(a.flags),
    }


def _fmt_num(value, digits=3, suffix=""):
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}{suffix}"


def _write_human_transition_summary(
    analyses: list[AVSTransitionAnalysis],
    *,
    csv_path: Path,
    text_path: Path,
) -> None:
    rows = [
        _human_summary_row(i, a)
        for i, a in enumerate(analyses, 1)
    ]

    with csv_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=HUMAN_TRANSITION_SUMMARY_COLUMNS,
        )
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "CY4500-EPR AVS per-transition summary",
        "====================================",
        "",
        (
            "T#  SNo  Dir    From -> Target   Plateau   Move     PS_RDY   "
            "V@PS / Plateau   Obs.Set  PS->Obs   Obs.Slew   Abs"
        ),
        (
            "--  ---  ----  ---------------  --------  -------  -------  "
            "---------------  -------  -------  ---------  ----------------------"
        ),
    ]

    for row in rows:
        from_to = (
            f"{_fmt_num(row['from_V'], 2):>5}"
            f" -> {_fmt_num(row['target_V'], 2):>5}V"
        )
        ps_ratio = (
            "-"
            if row["ps_rdy_vs_plateau_percent"] is None
            else f"{row['vbus_at_ps_rdy_V']:.3f}V / "
                 f"{row['ps_rdy_vs_plateau_percent']:.1f}%"
        )
        abs_text = (
            row["absolute_settle_status"]
            if row["absolute_settle_ms"] is None
            else f"{row['absolute_settle_ms']:.1f}ms"
        )
        lines.append(
            f"{row['transition']:>2}  "
            f"{str(row['request_sno']):>3}  "
            f"{row['direction']:<4}  "
            f"{from_to:<15}  "
            f"{_fmt_num(row['observed_plateau_V'], 3, 'V'):>8}  "
            f"{_fmt_num(row['movement_start_ms'], 1, 'ms'):>7}  "
            f"{_fmt_num(row['ps_rdy_ms'], 1, 'ms'):>7}  "
            f"{ps_ratio:>15}  "
            f"{_fmt_num(row['observed_settle_ms'], 1, 'ms'):>7}  "
            f"{_fmt_num(row['ps_rdy_to_observed_settle_ms'], 1, 'ms'):>7}  "
            f"{_fmt_num(row['observed_slew_V_per_s'], 2, 'V/s'):>9}  "
            f"{abs_text}"
        )

    lines.extend(
        [
            "",
            "Column meanings:",
            "  Move      = EPR_REQUEST -> sustained VBUS movement start",
            "  PS_RDY    = EPR_REQUEST -> PS_RDY",
            "  V@PS      = EP83 VBUS nearest PS_RDY",
            "  Plateau   = observed final plateau from stable EP83 data",
            "  Obs.Set   = EPR_REQUEST -> observed-plateau settle",
            "  PS->Obs   = PS_RDY -> observed-plateau settle "
            "(negative means settled before PS_RDY)",
            "  Abs       = requested-target absolute settling result",
            "",
            "Note: absolute target and observed plateau are intentionally "
            "reported separately.",
        ]
    )
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def _write_transition_outputs(
    analyses: list[AVSTransitionAnalysis],
    *,
    csv_path: Path,
    summary_path: Path,
    settings: dict[str, object],
    human_csv_path: Optional[Path] = None,
    human_text_path: Optional[Path] = None,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    with csv_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=TRANSITION_CSV_COLUMNS)
        writer.writeheader()
        for a in analyses:
            writer.writerow(_transition_row(a))

    lines = [
        "CY4500-EPR synchronized AVS transition analysis",
        "==============================================",
        "Clock handling: EP81 and EP83 device timestamps are used as captured;",
        "no host-time offset or fitted clock offset is applied.",
        "",
        "Analysis settings:",
    ]
    for key, value in settings.items():
        lines.append(f"  {key}: {value}")

    lines.extend(["", f"Transitions analyzed: {len(analyses)}", ""])

    for n, a in enumerate(analyses, 1):
        flags = ", ".join(a.flags) if a.flags else "none"
        lines.extend(
            [
                (
                    f"[{n}] SNo={a.request_sno} {a.direction} "
                    f"target={a.target_voltage_V:.3f} V "
                    f"current={a.requested_current_A if a.requested_current_A is not None else 'n/a'} A"
                ),
                f"  Request          : {a.request_start_us} us",
                (
                    f"  ACCEPT           : {a.accept_start_us} us "
                    f"(+{a.accept_latency_us/1000:.3f} ms)"
                    if a.accept_start_us is not None else
                    "  ACCEPT           : not matched"
                ),
                (
                    f"  PS_RDY           : {a.ps_rdy_start_us} us "
                    f"(+{a.ps_rdy_latency_us/1000:.3f} ms)"
                    if a.ps_rdy_start_us is not None else
                    "  PS_RDY           : not matched"
                ),
                (
                    f"  EP83 @ Request   : {a.request_scope_vbus_V:.6f} V "
                    f"@ {a.request_scope_timestamp_us} us"
                    if a.request_scope_vbus_V is not None else
                    "  EP83 @ Request   : unavailable"
                ),
                (
                    f"  EP83 @ PS_RDY    : {a.ps_rdy_scope_vbus_V:.6f} V "
                    f"@ {a.ps_rdy_scope_timestamp_us} us"
                    if a.ps_rdy_scope_vbus_V is not None else
                    "  EP83 @ PS_RDY    : unavailable"
                ),
                (
                    f"  Baseline         : {a.baseline_vbus_V:.6f} V; "
                    f"MAD={a.baseline_noise_mad_V:.6f} V; "
                    f"move threshold={a.movement_threshold_V:.6f} V"
                    if (
                        a.baseline_vbus_V is not None
                        and a.baseline_noise_mad_V is not None
                        and a.movement_threshold_V is not None
                    ) else
                    "  Baseline         : incomplete"
                ),
                (
                    f"  Movement start   : {a.movement_start_us} us "
                    f"(+{a.movement_latency_us/1000:.3f} ms)"
                    if a.movement_start_us is not None else
                    "  Movement start   : not detected"
                ),
                (
                    f"  Target crossing  : {a.target_crossing_us} us "
                    f"(+{a.target_crossing_latency_us/1000:.3f} ms)"
                    if a.target_crossing_us is not None else
                    "  Target crossing  : not observed"
                ),
                (
                    f"  ±{a.target_band_fraction*100:.2f}% first entry: "
                    f"{a.target_band_first_entry_us} us "
                    f"(+{a.target_band_first_entry_latency_us/1000:.3f} ms)"
                    if a.target_band_first_entry_us is not None else
                    f"  ±{a.target_band_fraction*100:.2f}% first entry: not observed"
                ),
                (
                    f"  Settled          : {a.settling_us} us "
                    f"(+{a.settling_latency_us/1000:.3f} ms; "
                    f"hold={a.settling_hold_us/1000:.1f} ms)"
                    if a.settling_us is not None else
                    "  Settled          : not observed"
                ),
                (
                    f"  Absolute slew    : {a.average_slew_V_per_s:.3f} V/s"
                    if a.average_slew_V_per_s is not None else
                    "  Absolute slew    : unavailable"
                ),
                (
                    f"  Observed plateau : {a.observed_plateau_V:.6f} V "
                    f"(MAD={a.observed_plateau_mad_V:.6f} V, "
                    f"n={a.observed_plateau_sample_count})"
                    if (
                        a.observed_plateau_V is not None
                        and a.observed_plateau_mad_V is not None
                    ) else
                    "  Observed plateau : unavailable"
                ),
                (
                    f"  Observed ±{a.observed_band_fraction*100:.2f}% entry: "
                    f"{a.observed_band_first_entry_us} us "
                    f"(+{a.observed_band_first_entry_latency_us/1000:.3f} ms)"
                    if a.observed_band_first_entry_us is not None else
                    f"  Observed ±{a.observed_band_fraction*100:.2f}% entry: unavailable"
                ),
                (
                    f"  Observed settled : {a.observed_settling_us} us "
                    f"(+{a.observed_settling_latency_us/1000:.3f} ms)"
                    if a.observed_settling_us is not None else
                    "  Observed settled : unavailable"
                ),
                (
                    f"  Observed slew    : {a.observed_average_slew_V_per_s:.3f} V/s"
                    if a.observed_average_slew_V_per_s is not None else
                    "  Observed slew    : unavailable"
                ),
                f"  Flags            : {flags}",
                "",
            ]
        )

    summary_path.write_text("\n".join(lines), encoding="utf-8")

    if human_csv_path is not None and human_text_path is not None:
        _write_human_transition_summary(
            analyses,
            csv_path=human_csv_path,
            text_path=human_text_path,
        )


def _run_sync_analysis(
    pd_csv: Path,
    scope_csv: Path,
    out_prefix: Path,
    *,
    baseline_window_ms: float,
    baseline_guard_ms: float,
    movement_min_threshold_v: float,
    movement_mad_multiplier: float,
    movement_sustain_samples: int,
    target_band_percent: float,
    settle_hold_ms: float,
    settle_max_gap_ms: float,
    plateau_lookback_ms: float,
    plateau_min_samples: int,
    observed_band_percent: float,
    observed_settle_hold_ms: float,
    plateau_stability_min_span_v: float,
    plateau_stability_percent: float,
    plateau_target_guard_percent: float,
) -> list[AVSTransitionAnalysis]:
    pd_samples = _load_pd_capture_csv(pd_csv)
    scope_samples = _load_scope_csv(scope_csv)

    settings = {
        "baseline_window_ms": baseline_window_ms,
        "baseline_guard_ms": baseline_guard_ms,
        "movement_min_threshold_V": movement_min_threshold_v,
        "movement_mad_multiplier": movement_mad_multiplier,
        "movement_sustain_samples": movement_sustain_samples,
        "target_band_percent": target_band_percent,
        "settle_hold_ms": settle_hold_ms,
        "settle_max_gap_ms": settle_max_gap_ms,
        "plateau_lookback_ms": plateau_lookback_ms,
        "plateau_min_samples": plateau_min_samples,
        "observed_band_percent": observed_band_percent,
        "observed_settle_hold_ms": observed_settle_hold_ms,
        "plateau_stability_min_span_V": plateau_stability_min_span_v,
        "plateau_stability_percent": plateau_stability_percent,
        "plateau_target_guard_percent": plateau_target_guard_percent,
    }

    analyses = analyze_avs_transitions(
        pd_samples,
        scope_samples,
        baseline_window_us=int(round(baseline_window_ms * 1000.0)),
        baseline_guard_us=int(round(baseline_guard_ms * 1000.0)),
        movement_min_threshold_V=movement_min_threshold_v,
        movement_mad_multiplier=movement_mad_multiplier,
        movement_sustain_samples=movement_sustain_samples,
        target_band_fraction=target_band_percent / 100.0,
        settle_hold_us=int(round(settle_hold_ms * 1000.0)),
        settle_max_sample_gap_us=int(round(settle_max_gap_ms * 1000.0)),
        plateau_lookback_us=int(round(plateau_lookback_ms * 1000.0)),
        plateau_min_samples=int(plateau_min_samples),
        observed_band_fraction=observed_band_percent / 100.0,
        observed_settle_hold_us=int(round(observed_settle_hold_ms * 1000.0)),
        plateau_stability_min_span_V=plateau_stability_min_span_v,
        plateau_stability_fraction=plateau_stability_percent / 100.0,
        plateau_target_guard_fraction=plateau_target_guard_percent / 100.0,
    )

    transition_csv = out_prefix.with_suffix(".transitions.csv")
    transition_summary = out_prefix.with_suffix(".transitions.txt")
    human_summary_csv = out_prefix.with_suffix(".transition_summary.csv")
    human_summary_text = out_prefix.with_suffix(".transition_summary.txt")
    _write_transition_outputs(
        analyses,
        csv_path=transition_csv,
        summary_path=transition_summary,
        settings=settings,
        human_csv_path=human_summary_csv,
        human_text_path=human_summary_text,
    )
    return analyses


def _print_transition_analysis(analyses: list[AVSTransitionAnalysis]) -> None:
    print()
    print("AVS TRANSITION ANALYSIS")
    if not analyses:
        print("No AVS EPR_REQUEST transitions found.")
        return

    for a in analyses:
        ps = (
            f"{a.ps_rdy_latency_us / 1000:.3f} ms"
            if a.ps_rdy_latency_us is not None else "-"
        )
        move = (
            f"{a.movement_latency_us / 1000:.3f} ms"
            if a.movement_latency_us is not None else "-"
        )
        settle = (
            f"{a.settling_latency_us / 1000:.3f} ms"
            if a.settling_latency_us is not None else "-"
        )
        ps_v = (
            f"{a.ps_rdy_scope_vbus_V:.3f} V"
            if a.ps_rdy_scope_vbus_V is not None else "-"
        )
        abs_slew = (
            f"{a.average_slew_V_per_s:.2f} V/s"
            if a.average_slew_V_per_s is not None else "-"
        )
        obs_settle = (
            f"{a.observed_settling_latency_us / 1000:.3f} ms"
            if a.observed_settling_latency_us is not None else "-"
        )
        obs_plateau = (
            f"{a.observed_plateau_V:.3f} V"
            if a.observed_plateau_V is not None else "-"
        )
        obs_slew = (
            f"{a.observed_average_slew_V_per_s:.2f} V/s"
            if a.observed_average_slew_V_per_s is not None else "-"
        )
        print(
            f"SNo={a.request_sno!s:>3} {a.direction:4s} "
            f"target={a.target_voltage_V:6.3f}V "
            f"move={move:>10} PS_RDY={ps:>10} "
            f"V@PS={ps_v:>9} abs_settle={settle:>10} "
            f"abs_slew={abs_slew:>10} | "
            f"plateau={obs_plateau:>9} "
            f"obs_settle={obs_settle:>10} "
            f"obs_slew={obs_slew:>10} "
            f"flags={';'.join(a.flags) if a.flags else '-'}"
        )


def _cmd_analyze_sync(args) -> int:
    pd_csv = Path(args.pd_csv)
    scope_csv = Path(args.scope_csv)
    prefix = Path(args.out_prefix)

    analyses = _run_sync_analysis(
        pd_csv,
        scope_csv,
        prefix,
        baseline_window_ms=args.baseline_window_ms,
        baseline_guard_ms=args.baseline_guard_ms,
        movement_min_threshold_v=args.movement_min_threshold_v,
        movement_mad_multiplier=args.movement_mad_multiplier,
        movement_sustain_samples=args.movement_sustain_samples,
        target_band_percent=args.target_band_percent,
        settle_hold_ms=args.settle_hold_ms,
        settle_max_gap_ms=args.settle_max_gap_ms,
        plateau_lookback_ms=args.plateau_lookback_ms,
        plateau_min_samples=args.plateau_min_samples,
        observed_band_percent=args.observed_band_percent,
        observed_settle_hold_ms=args.observed_settle_hold_ms,
        plateau_stability_min_span_v=args.plateau_stability_min_span_v,
        plateau_stability_percent=args.plateau_stability_percent,
        plateau_target_guard_percent=args.plateau_target_guard_percent,
    )
    _print_transition_analysis(analyses)
    print(f"Transitions CSV : {prefix.with_suffix('.transitions.csv').resolve()}")
    print(f"Transitions txt : {prefix.with_suffix('.transitions.txt').resolve()}")
    print(
        f"Human summary   : "
        f"{prefix.with_suffix('.transition_summary.csv').resolve()}"
    )
    print(
        f"Human text      : "
        f"{prefix.with_suffix('.transition_summary.txt').resolve()}"
    )
    return 0

def _cmd_scope(args) -> int:
    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    raw_path = Path(args.raw) if args.raw else None
    if raw_path is not None:
        raw_path.parent.mkdir(parents=True, exist_ok=True)

    csv_fp = csv_path.open("w", encoding="utf-8-sig", newline="")
    csv_export = ScopeCSV(csv_fp)
    raw_fp = raw_path.open("wb") if raw_path is not None else None

    first_ts = None
    last_ts = None
    padding_by_transfer: dict[int, tuple[bytes, ...]] = {}

    def on_scope_transfer(
        transfer_index: int,
        raw: bytes,
        decoded: ScopeTransferDecode,
    ) -> None:
        padding_by_transfer[transfer_index] = decoded.paddings
        if raw_fp is not None:
            # Preserve host bulk-transfer boundaries exactly.
            raw_fp.write(struct.pack("<I", len(raw)))
            raw_fp.write(raw)

        if decoded.residual and not args.quiet:
            print(
                f"[scope] transfer={transfer_index} "
                f"residual={len(decoded.residual)} B "
                f"{decoded.residual.hex(' ')}"
            )

    def on_scope_sample(transfer_index: int, sample: ScopeSample) -> None:
        nonlocal first_ts, last_ts

        if first_ts is None:
            first_ts = sample.timestamp_us
        last_ts = sample.timestamp_us

        paddings = padding_by_transfer.get(transfer_index, ())
        padding = (
            paddings[sample.packet_index]
            if sample.packet_index < len(paddings)
            else b""
        )
        csv_export.write_sample(transfer_index, sample, padding)

    def on_status(stats: CaptureStats) -> None:
        if not args.quiet:
            print(
                f"[scope status] "
                f"ep81_reads={stats.usb_reads} "
                f"ep83_xfers={stats.scope_data_transfers} "
                f"packets={stats.scope_packets} "
                f"samples={stats.scope_samples} "
                f"residual={stats.scope_residual_bytes}"
            )

    seconds = None if args.until_ctrl_c else args.seconds

    print("Opening CY4500-EPR...")
    if seconds is None:
        print("Scope mode: continuous (Ctrl+C to stop)")
    else:
        print(f"Scope duration: {seconds:.3f} s")
    print("EP81 is drained; EP83 real samples are written without synthetic points.")

    stats = None
    try:
        with CY4500EPR() as dev:
            stats = dev.capture(
                seconds,
                decode_ep81=False,
                strict_framing=False,
                scope_enabled=True,
                scope_sample_callback=on_scope_sample,
                scope_transfer_callback=on_scope_transfer,
                status_callback=on_status,
            )
    finally:
        csv_fp.close()
        if raw_fp is not None:
            raw_fp.close()

    if stats is None:
        raise CY4500Error("scope capture ended before statistics were available")

    print()
    print("SCOPE SUMMARY")
    print(f"Elapsed                : {stats.elapsed_seconds:.3f} s")
    print(f"Interrupted Ctrl+C     : {stats.interrupted}")
    print(f"EP81 reads drained     : {stats.usb_reads}")
    print(f"EP83 reads             : {stats.scope_reads}")
    print(f"EP83 short transfers   : {stats.scope_short_transfers}")
    print(f"EP83 data transfers    : {stats.scope_data_transfers}")
    print(f"EP83 packets           : {stats.scope_packets}")
    print(f"EP83 samples           : {stats.scope_samples}")
    print(f"EP83 residual bytes    : {stats.scope_residual_bytes}")
    print(
        f"Nonzero padding packets: "
        f"{stats.scope_nonzero_padding_packets}"
    )
    print(f"First EP83 timestamp   : {first_ts}")
    print(f"Last EP83 timestamp    : {last_ts}")
    print(f"CSV                    : {csv_path.resolve()}")
    if raw_path is not None:
        print(f"Raw transfers          : {raw_path.resolve()}")

    return 0

def _cmd_capture(args) -> int:
    prefix = Path(args.out_prefix)

    xfers_path = prefix.with_suffix(".xfers.bin")
    records_path = prefix.with_suffix(".records.bin")
    hex_path = prefix.with_suffix(".records.hex.txt")
    jsonl_path = prefix.with_suffix(".records.jsonl")
    csv_path = prefix.with_suffix(".csv")
    summary_path = prefix.with_suffix(".summary.txt")

    scope_csv_path = prefix.with_suffix(".scope.csv")
    scope_raw_path = prefix.with_suffix(".scope.xfers.bin")

    output_paths = [
        xfers_path,
        records_path,
        hex_path,
        jsonl_path,
        csv_path,
        summary_path,
    ]
    if args.scope:
        output_paths.append(scope_csv_path)
        if args.scope_raw:
            output_paths.append(scope_raw_path)

    for path in output_paths:
        path.parent.mkdir(parents=True, exist_ok=True)

    xfers_f = xfers_path.open("wb")
    records_f = records_path.open("wb")
    hex_f = hex_path.open("w", encoding="utf-8", newline="\n")
    jsonl_f = jsonl_path.open("w", encoding="utf-8", newline="\n")
    csv_f = csv_path.open("w", encoding="utf-8-sig", newline="")

    scope_csv_f = (
        scope_csv_path.open("w", encoding="utf-8-sig", newline="")
        if args.scope else None
    )
    scope_raw_f = (
        scope_raw_path.open("wb")
        if args.scope and args.scope_raw else None
    )

    csv_export = AnalyzerSchemaCSV(
        csv_f,
        bug_compatible_end_time=args.csv_bug_compatible,
    )
    scope_export = ScopeCSV(scope_csv_f) if scope_csv_f is not None else None
    semantic_tracker = CaptureSemanticTracker()

    ep81_first_start = None
    ep81_last_end = None
    ep83_first_raw = None
    ep83_first_us = None
    ep83_last_raw = None
    ep83_last_us = None

    scope_padding_by_transfer: dict[int, tuple[bytes, ...]] = {}

    def on_transfer(_index: int, raw: bytes) -> None:
        # Preserve data-bearing USB transfer boundaries:
        # uint32 LE transfer length + exact transfer including terminator.
        xfers_f.write(struct.pack("<I", len(raw)))
        xfers_f.write(raw)

    def on_record(record: CaptureRecord) -> None:
        nonlocal ep81_first_start, ep81_last_end

        detail_lines = semantic_tracker.process(record)

        if ep81_first_start is None:
            ep81_first_start = int(record.decoded["start_time"])
        ep81_last_end = int(record.decoded["end_time"])

        records_f.write(record.raw)
        hex_f.write(
            f"{record.index - 1:04d}  {record.raw.hex(' ')}\n"
        )

        row = {
            "index": record.index,
            "raw": record.raw.hex(" "),
            "decoded": _jsonable(record.decoded),
        }
        jsonl_f.write(json.dumps(row, ensure_ascii=False) + "\n")
        csv_export.write_record(record)

        if not args.quiet:
            print(_compact_record(record))
            for detail in detail_lines:
                print("    -> " + detail)

    def on_scope_transfer(
        transfer_index: int,
        raw: bytes,
        decoded: ScopeTransferDecode,
    ) -> None:
        scope_padding_by_transfer[transfer_index] = decoded.paddings
        if scope_raw_f is not None:
            scope_raw_f.write(struct.pack("<I", len(raw)))
            scope_raw_f.write(raw)

    def on_scope_sample(transfer_index: int, sample: ScopeSample) -> None:
        nonlocal ep83_first_raw, ep83_first_us, ep83_last_raw, ep83_last_us

        if ep83_first_us is None:
            ep83_first_raw = sample.timestamp_raw
            ep83_first_us = sample.timestamp_us

        ep83_last_raw = sample.timestamp_raw
        ep83_last_us = sample.timestamp_us

        if scope_export is not None:
            paddings = scope_padding_by_transfer.get(transfer_index, ())
            padding = (
                paddings[sample.packet_index]
                if sample.packet_index < len(paddings)
                else b""
            )
            scope_export.write_sample(transfer_index, sample, padding)

    def on_status(stats: CaptureStats) -> None:
        if not args.quiet:
            text = (
                f"[status] reads={stats.usb_reads} "
                f"idle={stats.idle_transfers} "
                f"data_xfers={stats.data_transfers} "
                f"records={stats.records} "
                f"framing_errors={stats.framing_errors}"
            )
            if args.scope:
                text += (
                    f" | scope_xfers={stats.scope_data_transfers} "
                    f"scope_packets={stats.scope_packets} "
                    f"scope_samples={stats.scope_samples}"
                )
            print(text)

    seconds = None if args.until_ctrl_c else args.seconds

    print("Opening CY4500-EPR...")
    if seconds is None:
        print("Capture mode: continuous (Ctrl+C to stop)")
    else:
        print(f"Capture duration: {seconds:.3f} s")
    print("START should put the analyzer into its active/green LED state.")
    if args.scope:
        print(
            "Combined EP81 + EP83 mode enabled. "
            "No timestamp offset/synchronization is applied."
        )

    stats = None

    try:
        with CY4500EPR() as dev:
            stats = dev.capture(
                seconds,
                record_callback=on_record,
                transfer_callback=on_transfer,
                status_callback=on_status,
                strict_framing=not args.allow_framing_errors,
                scope_enabled=args.scope,
                scope_sample_callback=on_scope_sample if args.scope else None,
                scope_transfer_callback=on_scope_transfer if args.scope else None,
            )
    finally:
        xfers_f.close()
        records_f.close()
        hex_f.close()
        jsonl_f.close()
        csv_f.close()
        if scope_csv_f is not None:
            scope_csv_f.close()
        if scope_raw_f is not None:
            scope_raw_f.close()

    if stats is None:
        raise CY4500Error("capture ended before statistics were available")

    semantic_summary = semantic_tracker.summary_lines()

    clock_lines = [
        "",
        "CAPTURE CLOCK METADATA",
        f"EP81 first start timestamp raw/us : {ep81_first_start}",
        f"EP81 last end timestamp raw/us    : {ep81_last_end}",
    ]

    if args.scope:
        clock_lines.extend(
            [
                f"EP83 first timestamp raw          : {ep83_first_raw}",
                f"EP83 first timestamp unwrapped/us : {ep83_first_us}",
                f"EP83 last timestamp raw           : {ep83_last_raw}",
                f"EP83 last timestamp unwrapped/us  : {ep83_last_us}",
                "EP81/EP83 common clock assumed     : NO",
                "Host timestamp offset applied       : NO",
            ]
        )

    summary_path.write_text(
        "\n".join(semantic_summary + clock_lines) + "\n",
        encoding="utf-8",
    )

    print()
    print("CAPTURE SUMMARY")
    print(f"Elapsed             : {stats.elapsed_seconds:.3f} s")
    print(f"Interrupted Ctrl+C  : {stats.interrupted}")
    print(f"USB reads           : {stats.usb_reads}")
    print(f"Idle transfers      : {stats.idle_transfers}")
    print(f"Data transfers      : {stats.data_transfers}")
    print(f"Records             : {stats.records}")
    print(f"Framing errors      : {stats.framing_errors}")
    print(f"Data-transfer bytes : {stats.data_transfer_bytes}")

    if args.scope:
        print(f"Scope reads         : {stats.scope_reads}")
        print(f"Scope short xfers   : {stats.scope_short_transfers}")
        print(f"Scope data xfers    : {stats.scope_data_transfers}")
        print(f"Scope packets       : {stats.scope_packets}")
        print(f"Scope samples       : {stats.scope_samples}")
        print(f"Scope residual bytes: {stats.scope_residual_bytes}")
        print(
            f"Scope nonzero pad   : "
            f"{stats.scope_nonzero_padding_packets}"
        )

    print()
    print(f"Transfers : {xfers_path.resolve()}")
    print(f"Records   : {records_path.resolve()}")
    print(f"Hex text  : {hex_path.resolve()}")
    print(f"JSONL     : {jsonl_path.resolve()}")
    print(f"CSV       : {csv_path.resolve()}")
    if args.scope:
        print(f"Scope CSV : {scope_csv_path.resolve()}")
        if args.scope_raw:
            print(f"Scope raw : {scope_raw_path.resolve()}")
    print(f"Summary   : {summary_path.resolve()}")

    if args.csv_bug_compatible:
        print("CSV mode  : Analyzer Utility 4.2.0 End Time bug reproduced")
    else:
        print("CSV mode  : corrected End Time; time fields are microseconds")

    if args.scope:
        print(
            "Clock mode: raw EP81 + EP83 device timestamps preserved; "
            "common clock NOT yet assumed"
        )

    print()
    for line in semantic_summary:
        print(line)

    if args.scope and not args.no_analyze_transitions:
        analyses = _run_sync_analysis(
            csv_path,
            scope_csv_path,
            prefix,
            baseline_window_ms=args.baseline_window_ms,
            baseline_guard_ms=args.baseline_guard_ms,
            movement_min_threshold_v=args.movement_min_threshold_v,
            movement_mad_multiplier=args.movement_mad_multiplier,
            movement_sustain_samples=args.movement_sustain_samples,
            target_band_percent=args.target_band_percent,
            settle_hold_ms=args.settle_hold_ms,
            settle_max_gap_ms=args.settle_max_gap_ms,
            plateau_lookback_ms=args.plateau_lookback_ms,
            plateau_min_samples=args.plateau_min_samples,
            observed_band_percent=args.observed_band_percent,
            observed_settle_hold_ms=args.observed_settle_hold_ms,
            plateau_stability_min_span_v=args.plateau_stability_min_span_v,
            plateau_stability_percent=args.plateau_stability_percent,
            plateau_target_guard_percent=args.plateau_target_guard_percent,
        )
        _print_transition_analysis(analyses)
        print(
            f"Transitions: "
            f"{prefix.with_suffix('.transitions.csv').resolve()}"
        )
        print(
            f"Transition summary: "
            f"{prefix.with_suffix('.transitions.txt').resolve()}"
        )
        print(
            f"Human summary: "
            f"{prefix.with_suffix('.transition_summary.csv').resolve()}"
        )
        print(
            f"Human text: "
            f"{prefix.with_suffix('.transition_summary.txt').resolve()}"
        )

    return 0



def _add_transition_analysis_options(p) -> None:
    p.add_argument(
        "--baseline-window-ms",
        type=float,
        default=TRANSITION_BASELINE_WINDOW_US / 1000.0,
        help="pre-request baseline window length (default: 100 ms)",
    )
    p.add_argument(
        "--baseline-guard-ms",
        type=float,
        default=TRANSITION_BASELINE_GUARD_US / 1000.0,
        help="exclude this time immediately before request from baseline (default: 5 ms)",
    )
    p.add_argument(
        "--movement-min-threshold-v",
        type=float,
        default=TRANSITION_MOVEMENT_MIN_THRESHOLD_V,
        help="minimum sustained VBUS deviation used for movement detection (default: 0.05 V)",
    )
    p.add_argument(
        "--movement-mad-multiplier",
        type=float,
        default=TRANSITION_MOVEMENT_MAD_MULTIPLIER,
        help="baseline MAD multiplier for movement threshold (default: 6)",
    )
    p.add_argument(
        "--movement-sustain-samples",
        type=int,
        default=TRANSITION_MOVEMENT_SUSTAIN_SAMPLES,
        help="consecutive EP83 samples required for movement detection (default: 5)",
    )
    p.add_argument(
        "--target-band-percent",
        type=float,
        default=TRANSITION_TARGET_BAND_FRACTION * 100.0,
        help="target-voltage band half-width in percent (default: 1.0)",
    )
    p.add_argument(
        "--settle-hold-ms",
        type=float,
        default=TRANSITION_SETTLE_HOLD_US / 1000.0,
        help="continuous in-band duration required to declare settled (default: 20 ms)",
    )
    p.add_argument(
        "--settle-max-gap-ms",
        type=float,
        default=TRANSITION_SETTLE_MAX_SAMPLE_GAP_US / 1000.0,
        help="maximum allowed EP83 sample gap during settle hold (default: 5 ms)",
    )
    p.add_argument(
        "--plateau-lookback-ms",
        type=float,
        default=TRANSITION_PLATEAU_LOOKBACK_US / 1000.0,
        help="terminal EP83 window used to estimate observed final plateau (default: 150 ms)",
    )
    p.add_argument(
        "--plateau-min-samples",
        type=int,
        default=TRANSITION_PLATEAU_MIN_SAMPLES,
        help="minimum terminal EP83 samples required for observed plateau (default: 50)",
    )
    p.add_argument(
        "--observed-band-percent",
        type=float,
        default=TRANSITION_RELATIVE_BAND_FRACTION * 100.0,
        help="relative settling band around observed plateau (default: 0.5%%)",
    )
    p.add_argument(
        "--observed-settle-hold-ms",
        type=float,
        default=TRANSITION_RELATIVE_SETTLE_HOLD_US / 1000.0,
        help="continuous observed-band hold time (default: 20 ms)",
    )
    p.add_argument(
        "--plateau-stability-min-span-v",
        type=float,
        default=TRANSITION_PLATEAU_STABILITY_MIN_SPAN_V,
        help="minimum allowed VBUS-span limit for plateau window (default: 0.10 V)",
    )
    p.add_argument(
        "--plateau-stability-percent",
        type=float,
        default=TRANSITION_PLATEAU_STABILITY_FRACTION * 100.0,
        help="relative allowed VBUS span for plateau window (default: 0.25%%)",
    )
    p.add_argument(
        "--plateau-target-guard-percent",
        type=float,
        default=TRANSITION_PLATEAU_TARGET_GUARD_FRACTION * 100.0,
        help="plausibility guard around requested target for observed plateau selection (default: 10%%)",
    )

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Direct CY4500-EPR controller/capture utility"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "usb-info",
        help="read-only USB descriptor enumeration",
    )
    p.set_defaults(func=_cmd_usb_info)

    p = sub.add_parser(
        "version",
        help="read analyzer firmware version",
    )
    p.set_defaults(func=_cmd_version)

    p = sub.add_parser(
        "live-status",
        aliases=["volt-amp"],
        help="read-only live VBUS / IBUS / CC1 / CC2 using Utility 4.2.0 formulas",
    )
    p.add_argument(
        "--count",
        type=int,
        default=None,
        help="number of samples; omit for continuous until Ctrl+C",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=0.1,
        help="seconds between samples (default: 0.1)",
    )
    p.add_argument(
        "--median",
        type=int,
        default=5,
        help="rolling median window for displayed current (default: 5)",
    )
    p.add_argument(
        "--csv",
        default=None,
        help="optional CSV output path",
    )
    p.set_defaults(func=_cmd_live_status)


    p = sub.add_parser(
        "analyze-sync",
        help="offline EP81 + EP83 AVS transition analysis",
    )
    p.add_argument("--pd-csv", required=True, help="EP81 capture CSV")
    p.add_argument("--scope-csv", required=True, help="EP83 scope CSV")
    p.add_argument(
        "--out-prefix",
        default="cy4500_sync_analysis",
        help="output prefix for .transitions.csv/.transitions.txt",
    )
    _add_transition_analysis_options(p)
    p.set_defaults(func=_cmd_analyze_sync)

    p = sub.add_parser(
        "scope",
        help="capture EP83 VBUS / IBUS / CC1 / CC2 telemetry while draining EP81",
    )
    duration = p.add_mutually_exclusive_group()
    duration.add_argument(
        "--seconds",
        type=float,
        default=5.0,
        help="scope duration in seconds (default: 5)",
    )
    duration.add_argument(
        "--until-ctrl-c",
        action="store_true",
        help="capture continuously until Ctrl+C",
    )
    p.add_argument(
        "--csv",
        default="cy4500_scope.csv",
        help="scope CSV output path (default: cy4500_scope.csv)",
    )
    p.add_argument(
        "--raw",
        default=None,
        help=(
            "optional length-prefixed EP83 raw transfer output path; "
            "omit to disable"
        ),
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="suppress periodic scope status lines",
    )
    p.set_defaults(func=_cmd_scope)

    p = sub.add_parser(
        "capture",
        help="capture EP81 USB-PD records",
    )

    duration = p.add_mutually_exclusive_group()
    duration.add_argument(
        "--seconds",
        type=float,
        default=8.0,
        help="capture duration in seconds (default: 8)",
    )
    duration.add_argument(
        "--until-ctrl-c",
        action="store_true",
        help="capture continuously until Ctrl+C",
    )

    p.add_argument(
        "--out-prefix",
        default="cy4500_capture",
        help="output path prefix (default: cy4500_capture)",
    )
    p.add_argument(
        "--scope",
        action="store_true",
        help=(
            "also capture EP83 scope telemetry in the same START/STOP session; "
            "device timestamps are preserved without assuming a common clock"
        ),
    )
    p.add_argument(
        "--scope-raw",
        action="store_true",
        help="with --scope, also save length-prefixed raw EP83 transfers",
    )
    p.add_argument(
        "--no-analyze-transitions",
        action="store_true",
        help="disable automatic EP81+EP83 AVS transition analysis",
    )
    _add_transition_analysis_options(p)
    p.add_argument(
        "--quiet",
        action="store_true",
        help="do not print each decoded record/status line",
    )
    p.add_argument(
        "--allow-framing-errors",
        action="store_true",
        help="count malformed EP81 data transfers instead of aborting",
    )
    p.add_argument(
        "--csv-bug-compatible",
        action="store_true",
        help=(
            "reproduce Analyzer Utility 4.2.0 CSV bug where "
            "End Time duplicates Start Time"
        ),
    )
    p.set_defaults(func=_cmd_capture)

    p = sub.add_parser(
        "trigger",
        help="configure the CY4500 hardware SOM/EOM/MTR trigger",
        description=(
            "Configure the exact Analyzer Utility trigger packet. "
            "Enabled conditions are combined with AND. "
            "Start SNo drives SOM; End SNo drives EOM; "
            "SOP/message/ObjCount/MsgID participate in MTR matching. "
            "Physical CY4500-EPR testing confirms trigger evaluation occurs "
            "only while CMD_START active/green measurement mode is running; "
            "use --arm to SET + START + drain + STOP in one command."
        ),
    )
    p.add_argument(
        "--start-sno",
        type=lambda s: int(s, 0),
        default=None,
        help="enable Start SNo criterion (uint32; SOM output)",
    )
    p.add_argument(
        "--end-sno",
        type=lambda s: int(s, 0),
        default=None,
        help="enable End SNo criterion (uint32; EOM output)",
    )
    p.add_argument(
        "--sop",
        choices=tuple(TRIGGER_SOP_TYPE.keys()),
        default=None,
        help="enable SOP criterion",
    )
    p.add_argument(
        "--msg-class",
        choices=("CONTROL", "DATA", "EXTENDED"),
        default=None,
        help="PD message class; required when --msg-type is used",
    )
    p.add_argument(
        "--msg-type",
        default=None,
        help=(
            "PD message name or numeric type index; "
            "requires --msg-class"
        ),
    )
    p.add_argument(
        "--obj-count",
        type=int,
        default=None,
        help="enable object-count criterion (0..7)",
    )
    p.add_argument(
        "--msg-id",
        type=int,
        default=None,
        help="enable Message ID criterion (0..7)",
    )
    p.add_argument(
        "--clear",
        action="store_true",
        help="send physically verified all-enable-bits-zero trigger packet",
    )
    p.add_argument(
        "--list-types",
        choices=("CONTROL", "DATA", "EXTENDED", "ALL"),
        default=None,
        help="list trigger message names/indexes without opening the device",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="build and display the exact packet without writing USB",
    )
    p.add_argument(
        "--arm",
        action="store_true",
        help=(
            "after setting the trigger, send CMD_START and continuously drain "
            "EP81/EP83 until Ctrl+C, then send CMD_STOP"
        ),
    )
    p.add_argument(
        "--arm-seconds",
        type=float,
        default=None,
        help=(
            "arm for a fixed number of seconds; implies --arm "
            "(default with --arm: until Ctrl+C)"
        ),
    )
    p.add_argument(
        "--clear-on-exit",
        action="store_true",
        help="after CMD_STOP, also clear all trigger criteria",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="with --arm, suppress periodic drain-status lines",
    )
    p.set_defaults(func=_cmd_trigger)

    p = sub.add_parser(
        "trigger-arm",
        help="START/arm previously configured trigger criteria without sending CMD_TRIGGER",
        description=(
            "Send CMD_START without modifying trigger configuration, drain "
            "EP81/EP83 while active, then send CMD_STOP. This is specifically "
            "for testing whether trigger criteria persist across CMD_STOP."
        ),
    )
    p.add_argument(
        "--seconds",
        type=float,
        default=None,
        help="arm for a fixed number of seconds; omit for Ctrl+C",
    )
    p.add_argument(
        "--clear-on-exit",
        action="store_true",
        help="after CMD_STOP, clear all trigger criteria",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="suppress periodic drain-status lines",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="display START/STOP sequence without USB access",
    )
    p.set_defaults(func=_cmd_trigger_arm)

    p = sub.add_parser(
        "trigger-epr-request",
        help="set DATA/EPR_REQUEST hardware MTR trigger",
    )
    p.set_defaults(func=_cmd_trigger_epr_request)

    p = sub.add_parser(
        "trigger-clear",
        help="disable all hardware trigger conditions",
    )
    p.set_defaults(func=_cmd_trigger_clear)

    p = sub.add_parser(
        "termination",
        help="configure CY4500 CC1/CC2 terminations",
        description=(
            "Set the analyzer's physical CC terminations. "
            "Both CC1 and CC2 must be specified explicitly because the "
            "device exposes no readback of the current setting."
        ),
    )
    choices = ("RP", "RA", "RD", "NONE")
    p.add_argument("--cc1", default=None, choices=choices)
    p.add_argument("--cc2", default=None, choices=choices)
    p.add_argument(
        "--clear",
        action="store_true",
        help="set CC1=NONE and CC2=NONE",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="build and display the exact packet without writing USB",
    )
    p.set_defaults(func=_cmd_termination)

    p = sub.add_parser(
        "termination-clear",
        help="set CC1=NONE and CC2=NONE",
    )
    p.set_defaults(func=_cmd_termination_clear)

    return parser


def main(argv=None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        return int(args.func(args))
    except (CY4500Error, usb1.USBError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
