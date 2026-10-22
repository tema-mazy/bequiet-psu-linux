#!/usr/bin/env python3
"""Read-only telemetry reader for be quiet! DARK POWER PRO 14 IO power supplies.

Protocol reverse-engineered from be quiet!'s IO Center Web app (archived
iocenter.bequiet.com bundle, 2026-06). Talks to the PSU over /dev/hidraw using
only the Python standard library.

Safety: only commands in READ_ONLY_COMMANDS can be sent. Nothing that changes
fan mode, rail mode, sensor config, serial number or firmware is implemented.
"""

import argparse
import csv
import glob
import json
import os
import random
import select
import signal
import struct
import sys
import time

VENDOR_ID = 0x373F
PSU_MODELS = {
    0x0023: "Dark Power Pro 14 IO 1300W",
    0x0024: "Dark Power Pro 14 IO 1600W",
}

PACKET_SIZE = 64
CLIENT_TYPE_WEB = 2  # what IO Center Web announces in OpenSession

# Packet layout (64-byte reports, report ID 0)
OFF_LEN, OFF_SEQ, OFF_SESSION, OFF_STATUS, OFF_REQ, OFF_FEATURE, OFF_CMD, OFF_DATA = range(8)

# Feature IDs
F_ROOT, F_DEVICE_INFO, F_SENSORS, F_CONTROLS = 1, 3, 48, 49
FEATURE_NAMES = {1: "ROOT", 2: "DFU", 3: "DEVICE_INFO", 4: "STORAGE", 48: "SENSORS",
                 49: "CONTROLS", 52: "KEY_VALUE_STORAGE"}

# Command IDs
ROOT_OPEN, ROOT_CLOSE, ROOT_KEEPALIVE, ROOT_FEATURES = 1, 2, 3, 4
DI_INFO, DI_SERIAL = 1, 2
S_INFO, S_SENSOR_INFO, S_VALUE, S_CONFIG = 1, 2, 3, 4
C_INFO, C_CONTROL_INFO, C_VALUE, C_CONFIG = 1, 2, 3, 5

READ_ONLY_COMMANDS = {
    (F_ROOT, ROOT_OPEN), (F_ROOT, ROOT_CLOSE), (F_ROOT, ROOT_KEEPALIVE), (F_ROOT, ROOT_FEATURES),
    (F_DEVICE_INFO, DI_INFO), (F_DEVICE_INFO, DI_SERIAL),
    (F_SENSORS, S_INFO), (F_SENSORS, S_SENSOR_INFO), (F_SENSORS, S_VALUE), (F_SENSORS, S_CONFIG),
    (F_CONTROLS, C_INFO), (F_CONTROLS, C_CONTROL_INFO), (F_CONTROLS, C_VALUE), (F_CONTROLS, C_CONFIG),
}

STATUS_NAMES = ["SUCCESS", "INVALID_SESSION_ID", "INVALID_FEATURE_ID", "INVALID_COMMAND_ID",
                "INVALID_REQUEST_ID", "INVALID_PARAMETER", "TIMEOUT", "BUSY", "NOT_AUTHORIZED",
                "NOT_ACTIVE", "INVALID_STATE", "INVALID_SIZE", "INSUFFICIENT_RESOURCES",
                "NOT_SUPPORTED"]

# Value types: (struct format, size)
VALUE_TYPES = {0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2), 4: ("<I", 4), 5: ("<i", 4),
               6: ("<Q", 8), 7: ("<q", 8), 8: ("<e", 2), 9: ("<f", 4), 10: ("<d", 8)}

SENSOR_TYPE_UNITS = {0: "", 1: "RPM", 2: "°C", 3: "", 4: "W", 5: "", 6: "%", 7: "V", 8: "A",
                     9: "Ω", 10: "Wh", 11: "", 12: "s"}

# Sensor index -> (key, label), from the PSU device class in IO Center
SENSORS = [
    ("uptime", "PSU uptime"),
    ("total_runtime", "PSU total runtime"),
    ("fan_speed", "Fan speed"),
    ("temperature", "Temperature"),
    ("error_state", "Error state"),
    ("ac_input_power", "AC input power"),
    ("ac_input_voltage", "AC input voltage"),
    ("i_3v3", "3.3V current"), ("v_3v3", "3.3V voltage"),
    ("i_5v", "5V current"), ("v_5v", "5V voltage"),
    ("i_12v1", "12V1 current"), ("v_12v1", "12V1 voltage"),
    ("i_12v2", "12V2 current"), ("v_12v2", "12V2 voltage"),
    ("i_12v3", "12V3 current"), ("v_12v3", "12V3 voltage"),
    ("i_12v4", "12V4 current"), ("v_12v4", "12V4 voltage"),
    ("i_12v5", "12V5 current"), ("v_12v5", "12V5 voltage"),
    ("i_12v6", "12V6 current"), ("v_12v6", "12V6 voltage"),
]
RAILS = ["3v3", "5v", "12v1", "12v2", "12v3", "12v4", "12v5", "12v6"]

CONTROL_NAMES = {0: ("fan_mode", {0: "Silent (semi-passive)", 1: "Performance"}),
                 1: ("rail_mode", {0: "Single", 1: "Multi"})}


class PSUError(Exception):
    pass


def crc16(data):
    """CRC-16/MODBUS (poly 0xA001 reflected, init 0xFFFF)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def find_psus():
    """Return [(hidraw_path, pid, model_name)] for every connected PSU."""
    found = []
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(node, "device", "uevent")) as f:
                uevent = dict(line.strip().split("=", 1) for line in f if "=" in line)
        except OSError:
            continue
        hid_id = uevent.get("HID_ID", "")  # e.g. 0003:0000373F:00000023
        parts = hid_id.split(":")
        if len(parts) != 3:
            continue
        vid, pid = int(parts[1], 16), int(parts[2], 16)
        if vid == VENDOR_ID and pid in PSU_MODELS:
            found.append(("/dev/" + os.path.basename(node), pid, PSU_MODELS[pid]))
    return found


class PSU:
    def __init__(self, path, timeout=1.0):
        self.path = path
        self.timeout = timeout
        self.fd = os.open(path, os.O_RDWR)
        self.req_id = random.randint(1, 255)
        self.session = 0
        self.session_timeout = 0
        self.last_keepalive = 0.0
        self.sensors = []
        self.controls = []

    # -- transport -----------------------------------------------------------

    def _next_req(self):
        self.req_id = (self.req_id + 1) % 256 or 1
        return self.req_id

    def _packet(self, feature, command, data=b""):
        if (feature, command) not in READ_ONLY_COMMANDS:
            raise PSUError(f"refusing non-read-only command {feature}/{command}")
        pkt = bytearray(PACKET_SIZE)
        pkt[OFF_LEN] = len(data) + 6
        pkt[OFF_SESSION] = self.session
        pkt[OFF_REQ] = self._next_req()
        pkt[OFF_FEATURE] = feature
        pkt[OFF_CMD] = command
        pkt[OFF_DATA:OFF_DATA + len(data)] = data
        pkt[-2:] = struct.pack("<H", crc16(pkt[:-2]))
        return bytes(pkt)

    def _read_frame(self, deadline):
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise PSUError("timeout waiting for PSU reply")
            r, _, _ = select.select([self.fd], [], [], left)
            if r:
                return os.read(self.fd, PACKET_SIZE + 1)

    def command(self, feature, command, data=b""):
        pkt = self._packet(feature, command, bytes(data))
        os.write(self.fd, b"\x00" + pkt)  # report ID 0 prefix for hidraw
        deadline = time.monotonic() + self.timeout
        while True:
            buf = self._read_frame(deadline)
            if len(buf) < 8 or buf[OFF_FEATURE] != feature or buf[OFF_CMD] != command:
                continue  # notification or unrelated report
            if buf[OFF_REQ] == 0 and buf[OFF_SEQ] == 0:
                continue  # unsolicited notification
            break
        status = buf[OFF_STATUS]
        seq = buf[OFF_SEQ]
        payload = bytearray(buf[OFF_DATA if not seq & 0x7F else 3:buf[OFF_LEN] + 1])
        while seq >> 7:  # multi-frame response
            buf = self._read_frame(time.monotonic() + self.timeout)
            seq = buf[OFF_SEQ]
            payload += buf[3 if seq & 0x7F else OFF_DATA:buf[OFF_LEN] + 1]
        if status != 0:
            name = STATUS_NAMES[status] if status < len(STATUS_NAMES) else str(status)
            raise PSUError(f"PSU returned {name} for feature {feature} command {command}")
        return bytes(payload)

    # -- session -------------------------------------------------------------

    def open_session(self):
        conn_id = random.randint(10000, 99999)
        d = self.command(F_ROOT, ROOT_OPEN, struct.pack("<I", conn_id) + bytes([CLIENT_TYPE_WEB]))
        self.session = d[4]
        state = d[5] if len(d) > 5 else 0
        self.session_timeout = d[6] if len(d) > 6 else 0
        self.last_keepalive = time.monotonic()
        if state != 1:
            raise PSUError("PSU did not grant an active session "
                           "(is IO Center or another reader connected?)")
        # IO Center always does this handshake before touching sensors
        self.serial_number = self.serial()
        self.info = self.device_info()
        self.feature_list = self.features()

    def sleep(self, seconds):
        """Sleep while keeping the (2 s) session alive."""
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            time.sleep(min(left, 0.25))
            self.keepalive_if_needed()

    def keepalive_if_needed(self):
        if self.session and self.session_timeout and \
                time.monotonic() - self.last_keepalive > self.session_timeout / 2:
            self.command(F_ROOT, ROOT_KEEPALIVE)
            self.last_keepalive = time.monotonic()

    def close(self):
        try:
            if self.session:
                self.command(F_ROOT, ROOT_CLOSE, bytes([self.session]))
        except (PSUError, OSError):
            pass
        finally:
            self.session = 0
            os.close(self.fd)

    def __enter__(self):
        self.open_session()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- device info ---------------------------------------------------------

    def serial(self):
        d = self.command(F_DEVICE_INFO, DI_SERIAL)
        return d[1:1 + d[0]].decode("ascii", "replace") if d else ""

    def device_info(self):
        d = self.command(F_DEVICE_INFO, DI_INFO)
        info = {"model_id": d[0] | d[1] << 8, "revision": d[2], "mcu_versions": []}
        for i in range(d[3]):
            v = d[4 + 4 * i:8 + 4 * i]
            if len(v) == 4:  # IO Center shows these as BCD-ish major.middle.minor
                info["mcu_versions"].append(f"{v[3]:x}.{v[1]:x}{v[2]:x}.{v[0]:x}")
        return info

    def features(self):
        d = self.command(F_ROOT, ROOT_FEATURES, bytes([0]))
        return [(d[1 + 2 * i], d[2 + 2 * i]) for i in range(d[0])]

    # -- sensors -------------------------------------------------------------

    def discover(self):
        d = self.command(F_SENSORS, S_INFO)
        count = d[0]
        self.sensors = []
        for idx in range(count):
            enabled = bool(d[1 + idx // 8] >> (idx % 8) & 1)
            si = b"\0\0\0"
            if enabled:  # disabled sensors (e.g. 12V2-6 in single-rail mode) answer INVALID_STATE
                try:
                    si = self.command(F_SENSORS, S_SENSOR_INFO, bytes([idx]))
                except PSUError:
                    enabled = False
            key, label = SENSORS[idx] if idx < len(SENSORS) else (f"sensor_{idx}", f"Sensor {idx}")
            self.sensors.append({
                "index": idx, "key": key, "label": label, "enabled": enabled,
                "type": si[0], "unit": SENSOR_TYPE_UNITS.get(si[0], ""),
                "value_type": si[1], "exponent": struct.unpack("<b", si[2:3])[0],
            })
        try:
            d = self.command(F_CONTROLS, C_INFO)
            self.controls = [i for i in range(d[0])]
        except PSUError:
            self.controls = []
        return self.sensors

    def read_sensor(self, s):
        d = self.command(F_SENSORS, S_VALUE, bytes([s["index"]]))
        fmt, size = VALUE_TYPES.get(s["value_type"], ("<H", 2))
        raw = struct.unpack(fmt, d[:size].ljust(size, b"\0"))[0]
        value = raw * 10 ** s["exponent"]
        return round(value, -s["exponent"]) if s["exponent"] < 0 else value

    def read_controls(self):
        out = {}
        for idx in self.controls:
            info = self.command(F_CONTROLS, C_CONTROL_INFO, bytes([idx]))
            fmt, size = VALUE_TYPES.get(info[1], ("<B", 1))
            d = self.command(F_CONTROLS, C_VALUE, bytes([idx]))
            val = struct.unpack(fmt, d[:size].ljust(size, b"\0"))[0]
            key, labels = CONTROL_NAMES.get(idx, (f"control_{idx}", {}))
            out[key] = labels.get(val, val)
        return out

    def read_all(self):
        self.keepalive_if_needed()
        if not self.sensors:
            self.discover()
        r = {s["key"]: self.read_sensor(s) for s in self.sensors if s["enabled"]}
        dc = 0.0
        for rail in RAILS:
            i, v = r.get("i_" + rail), r.get("v_" + rail)
            if i is not None and v is not None:
                r["p_" + rail] = round(i * v, 1)
                dc += i * v
        r["dc_output_power"] = round(dc, 1)
        ac = r.get("ac_input_power")
        # AC and DC sensors refresh independently every 500 ms, so a single sample's
        # ratio swings wildly under bursty loads; the running average is the useful number.
        r["efficiency_pct"] = round(100 * dc / ac, 1) if ac else None
        if ac:
            self.ac_sum = getattr(self, "ac_sum", 0.0) + ac
            self.dc_sum = getattr(self, "dc_sum", 0.0) + dc
            r["efficiency_avg_pct"] = round(100 * self.dc_sum / self.ac_sum, 1)
        return r


def print_table(psu_name, r, controls, sensors):
    units = {s["key"]: s["unit"] for s in sensors}
    print(f"== {psu_name}")
    for key, label in SENSORS:
        if key in r:
            u = units.get(key, "")
            val = r[key]
            if key in ("uptime", "total_runtime"):
                h, rem = divmod(int(val), 3600)
                val = f"{val} ({h}h{rem // 60:02d}m)"
            print(f"  {label:<20} {val} {u}")
    print(f"  {'DC output (sum)':<20} {r['dc_output_power']} W")
    print(f"  {'Efficiency (sample)':<20} {r['efficiency_pct']} %  (noisy, see README)")
    for k, v in controls.items():
        print(f"  {k.replace('_', ' ').capitalize():<20} {v}")


def main():
    ap = argparse.ArgumentParser(description="Read-only telemetry for be quiet! Dark Power Pro 14 IO PSUs")
    ap.add_argument("--list", action="store_true", help="list connected PSUs and exit")
    ap.add_argument("--device", help="hidraw path (default: all detected PSUs)")
    ap.add_argument("--info", action="store_true", help="show serial, firmware, features, sensor metadata")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    ap.add_argument("--watch", type=float, metavar="SEC", help="repeat every SEC seconds")
    ap.add_argument("--csv", metavar="FILE", help="append readings to FILE (implies --watch 1 if not set)")
    args = ap.parse_args()

    psus = find_psus()
    if args.device:
        psus = [p for p in psus if p[0] == args.device] or [(args.device, 0, "PSU")]
    if args.list or not psus:
        if not psus:
            print("No be quiet! Dark Power Pro 14 IO PSU found "
                  "(check the IO USB cable and udev rule).", file=sys.stderr)
            return 1
        for path, pid, name in psus:
            print(f"{path}  {VENDOR_ID:04x}:{pid:04x}  {name}")
        return 0
    if args.csv and not args.watch:
        args.watch = 1.0
    # close the PSU session cleanly on `kill`/systemd stop, not only on Ctrl-C
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))

    opened = []
    try:
        for path, pid, name in psus:
            try:
                p = PSU(path)
            except PermissionError:
                print(f"{path}: permission denied (install the udev rule, see README)", file=sys.stderr)
                return 2
            p.name = name
            p.__enter__()
            opened.append(p)
            p.discover()
            p.ctl = p.read_controls()
            if args.info:
                info = p.info
                print(f"== {name} ({path})")
                print(f"  Serial        {p.serial_number}")
                print(f"  Model ID      {info['model_id']} (0x{info['model_id']:04x}), revision {info['revision']}")
                print(f"  Firmware      {', '.join(info['mcu_versions']) or '?'}")
                print(f"  Features      {', '.join(f'{FEATURE_NAMES.get(f, f)} v{v}' for f, v in p.feature_list)}")
                print(f"  Session       id {p.session}, timeout {p.session_timeout}s")
                for s in p.sensors:
                    print(f"  sensor {s['index']:2} {s['label']:<20} type {s['type']:2} "
                          f"vtype {s['value_type']} exp {s['exponent']:+d} "
                          f"{'enabled' if s['enabled'] else 'disabled'}")

        writer = csvfile = None
        while True:
            ts = time.strftime("%Y-%m-%dT%H:%M:%S")
            for p in opened:
                r = p.read_all()
                if args.csv:
                    row = {"time": ts, "device": p.path, **r}
                    if writer is None:
                        new = not os.path.exists(args.csv) or os.path.getsize(args.csv) == 0
                        csvfile = open(args.csv, "a", newline="")
                        writer = csv.DictWriter(csvfile, fieldnames=list(row), extrasaction="ignore")
                        if new:
                            writer.writeheader()
                    writer.writerow(row)
                    csvfile.flush()
                if args.json:
                    print(json.dumps({"time": ts, "device": p.path, "model": p.name, **r, **p.ctl}), flush=True)
                elif not args.csv or not args.watch:
                    print_table(f"{p.name} ({p.path}) {ts}", r, p.ctl, p.sensors)
                else:
                    print(f"{ts} {p.path} AC {r.get('ac_input_power')} W  DC {r['dc_output_power']} W  "
                          f"eff(avg) {r.get('efficiency_avg_pct')} %  fan {r.get('fan_speed')} RPM  "
                          f"{r.get('temperature')} °C", flush=True)
            if not args.watch:
                break
            deadline = time.monotonic() + args.watch
            while (left := deadline - time.monotonic()) > 0:
                for p in opened:
                    p.keepalive_if_needed()
                time.sleep(min(left, 0.25))
    except KeyboardInterrupt:
        pass
    except PSUError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    finally:
        for p in opened:
            p.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
