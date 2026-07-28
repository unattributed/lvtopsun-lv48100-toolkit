# Safety and protocol notes

## Safety boundary

These tools are designed to open a serial service interface and issue a
small allowlist of read requests. They do not make work around a battery
system electrically safe.

- Use only the documented RS232 service port and a compatible cable.
- Do not connect a TTL UART adapter to an RS232 port.
- Do not assume RS232, RS485, and CAN connectors are electrically or
  mechanically interchangeable.
- Do not move high-current, inverter communication, or inter-battery
  cables as part of collection.
- Do not change address/DIP switches merely to make this software
  respond.
- Stop if the port, pinout, voltage levels, or battery state are
  uncertain.

The guided two-battery workflow asks the operator to move only the
service cable. Follow the installed system's manufacturer and qualified
technician requirements first if they differ.

## Read-command allowlist

The repository uses these PACE CID2 operations:

| CID2 | Purpose |
|---:|---|
| `0x90` | Read BMS count |
| `0x42` | Read analogue information |
| `0x44` | Read status information |
| `0xC1` | Read hardware version |
| `0xC2` | Read serial number |

The collectors preserve raw request/response evidence alongside decoded
fields. Protocol frames use the observed PACE `0x25` protocol version,
`0x46` lithium-battery CID1, 9600 baud, 8 data bits, no parity, and one
stop bit.

No write/control CID2 constants should be added without redesigning the
project's safety model, user interface, review process, and name. Pull
requests that introduce battery writes or controls are out of scope.

## Serial-port ownership

Desktop ModemManager can claim USB serial adapters. The guarded tools:

1. record whether ModemManager is active;
2. stop it only when necessary;
3. check whether another process owns the resolved device;
4. run the collector with bounded serial timeouts; and
5. restore ModemManager if it was active before collection.

Signal and exception cleanup is best-effort. After an interrupted run,
use `audit_interrupted_lvtopsun_lv48100_query.sh`. That audit does not
open the serial port or transmit a battery request.

## Compatibility limits

The decoder reflects responses observed from LVTOPSUN LV48100 batteries
using a PACE-style BMS interface. A similar connector or frame prefix
does not prove compatibility. Unknown trailing vendor bytes are
preserved rather than assigned speculative meanings.

Zero-filled responses can be structurally valid but semantically
useless. The live-validation tool checks for plausible nonzero telemetry
and marks rejected samples rather than reporting zeros as battery
measurements.
