# Contributing

Bug reports, anonymized protocol observations, documentation
improvements, and read-only decoder fixes are welcome.

## Before opening an issue

- Remove battery serial numbers, usernames, local paths, and installation
  details from logs and JSON.
- State the battery model, known firmware, adapter type, operating
  system, command used, and exit status.
- Include the smallest relevant redacted frame or traceback.
- Say whether the adapter was connected directly to the affected pack.

Never publish proprietary manuals or data you do not have permission to
share.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 -m unittest discover -s tests -v
python3 -m compileall -q battery tests
bash -n battery/*.sh
```

Keep changes read-only. Write, configuration, calibration, shutdown,
MOSFET-control, addressing, and firmware commands are outside this
project's scope.

Use focused commits and explain any protocol interpretation with
anonymized raw evidence. Hardware-dependent behavior should fail safely
when the serial device is missing, inaccessible, busy, or returns an
invalid frame.
