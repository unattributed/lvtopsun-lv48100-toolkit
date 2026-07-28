# LVTOPSUN LV48100 Read-Only Toolkit

Collect, validate, and report diagnostic information from LVTOPSUN
LV48100 battery-management systems over their RS232/PACE service
interface.

The primary workflow guides you through two batteries and creates:

- a human-readable Markdown comparison report;
- structured JSON containing decoded and raw evidence;
- a complete collection log; and
- SHA-256 checksum files for integrity verification.

The tools send only the PACE read commands documented in
[Safety and protocol notes](docs/SAFETY_AND_PROTOCOL.md). They do not
contain configuration, calibration, shutdown, MOSFET-control,
address-changing, clock-setting, or firmware commands.

> [!CAUTION]
> A lithium battery system can expose hazardous voltage and very high
> fault current. Use the correct manufacturer-approved RS232 service
> cable or an electrically compatible adapter. RS232, TTL serial, CAN,
> and RS485 are not interchangeable. Do not open a battery, move
> high-current cables, or change DIP switches for this procedure. If you
> are not qualified to work around the installed battery system, ask a
> qualified technician to connect the service cable.

## What you need

### Hardware

- One or more LVTOPSUN LV48100 batteries with a compatible PACE-style
  RS232 service port.
- The correct RS232 service cable for the battery.
- A Linux computer with a USB port.
- A USB-to-RS232 adapter if it is not already built into the service
  cable. An isolated adapter is preferable around energy-storage
  equipment.

This project was developed for the observed LV48100 protocol behavior.
Firmware and wiring revisions may differ. Stop if the connector,
electrical interface, or protocol is uncertain.

### Software

- Linux with `systemd` (Debian, Ubuntu, Raspberry Pi OS, and similar
  distributions are the intended environment).
- Python 3.9 or newer.
- Bash, `coreutils`, `util-linux`, `psmisc`, and `sudo`.
- Python package `pyserial` 3.5 or newer.

Windows and macOS are not currently supported by the guarded workflows
because serial ownership and ModemManager handling are Linux-specific.

## Quick start

### 1. Download and prepare the toolkit

```bash
git clone https://github.com/unattributed/lvtopsun-lv48100-toolkit.git
cd lvtopsun-lv48100-toolkit
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

On Debian/Ubuntu, install the operating-system prerequisites if needed:

```bash
sudo apt update
sudo apt install git python3 python3-venv python3-pip psmisc
```

### 2. Connect and identify the serial adapter

Connect only the RS232 service cable, then list stable device names:

```bash
ls -l /dev/serial/by-id/
```

Copy the full path shown for your adapter. A stable
`/dev/serial/by-id/...` name is better than `/dev/ttyUSB0`, which can
change after a reboot. If no `by-id` entry exists, inspect:

```bash
ls -l /dev/ttyUSB* /dev/ttyACM*
```

If access is denied, add your user to the serial-port group, sign out,
and sign in again:

```bash
sudo usermod -aG dialout "$USER"
```

### 3. Collect and compare two batteries

Replace the example device with the path found above:

```bash
python3 battery/collect_lvtopsun_lv48100_two_battery_report_data.py \
  --device /dev/serial/by-id/YOUR_ADAPTER_NAME \
  --output-dir reports
```

Read each prompt before responding. The program will ask you to confirm
battery 1, collect it, and then move **only the RS232 service cable** to
battery 2. It normally tries PACE address 1 for battery 1 and addresses
2 then 1 for battery 2.

For a direct adapter at `/dev/ttyUSB0`, the shorter form is:

```bash
python3 battery/collect_lvtopsun_lv48100_two_battery_report_data.py
```

You can also set reusable defaults:

```bash
export LV48100_DEVICE=/dev/serial/by-id/YOUR_ADAPTER_NAME
export LV48100_OUTPUT_DIR="$PWD/reports"
python3 battery/collect_lvtopsun_lv48100_two_battery_report_data.py
```

### 4. Review the report

Successful collection creates files similar to:

```text
reports/
├── lvtopsun-lv48100-two-battery-collection-20260728T120000Z.log
├── lvtopsun-lv48100-two-battery-report-data-20260728T120000Z.json
├── lvtopsun-lv48100-two-battery-report-data-20260728T120000Z.json.sha256
├── lvtopsun-lv48100-two-battery-report-source-20260728T120000Z.md
└── lvtopsun-lv48100-two-battery-report-source-20260728T120000Z.md.sha256
```

Open the `.md` file in any text editor or Markdown viewer. Verify
integrity from the report directory:

```bash
cd reports
sha256sum --check *.sha256
```

See [Reporting guide](docs/REPORTING_GUIDE.md) for interpretation,
privacy review, and optional PDF export.

## One-battery status and identity collection

The guarded wrapper collects warnings, protection state, balancing
state, hardware version, and serial identity for one battery:

```bash
SERIAL_LINK=/dev/serial/by-id/YOUR_ADAPTER_NAME \
OUTPUT_DIR="$PWD/reports" \
battery/query_lvtopsun_lv48100_readonly_status_identity.sh 1
```

The final `1` is the PACE pack address and must be from 1 through 15.
This smaller workflow does not collect the full capacity and cell-level
comparison used by the two-battery report.

## Tool reference

| Tool | Intended use |
|---|---|
| `collect_lvtopsun_lv48100_two_battery_report_data.py` | Recommended guided two-pack collection and Markdown report |
| `query_lvtopsun_lv48100_readonly_status_identity.sh` | Safe wrapper for a single-pack status/identity snapshot |
| `run_lvtopsun_lv48100_collector_guarded.sh` | Extra preflight, allowlist, timeout, and recovery checks around the single-pack wrapper |
| `query_lvtopsun_lv48100_second_pack_readonly.py` | Advanced attempt to read a second addressed pack through a directly connected master |
| `validate_lvtopsun_lv48100_battery2_live_telemetry.py` | Repeated direct battery-2 sampling that rejects zero-filled telemetry |
| `audit_interrupted_lvtopsun_lv48100_query.sh` | Recovery audit that does not open or transmit to the serial port |

Run any Python tool with `--help` before using its advanced options:

```bash
python3 battery/query_lvtopsun_lv48100_second_pack_readonly.py --help
```

## What the report means

The full report includes pack voltage/current, cell voltages, cell
differential, temperatures, reported capacity values, cycle count,
warnings, protections, faults, balancing state, hardware identity, and
serial identity when the BMS provides them.

State of charge (SOC) and state of health (SOH) are calculations from
BMS-reported capacity fields. They are not independent laboratory
capacity tests. Values are point-in-time observations, and the two
batteries are sampled sequentially rather than simultaneously.

## Troubleshooting

### `serial device does not exist`

Unplug and reconnect the adapter, run `ls -l /dev/serial/by-id/`, and
pass the exact path with `--device`.

### `Permission denied`

Confirm your user belongs to `dialout` with `groups`. After using
`usermod`, you must sign out and back in. Avoid running the entire
collector as root.

### Device is busy

Close serial terminals, inverter-monitoring applications, and other
BMS tools. The guarded workflows temporarily stop ModemManager only if
it was active and restore it afterward.

### No response or checksum/frame error

Stop and verify the service cable type, connector pinout, selected port,
and battery power state. Do not experiment with adjacent battery ports
or improvised wiring.

### Battery 2 returns zeros

Do not treat a syntactically valid zero-filled frame as real battery
data. Connect the service cable directly to battery 2 and use:

```bash
python3 battery/validate_lvtopsun_lv48100_battery2_live_telemetry.py \
  --device /dev/serial/by-id/YOUR_ADAPTER_NAME \
  --output-dir reports
```

## Privacy

JSON, Markdown, and log files may contain battery serial numbers,
hardware versions, timestamps, and local device paths. Review and
redact reports before publishing or attaching them to support tickets.
Generated reports are ignored by Git by default.

## Development

The protocol and report tests do not require battery hardware:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q battery tests
bash -n battery/*.sh
```

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Project status and disclaimer

This is independent community tooling, provided without warranty and
not affiliated with or endorsed by LVTOPSUN. It is intentionally
read-only, but software cannot eliminate electrical, wiring, battery,
or protocol risks. You are responsible for confirming compatibility
with your equipment.

Licensed under the [MIT License](LICENSE).
