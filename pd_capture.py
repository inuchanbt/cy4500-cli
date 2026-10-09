"""Read CY4500 Utility/legacy PD CSV without importing USB device control."""
import csv
import math
from pathlib import Path

from ezpd_protocol import SyncPDSample


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def load_pd_capture_csv(path: Path) -> list[SyncPDSample]:
    packets = []
    with path.open(encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        if not {"Message", "Start Time", "Data", "Ok", "SOP"} <= set(reader.fieldnames or []):
            raise ValueError("Expected a CY4500 PD CSV with Message, Start Time, Data, Ok and SOP columns")
        for index, row in enumerate(reader):
            start = _number(row.get("Start Time"))
            if start is None:
                continue
            end = _number(row.get("End Time"))
            status = (row.get("Ok") or "").strip().upper()
            utility = status not in {"0", "1"}
            text = (row.get("Data") or "").strip()
            try:
                if utility and text:
                    words = text.split()
                    header = int(words[0], 16)
                    if header & 0x8000:
                        data = int(words[1], 16).to_bytes(2, "little")
                        data += bytes(int(word, 16) for word in words[2:])
                    else:
                        data = b"".join(int(word, 16).to_bytes(4, "little") for word in words[1:])
                else:
                    data = bytes.fromhex(text) if text else b""
            except (ValueError, IndexError, OverflowError):
                data = b""
            voltage = _number(row.get("Vbus(V)"))
            if utility and voltage is not None:
                voltage /= 1000
            sno = _number(row.get("Sno"))
            message = (row.get("Message") or "").strip().upper()
            if status == "DETACH":
                message = "DETACH"
            packets.append(SyncPDSample(
                row_index=index, sno=None if sno is None else int(sno),
                message=message, start_us=int(start), end_us=int(start if end is None else end),
                vbus_V=voltage, data=data, ok=status in {"OK", "1", "DETACH"},
                sop=(row.get("SOP") or "").strip().upper(),
            ))
    return packets
