import csv
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
import zipfile
from utility_export import UtilityExport, UtilityRows

def synthetic_records():
    """Artificial records only; no captured device data is distributed."""
    records = []
    for sno, word, payload in [(2, 1 << 28, b''),
                               (0, (1 << 31) | 0x1002, bytes([1, 2, 3, 4])),
                               (1, (1 << 31) | 0x9001, bytes([2, 0, 5, 6]))]:
        raw = bytearray(64)
        start = 100 + len(records) * 100
        struct.pack_into('<IIIII', raw, 0, sno, 400, start, start + 20, word)
        raw[20:20+len(payload)] = payload
        records.append(bytes(raw))
    return b''.join(records)

class UtilityExportTests(unittest.TestCase):
    def test_new_and_legacy_analysis_inputs(self):
        from cy4500_cli import AnalyzerSchemaCSV, CaptureRecord, _load_pd_capture_csv
        from ezpd_protocol import decode_capture_record, capture_record_logical_message_bytes
        raw = synthetic_records()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            new = UtilityExport(path/'new.csv')
            with (path/'old.csv').open('w', newline='') as fp:
                old = AnalyzerSchemaCSV(fp)
                for i in range(0, len(raw), 64):
                    packet = raw[i:i+64]
                    record = CaptureRecord(i//64+1, packet, decode_capture_record(packet))
                    old.write_record(record)
                    new.write_record(record)
            new.close()
            before = _load_pd_capture_csv(path/'old.csv')
            after = _load_pd_capture_csv(path/'new.csv')
            self.assertEqual([p.sno for p in after], [1, 2, 3])
            for i, (a, b) in enumerate(zip(before, after)):
                self.assertEqual(a.data, b.data)
                self.assertEqual(b.data, capture_record_logical_message_bytes(raw[i*64:(i+1)*64]))
                self.assertEqual((a.start_us, a.end_us), (b.start_us, b.end_us))
                self.assertLess(abs(a.vbus_V-b.vbus_V), 0.001)

    def test_offline_standard_csv_name(self):
        from cy4500_cli import main
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'input.bin'
            source.write_bytes(synthetic_records())
            prefix = Path(folder)/'converted'
            self.assertEqual(main(['export-gui', '--records', str(source),
                                   '--out-prefix', str(prefix)]), 0)
            self.assertTrue(prefix.with_suffix('.csv').exists())
            self.assertFalse(prefix.with_suffix('.utility.csv').exists())
            self.assertTrue(prefix.with_suffix('.ccgx3').exists())

    def test_rollover_and_errors(self):
        raw = bytearray(64)
        struct.pack_into('<III', raw, 8, 0xfffffff0, 5, (1<<30)|(1<<29))
        rows = UtilityRows()
        first = rows.row(raw)
        self.assertEqual(first[1], 'ER_CRC_EOP')
        self.assertEqual(first[9], '21')
        struct.pack_into('<II', raw, 8, 10, 20)
        second = rows.row(raw)
        self.assertEqual(second[10], '5')
        self.assertEqual(second[13], str((1<<32)+10))

    def test_empty_and_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'empty.ccgx3'
            export = UtilityExport(ccgx3_path=path)
            export.write_scope(SimpleNamespace(ibus_raw=65535, cc1_raw=123,
                cc2_raw=456, timestamp_us=(1<<32)+10, vbus_raw=4095))
            export.close()
            with zipfile.ZipFile(path) as z:
                graph = z.read(next(n for n in z.namelist() if n.endswith('.scope')))
                self.assertIn(struct.pack('>HHHQH',65535,123,456,(1<<32)+10,4095), graph)

if __name__ == '__main__': unittest.main()
