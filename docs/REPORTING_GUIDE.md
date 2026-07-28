# Reporting guide

## Choosing the evidence file

For a two-battery assessment, use the generated Markdown file as the
human-readable report and retain its matching JSON, log, and SHA-256
sidecars. The JSON is the authoritative structured evidence; the
Markdown is a summary derived from that same capture.

Do not combine files from different timestamped runs without clearly
stating that the observations were collected at different times.

## Reading the comparison

- **Pack voltage and current** are point-in-time BMS observations.
  Current direction depends on the sign convention decoded by the
  tool.
- **Cell differential** is the highest reported cell voltage minus the
  lowest. Interpret it in the context of charge state, load, resting
  time, temperature, and the manufacturer's limits.
- **Temperatures** come from the sensors reported by the BMS; sensor
  placement is not inferred.
- **Remaining, full, and design capacity** are values maintained by the
  BMS.
- **Calculated SOC** is remaining capacity divided by learned full
  capacity.
- **Calculated SOH** is learned full capacity divided by design
  capacity.
- **Cycle count** is the BMS-maintained count and may not match another
  vendor's definition of a full-equivalent cycle.
- **Warnings, protections, and faults** are decoded from the status
  response. An empty active list means none were reported at capture
  time; it does not prove there have never been historical events.

SOC and SOH are not independent capacity tests. For warranty,
commissioning, or safety decisions, follow the battery manufacturer's
procedure and use qualified test equipment.

## Evidence integrity

Each `.sha256` sidecar records the digest of its neighboring evidence
file. From the output directory:

```bash
sha256sum --check *.sha256
```

`OK` confirms that the file has not changed since collection. It does
not certify the battery, the adapter, the protocol interpretation, or
the operator procedure.

## Privacy review

Before sharing a report, search it for:

- battery serial numbers;
- local usernames and directory paths;
- serial-device identifiers;
- exact capture times;
- installation names or notes added after collection.

Keep the original evidence private if you create a redacted copy.
Record that the shared copy was redacted and do not reuse the original
checksum for a modified file.

## Optional PDF export

The Markdown file is deliberately portable. If Pandoc is installed:

```bash
pandoc \
  reports/lvtopsun-lv48100-two-battery-report-source-TIMESTAMP.md \
  -o lv48100-battery-report.pdf
```

The PDF is a presentation copy. Retain the JSON, log, Markdown, and
checksums for traceability.

## Suggested report notes

Add installation context outside the generated evidence:

- battery model and firmware, if independently known;
- connection topology;
- whether the system was charging, discharging, or resting;
- approximate time since the last major load or charge;
- ambient temperature;
- why the capture was taken; and
- any cable or address deviations from the standard workflow.

Keep observations separate from conclusions. A single telemetry capture
is useful for comparison and troubleshooting but is not a substitute
for longitudinal monitoring or a controlled capacity test.
