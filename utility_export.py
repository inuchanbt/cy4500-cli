"""Analyzer Utility 4.2 ccgx3 export (ZIP containing Java serialization).

No Java runtime or proprietary libraries are required. The writer deliberately
supports only the value types in the captured Utility schema; it never loads
Java objects or executes content from input files.
"""
import csv
import shutil
import struct
import tempfile
import uuid
import zipfile
from datetime import datetime
from ezpd_protocol import decode_capture_record, vbus_epr_raw_scale

COLUMNS = 'Sno,Ok,SOP,Message,Msg Id,Data Role,Power Role,Obj Count,Rev,Duration,Delta,Vbus(V),Data,Start Time,End Time'.split(',')
EVENTS = ('NONE', 'VBUS_DN', 'VBUS_UP', 'CC_DEF', 'CC_1_5A', 'CC_3A', 'DETACH')

class UtilityRows:
    def __init__(self):
        self.count = 0
        self.previous_start = None
        self.previous_end = None
        self.epoch = 0

    def row(self, raw):
        d = decode_capture_record(raw)
        f = d['fields']
        self.count += 1
        start = d['start_time']
        if self.previous_start is not None and start < self.previous_start:
            self.epoch += 1 << 32
        self.previous_start = start
        start += self.epoch
        duration = (d['end_time'] - d['start_time']) & 0xffffffff
        end = start + duration
        delta = '' if self.previous_end is None else str(start - self.previous_end)
        self.previous_end = end
        ok = 'OK' if f['OK'] else 'ER' + ('_CRC' if f['CRC_ERROR'] else '') + ('_EOP' if f['EOP_ERROR'] else '')
        if f['PKT_TYPE'] == 1:
            ok = EVENTS[d['sno']] if d['sno'] < len(EVENTS) else f"UNKNOWN_{d['sno']}"
        data = f"0x{d['pd_header']:X}"
        if f['EXTENDED']:
            data += f" 0x{int.from_bytes(raw[20:22], 'little'):X}"
            data += ''.join(f' 0x{b:02X}' for b in raw[22:20 + 4*f['OBJ_COUNT']])
        else:
            data += ''.join(f" 0x{int.from_bytes(raw[i:i+4], 'little'):X}" for i in range(20, 20+4*f['OBJ_COUNT'], 4))
        # VOLT records still carry a zero PD header in the Utility table.
        msg = d['message_name'] or 'C_RSVD0'
        return list(map(str, (self.count, ok, d['sop_name'], msg, f['MSG_ID'],
            'DFP' if f['DATA_ROLE'] else 'UFP', 'SRC' if f['POWER_ROLE'] else 'SNK',
            f['OBJ_COUNT'], d['spec_rev_name'], duration, delta,
            vbus_epr_raw_scale(d['vbus_raw']), data, start, end)))


def _utf(s):
    b = s.encode('ascii')  # All schema names and exported values are ASCII.
    return struct.pack('>H', len(b)) + b

def _string(s):
    return b'\x70' if s is None else b'\x74' + _utf(s)

def _desc(name, uid, fields, flags=2):
    out = b'\x72' + _utf(name) + struct.pack('>QBH', uid, flags, len(fields))
    for kind, field, signature in fields:
        out += kind.encode() + _utf(field)
        if kind in 'L[':
            out += _string(signature)
    return out + b'\x78\x70'

LIST = _desc('java.util.ArrayList', 0x7881d21d99c7619d, [('I', 'size', None)], 3)
BYTE_ARRAY = _desc('[B', 0xacf317f8060854e0, [])
UUID = _desc('java.util.UUID', 0xbc9903f7986d852f, [('J', 'leastSigBits', None), ('J', 'mostSigBits', None)])
STR = 'Ljava/lang/String;'
ARR = 'Ljava/util/ArrayList;'
PACKET_FIELDS = [('Z', 'isMarked', None), ('I', 'markerCount', None)] + [
    ('L', n, sig) if n != 'pktData' else ('[', n, sig) for n, sig in [
    ('bg', 'Lcom/cypress/ezpdanalyzer/ui/util/BGColor;'), ('count', STR), ('dRole', STR),
    ('data', STR), ('delta', STR), ('duration', STR), ('eTime', STR), ('id', STR),
    ('msg', STR), ('ok', STR), ('pRole', STR), ('packetDetails', ARR), ('payloads', ARR),
    ('pktData', '[B'), ('rev', STR), ('sTime', STR), ('sno', STR), ('sop', STR),
    ('subPackets', ARR), ('uniqueId', 'Ljava/util/UUID;'), ('vbus', STR)]]
PACKET = _desc('com.cypress.ezpdanalyzer.ui.model.USBPacketData', 1, PACKET_FIELDS)
GRAPH = _desc('com.cypress.ezpdanalyzer.ui.model.GraphData', 1,
    [('S', 'amp', None), ('S', 'cc1', None), ('S', 'cc2', None), ('J', 'timeStamp', None), ('S', 'volt', None)])

def _list_start(count):
    return b'\x73' + LIST + struct.pack('>i', count) + b'\x77\x04' + struct.pack('>i', count)

EMPTY_LIST = _list_start(0) + b'\x78'

def _packet(raw, row):
    values = dict(zip(('sno','ok','sop','msg','id','dRole','pRole','count','rev','duration','delta','vbus','data','sTime','eTime'), row))
    # Native objects store hardware Sno; CSV exports sequential row numbers.
    values['sno'] = str(int.from_bytes(raw[:4], 'little'))
    raw = raw[:20 + 4 * ((int.from_bytes(raw[16:20], 'little') >> 12) & 7)]
    out = b'\x73' + PACKET + b'\x00' * 5
    for _, name, _ in PACKET_FIELDS[2:]:
        if name == 'bg': out += b'\x70'
        elif name in ('packetDetails', 'payloads', 'subPackets'): out += EMPTY_LIST
        elif name == 'pktData': out += b'\x75' + BYTE_ARRAY + struct.pack('>i', len(raw)) + raw
        elif name == 'uniqueId':
            u = uuid.uuid4().int
            out += b'\x73' + UUID + struct.pack('>QQ', u & ((1<<64)-1), u >> 64)
        else: out += _string(values[name] or None)
    return out

class UtilityExport:
    """Stream into disk-backed temporary files, then assemble the ZIP on close."""
    def __init__(self, csv_path=None, ccgx3_path=None):
        self.rows = UtilityRows()
        self.csv_fp = None
        self.packets = None
        self.scope = None
        self.ccgx3_path = ccgx3_path
        self.scope_count = 0
        try:
            if csv_path is not None:
                self.csv_fp = open(csv_path, 'w', encoding='utf-8', newline='')
                self.csv = csv.writer(self.csv_fp, lineterminator='\n')
                self.csv.writerow(COLUMNS)
            if ccgx3_path is not None:
                self.packets = tempfile.TemporaryFile()
                self.scope = tempfile.TemporaryFile()
        except BaseException:
            self._cleanup()
            raise

    def write_record(self, record):
        if self.csv_fp is None and self.packets is None:
            return
        row = self.rows.row(record.raw)
        if self.csv_fp is not None: self.csv.writerow(row)
        if self.packets is not None: self.packets.write(_packet(record.raw, row))

    def write_scope(self, sample):
        if self.scope is not None:
            self.scope.write(b'\x73' + GRAPH + struct.pack('>HHHQH', sample.ibus_raw,
                sample.cc1_raw, sample.cc2_raw, sample.timestamp_us, sample.vbus_raw))
            self.scope_count += 1

    def _cleanup(self):
        for fp in (self.csv_fp, self.packets, self.scope):
            if fp is not None: fp.close()

    def close(self):
        try:
            if self.ccgx3_path is not None:
                folder = datetime.now().strftime('%Y_%m_%d_%H_%M_%S') + '/'
                with zipfile.ZipFile(self.ccgx3_path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                    z.writestr(folder, b'')
                    for name, fp, count in [('ezpd_last.part', self.packets, self.rows.count), ('ezpd_scope.scope', self.scope, self.scope_count)]:
                        with z.open(folder + name, 'w', force_zip64=True) as dest:
                            dest.write(b'\xac\xed\x00\x05' + _list_start(count))
                            fp.seek(0)
                            shutil.copyfileobj(fp, dest)
                            dest.write(b'\x78')
        finally:
            self._cleanup()
