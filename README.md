# bqpsu — be quiet! Dark Power Pro 14 IO telemetry on Linux

`bqpsu.py` is a read-only reader for the USB telemetry in be quiet!'s
**Dark Power Pro 14 IO** power supplies. It needs only Python 3 (standard library) and
the kernel's `hidraw` driver. You don't need IO Center, a browser or any be quiet! servers.

| USB ID | Model |
|---|---|
| `373f:0023` | Dark Power Pro 14 IO 1300W (tested: firmware 1.07.0) |
| `373f:0024` | Dark Power Pro 14 IO 1600W (same protocol, untested) |

The protocol was reverse-engineered from be quiet!'s IO Center Web app, using the archived
`iocenter.bequiet.com` bundle from June 2026. See [Protocol](#protocol).

## 1. Connect the PSU

1. Plug the supplied cable into the **IO USB** (USB-C) port on the PSU's modular panel.
2. Plug the other end into a free **internal USB 2.0 header** on the motherboard.
3. Boot and check that the PSU is detected:

   ```bash
   lsusb -d 373f:
   # Bus 001 Device 008: ID 373f:0023 be quiet! DARK POWER PRO 14 IO
   ```

   The kernel binds it to `hid-generic`, and it appears as `/dev/hidrawN`.
   `N` can change between boots, so `bqpsu.py` finds the device by USB ID.

## 2. Allow non-root access (udev)

By default `/dev/hidraw*` is root-only. Install the rule from this folder:

```bash
sudo install -m 644 71-bequiet-psu.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=hidraw
```

The rule gives group `plugdev` read/write access and adds `uaccess`, so the user logged
in at the desktop also gets access. Check it:

```bash
ls -l /dev/hidraw*            # the PSU node should be  crw-rw----+ root plugdev
id -nG | grep -w plugdev      # your user must be in plugdev
sudo usermod -aG plugdev $USER   # if not; then log out/in
```

The same rule also lets the IO Center Web app work in Chrome/Chromium, which uses
WebHID. Firefox has no WebHID support, and snap Chromium may block hidraw.

## 3. Use

```bash
./bqpsu.py --list                 # detected PSUs
./bqpsu.py                        # one reading, as a table
./bqpsu.py --info                 # serial, firmware, features, sensor metadata
./bqpsu.py --json                 # one JSON line (for scripts)
./bqpsu.py --watch 1              # table every second
./bqpsu.py --watch 1 --json       # JSON line every second
./bqpsu.py --csv psu.csv          # append CSV every 1 s (with --watch N for another interval)
./bqpsu.py --device /dev/hidraw7  # pick one PSU when several are connected
```

By default every detected PSU is read. Stop with Ctrl-C or `kill`; the session is closed
cleanly either way.

Example:

```
== Dark Power Pro 14 IO 1300W (/dev/hidraw7) 2026-10-22T12:52:03
  PSU uptime           13413 (3h43m) s
  Fan speed            366 RPM
  Temperature          57 °C
  Error state          0
  AC input power       471.4 W
  AC input voltage     224.0 V
  3.3V current         0.32 A     3.3V voltage   3.28 V
  5V current           2.39 A     5V voltage     5.05 V
  12V1 current         57.69 A    12V1 voltage   11.91 V
  DC output (sum)      700.2 W
  Fan mode             Silent (semi-passive)
  Rail mode            Single
```

### What you get

The PSU exposes 23 sensors:

- uptime and total runtime
- fan RPM and temperature
- error state
- AC input power and voltage
- current and voltage on 3.3 V, 5 V and 12V1 to 12V6

The reader also computes:

- watts per rail and total DC output
- efficiency, as a single sample and as a running average
- the fan mode and rail mode, read only

**12V2 to 12V6 only appear in multi-rail mode.** In single-rail mode the PSU disables
those sensors (enabled mask `ff 1f 00`), and all 12 V current is reported on 12V1.

**Efficiency is noisy under bursty loads.** The AC and DC sensors each refresh on their
own 500 ms timer. With a GPU training job swinging hundreds of watts within a second,
one sample's DC/AC ratio can read 60–150%. Use `efficiency_avg_pct` over a long
`--watch` run, or average the CSV yourself.

### Log next to the GPU

```bash
./bqpsu.py --csv ~/work/psu_$(date +%F).csv --watch 1 &
nvidia-smi --query-gpu=timestamp,power.draw,temperature.gpu,clocks.sm --format=csv -l 1 > ~/work/gpu_$(date +%F).csv &
```

### Run as a service (optional)

`/etc/systemd/system/bqpsu.service`:

```ini
[Unit]
Description=be quiet! PSU telemetry logger

[Service]
User=user
ExecStart=/opt/bequiet-psu/bqpsu.py --watch 5 --csv /opt/bequiet-psu/psu.csv
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now bqpsu
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No ... PSU found` | Check the cable (`lsusb -d 373f:`). The internal USB header may be disabled in BIOS. |
| `permission denied` | Install the udev rule (step 2) and log in again after joining `plugdev`. |
| `did not grant an active session` / `BUSY` | Another client holds the PSU, e.g. IO Center Web open in Chrome or a second `bqpsu.py`. The PSU allows one session at a time. Close the other client. |
| `timeout waiting for PSU reply` | Replug the USB cable, or run `sudo udevadm trigger`. |

## Protocol

These notes are for anyone extending the reader.

- **Transport:** USB HID, vendor usage page `0xFF00`, 64-byte input and output
  reports, no report ID. On Linux, write `0x00` followed by the 64 bytes to
  `/dev/hidrawN`; reads return 64 bytes.
- **Packet layout:**

  | Byte | Field |
  |---|---|
  | 0 | payload length (data + 6) |
  | 1 | sequence ID (bit 7 = more frames follow) |
  | 2 | session ID |
  | 3 | status |
  | 4 | request ID (1–255, rolling) |
  | 5 | feature ID |
  | 6 | command ID |
  | 7…61 | data |
  | 62–63 | CRC-16/MODBUS of bytes 0–61, little-endian |

- **Notifications** have request ID 0 and sequence 0.
- **Session:**
  1. `ROOT/OpenSession` with data = random u32 + client type (2 = Web). The reply gives
     the session ID (byte 4), state (byte 5, 1 = active) and timeout in seconds (byte 6;
     2 s on firmware 1.07).
  2. Send `ROOT/KeepAlive` at least every timeout/2.
  3. End with `ROOT/CloseSession` [session ID].
- **Features on this PSU:** ROOT(1) v2, DEVICE_INFO(3), SENSORS(48), CONTROLS(49) and
  KEY_VALUE_STORAGE(52), all v1.
- **SENSORS commands:**
  - `GetInfo` (1) returns a count and an enabled bitmask.
  - `GetSensorInfo` (2) [idx] returns the type, the value type (0 = u8, 2 = u16, 6 = u64, …)
    and an exponent (i8). The value is raw × 10^exp.
  - `GetSensorValue` (3) [idx] returns the raw value, little-endian.
- **CONTROLS:**
  - Control 0 is the fan mode: 0 = Silent/semi-passive, 1 = Performance.
  - Control 1 is the 12 V rail mode: 0 = Single, 1 = Multi.

### Write commands (deliberately not implemented)

`bqpsu.py` refuses to build anything outside its read-only allowlist. The protocol does
have these write commands:

| Feature / command | Effect |
|---|---|
| `CONTROLS/SetControlValue` (4) | **switch fan mode or 12 V rail mode** (what IO Center's toggles send) |
| `CONTROLS/SetControlConfig` (6), `SetControlCurve` (8), `SetControlPID` (10) | change control behaviour (curve/PID modes; not used by the PSU UI) |
| `SENSORS/SetSensorConfig` (5) | change sensor update rate / notification throttling |
| `ROOT/RequestStateChange` (7), `SendStateChangeDecision` (8) | device state transitions (exact effect unknown) |
| `DEVICE_INFO/SetSerialNumber` (3), `ResetSerialNumber` (4) | overwrite the serial number |
| `DEVICE_INFO/FactoryReset` (5) | factory reset |
| `DFU` feature (2) | firmware update |
| `KEY_VALUE_STORAGE` (52) | persistent storage on the PSU |

Switching the rail mode under load changes how over-current protection behaves. If
support for it is added, do it with the system idle.

My site: 
[be quiet! Dark Power Pro 14 IO telemetry on Linux](https://meow.kyiv.ua/be-quiet-dark-power-pro-14-io-telemetry-on-linux/diy/)
