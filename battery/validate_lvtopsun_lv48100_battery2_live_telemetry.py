#!/usr/bin/env python3
"""Validate live battery telemetry and reject zero-filled responses."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import serial
except ImportError as exc:
    print(
        "FAIL: Python module 'serial' is unavailable.\n"
        "Install it with: sudo apt install python3-serial",
        file=sys.stderr,
    )
    raise SystemExit(20) from exc


PROTOCOL_VERSION = 0x25
CID1_LITHIUM_IRON = 0x46
CID2_READ_ANALOG_INFORMATION = 0x42
CID2_READ_STATUS_INFORMATION = 0x44
CID2_READ_HARDWARE_VERSION = 0xC1
CID2_READ_SERIAL_NUMBER = 0xC2

DEFAULT_DEVICE = os.environ.get(
    "LV48100_DEVICE",
    "/dev/ttyUSB0",
)
DEFAULT_OUTPUT_DIR = os.environ.get(
    "LV48100_OUTPUT_DIR",
    "reports",
)


class PaceError(RuntimeError):
    """PACE transport or frame validation failure."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def filename_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def calculate_length_field(info_ascii_length: int) -> int:
    if not 0 <= info_ascii_length <= 0x0FFF:
        raise PaceError(
            f"INFO ASCII length is outside the PACE range: "
            f"{info_ascii_length}"
        )

    length_id = info_ascii_length & 0x0FFF
    nibble_sum = (
        ((length_id >> 8) & 0x0F)
        + ((length_id >> 4) & 0x0F)
        + (length_id & 0x0F)
    )
    checksum_nibble = (-nibble_sum) & 0x0F
    return (checksum_nibble << 12) | length_id


def frame_checksum(body: bytes) -> int:
    return (-sum(body)) & 0xFFFF


def build_request(
    address: int,
    cid2: int,
    payload: bytes = b"",
) -> bytes:
    info_ascii = payload.hex().upper()
    body = (
        f"{PROTOCOL_VERSION:02X}"
        f"{address:02X}"
        f"{CID1_LITHIUM_IRON:02X}"
        f"{cid2:02X}"
        f"{calculate_length_field(len(info_ascii)):04X}"
        f"{info_ascii}"
    ).encode("ascii")

    return (
        b"~"
        + body
        + f"{frame_checksum(body):04X}".encode("ascii")
        + b"\r"
    )


def verify_request_generation() -> None:
    expected = {
        (2, CID2_READ_ANALOG_INFORMATION, b"\x02"):
            b"~25024642E00202FD2E\r",
        (2, CID2_READ_STATUS_INFORMATION, b"\x02"):
            b"~25024644E00202FD2C\r",
        (2, CID2_READ_HARDWARE_VERSION, b""):
            b"~250246C10000FD99\r",
        (2, CID2_READ_SERIAL_NUMBER, b""):
            b"~250246C20000FD98\r",
        (1, CID2_READ_ANALOG_INFORMATION, b"\x01"):
            b"~25014642E00201FD30\r",
        (1, CID2_READ_STATUS_INFORMATION, b"\x01"):
            b"~25014644E00201FD2E\r",
    }

    for arguments, expected_frame in expected.items():
        actual = build_request(*arguments)

        if actual != expected_frame:
            raise PaceError(
                "request-generation self-test failed, "
                f"arguments={arguments!r}, actual={actual!r}, "
                f"expected={expected_frame!r}"
            )


def parse_response(response: bytes) -> dict[str, Any]:
    if not response:
        raise PaceError("no response was received")

    if not response.startswith(b"~"):
        raise PaceError("response does not begin with PACE SOI 0x7E")

    if not response.endswith(b"\r"):
        raise PaceError("response does not end with PACE EOI 0x0D")

    body = response[1:-1]

    if len(body) < 16:
        raise PaceError(
            f"response is too short for a PACE frame: {len(body)} bytes"
        )

    try:
        text = body.decode("ascii")
    except UnicodeDecodeError as exc:
        raise PaceError(
            "response contains non-ASCII frame data"
        ) from exc

    try:
        version = int(text[0:2], 16)
        header_address = int(text[2:4], 16)
        cid1 = int(text[4:6], 16)
        return_code = int(text[6:8], 16)
        length_field = int(text[8:12], 16)
        received_checksum = int(text[-4:], 16)
    except ValueError as exc:
        raise PaceError(
            "response header contains invalid hexadecimal ASCII"
        ) from exc

    info_ascii = text[12:-4]
    calculated_checksum = frame_checksum(body[:-4])

    if received_checksum != calculated_checksum:
        raise PaceError(
            "frame checksum mismatch, "
            f"received {received_checksum:04X}, "
            f"calculated {calculated_checksum:04X}"
        )

    declared_length = length_field & 0x0FFF

    if len(info_ascii) != declared_length:
        raise PaceError(
            "INFO length mismatch, "
            f"declared {declared_length}, received {len(info_ascii)}"
        )

    if length_field != calculate_length_field(declared_length):
        raise PaceError("PACE LENGTH checksum is invalid")

    if len(info_ascii) % 2:
        raise PaceError("INFO contains an odd number of hex characters")

    try:
        payload = bytes.fromhex(info_ascii)
    except ValueError as exc:
        raise PaceError("INFO contains invalid hexadecimal") from exc

    if return_code != 0:
        raise PaceError(
            f"BMS returned non-zero status RTN={return_code:02X}"
        )

    return {
        "version": version,
        "version_hex": f"{version:02X}",
        "header_address": header_address,
        "header_address_hex": f"{header_address:02X}",
        "cid1": cid1,
        "cid1_hex": f"{cid1:02X}",
        "return_code": return_code,
        "return_code_hex": f"{return_code:02X}",
        "length_field_hex": f"{length_field:04X}",
        "payload": payload,
        "payload_hex": payload.hex(" ").upper(),
        "payload_byte_length": len(payload),
        "received_checksum": f"{received_checksum:04X}",
        "calculated_checksum": f"{calculated_checksum:04X}",
        "checksum_valid": True,
        "response_ascii": response.decode(
            "ascii",
            errors="backslashreplace",
        ).replace("\r", "\\r"),
        "response_hex": response.hex(" ").upper(),
    }


def u16(data: bytes) -> int:
    if len(data) != 2:
        raise PaceError(f"expected two bytes, received {len(data)}")
    return int.from_bytes(data, byteorder="big", signed=False)


def i16(data: bytes) -> int:
    if len(data) != 2:
        raise PaceError(f"expected two bytes, received {len(data)}")
    return int.from_bytes(data, byteorder="big", signed=True)


def decode_analog(
    payload: bytes,
    expected_address: int,
) -> dict[str, Any]:
    offset = 0

    def take_byte(label: str) -> int:
        nonlocal offset
        if offset >= len(payload):
            raise PaceError(
                f"analogue payload ended while reading {label}"
            )
        value = payload[offset]
        offset += 1
        return value

    def take_u16(label: str) -> int:
        nonlocal offset
        end = offset + 2
        if end > len(payload):
            raise PaceError(
                f"analogue payload ended while reading {label}"
            )
        value = u16(payload[offset:end])
        offset = end
        return value

    def take_i16(label: str) -> int:
        nonlocal offset
        end = offset + 2
        if end > len(payload):
            raise PaceError(
                f"analogue payload ended while reading {label}"
            )
        value = i16(payload[offset:end])
        offset = end
        return value

    leading = take_byte("leading byte")
    responding_address = take_byte("responding address")

    if responding_address != expected_address:
        raise PaceError(
            "analogue payload address mismatch, "
            f"expected {expected_address}, "
            f"received {responding_address}"
        )

    cell_count = take_byte("cell count")

    if not 1 <= cell_count <= 32:
        raise PaceError(f"unreasonable cell count: {cell_count}")

    cell_mv = [
        take_u16(f"cell {number}")
        for number in range(1, cell_count + 1)
    ]

    temperature_count = take_byte("temperature count")

    if not 0 <= temperature_count <= 16:
        raise PaceError(
            f"unreasonable temperature count: {temperature_count}"
        )

    temperature_raw = [
        take_u16(f"temperature {number}")
        for number in range(1, temperature_count + 1)
    ]

    current_raw = take_i16("current")
    pack_mv = take_u16("pack voltage")
    remaining_raw = take_u16("remaining capacity")
    user_defined = take_byte("user-defined value")
    full_raw = take_u16("full capacity")
    cycles = take_u16("cycle count")
    design_raw = take_u16("design capacity")
    trailing = payload[offset:]

    temperatures_c = [
        round((value - 2730) / 10.0, 1)
        for value in temperature_raw
    ]

    return {
        "leading_byte_hex": f"{leading:02X}",
        "responding_address": responding_address,
        "cell_count": cell_count,
        "cell_voltages_mv": cell_mv,
        "temperature_count": temperature_count,
        "temperature_raw": temperature_raw,
        "temperatures_c": temperatures_c,
        "current_a": round(current_raw / 100.0, 2),
        "pack_voltage_v": round(pack_mv / 1000.0, 3),
        "remaining_capacity_ah": round(remaining_raw / 100.0, 2),
        "full_capacity_ah": round(full_raw / 100.0, 2),
        "design_capacity_ah": round(design_raw / 100.0, 2),
        "cycle_count": cycles,
        "user_defined_value": user_defined,
        "vendor_trailing_hex": trailing.hex(" ").upper(),
    }


def decode_status(
    payload: bytes,
    expected_address: int,
) -> dict[str, Any]:
    offset = 0

    def take(label: str) -> int:
        nonlocal offset
        if offset >= len(payload):
            raise PaceError(
                f"status payload ended while reading {label}"
            )
        value = payload[offset]
        offset += 1
        return value

    leading = take("leading byte")
    responding_address = take("responding address")

    if responding_address != expected_address:
        raise PaceError(
            "status payload address mismatch, "
            f"expected {expected_address}, "
            f"received {responding_address}"
        )

    cell_count = take("cell count")
    cell_warnings = [
        take(f"cell warning {number}")
        for number in range(1, cell_count + 1)
    ]

    temperature_count = take("temperature count")
    temperature_warnings = [
        take(f"temperature warning {number}")
        for number in range(1, temperature_count + 1)
    ]

    charge_current_warning = take("charge current warning")
    total_voltage_warning = take("total voltage warning")
    discharge_current_warning = take("discharge current warning")
    protection_1 = take("protection 1")
    protection_2 = take("protection 2")
    system_status = take("system status")
    configuration_status = take("configuration status")
    fault_status = take("fault status")
    balance_high = take("balance high")
    balance_low = take("balance low")
    warning_1 = take("warning 1")
    warning_2 = take("warning 2")

    return {
        "leading_byte_hex": f"{leading:02X}",
        "responding_address": responding_address,
        "cell_count": cell_count,
        "cell_warning_values": cell_warnings,
        "temperature_count": temperature_count,
        "temperature_warning_values": temperature_warnings,
        "charge_current_warning": charge_current_warning,
        "total_voltage_warning": total_voltage_warning,
        "discharge_current_warning": discharge_current_warning,
        "protection_1": protection_1,
        "protection_2": protection_2,
        "system_status": system_status,
        "configuration_status": configuration_status,
        "fault_status": fault_status,
        "balancing_mask": (balance_high << 8) | balance_low,
        "warning_1": warning_1,
        "warning_2": warning_2,
        "vendor_trailing_hex": payload[offset:].hex(" ").upper(),
    }


def decode_identity(payload: bytes) -> str:
    return payload.rstrip(b"\x00 ").decode(
        "ascii",
        errors="backslashreplace",
    )


def semantic_assessment(
    analog: dict[str, Any],
    status: dict[str, Any],
) -> dict[str, Any]:
    cell_mv = analog["cell_voltages_mv"]
    nonzero_cells = sum(1 for value in cell_mv if value > 0)
    cell_sum_mv = sum(cell_mv)
    pack_mv = round(analog["pack_voltage_v"] * 1000)
    voltage_agreement_mv = abs(cell_sum_mv - pack_mv)

    reasons: list[str] = []

    if pack_mv < 30000:
        reasons.append(
            "pack voltage is below 30 V and is not plausible for an "
            "energized LV48100 pack"
        )

    if nonzero_cells != analog["cell_count"]:
        reasons.append(
            f"only {nonzero_cells} of {analog['cell_count']} cells "
            "reported nonzero voltage"
        )

    if any(
        value < 2000 or value > 4000
        for value in cell_mv
    ):
        reasons.append(
            "one or more cell voltages fall outside the broad "
            "2.0 V to 4.0 V plausibility range"
        )

    if pack_mv >= 30000 and voltage_agreement_mv > 1000:
        reasons.append(
            "sum of cell voltages differs from pack voltage by "
            f"{voltage_agreement_mv} mV"
        )

    if analog["design_capacity_ah"] <= 0:
        reasons.append("design capacity is zero")

    if analog["full_capacity_ah"] <= 0:
        reasons.append("learned full capacity is zero")

    all_cells_low = (
        bool(status["cell_warning_values"])
        and all(
            value == 1
            for value in status["cell_warning_values"]
        )
    )

    if all_cells_low:
        reasons.append(
            "every reported cell warning is below lower limit"
        )

    if status["total_voltage_warning"] == 1:
        reasons.append(
            "total-voltage warning is below lower limit"
        )

    plausible = not reasons

    return {
        "plausible_live_telemetry": plausible,
        "rejection_reasons": reasons,
        "nonzero_cell_count": nonzero_cells,
        "reported_cell_count": analog["cell_count"],
        "cell_sum_mv": cell_sum_mv,
        "pack_voltage_mv": pack_mv,
        "cell_sum_pack_difference_mv": voltage_agreement_mv,
        "all_cells_below_lower_limit": all_cells_low,
    }


def run(
    command: list[str],
    *,
    check: bool = False,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        check=check,
        capture_output=capture_output,
    )


def service_active() -> bool:
    return (
        run(
            [
                "systemctl",
                "is-active",
                "--quiet",
                "ModemManager.service",
            ]
        ).returncode
        == 0
    )


def serial_busy(device: str) -> bool:
    return (
        run(
            ["sudo", "fuser", device],
            capture_output=True,
        ).returncode
        == 0
    )


def query(
    port: serial.Serial,
    *,
    address: int,
    cid2: int,
    payload: bytes,
) -> dict[str, Any]:
    request = build_request(address, cid2, payload)

    port.reset_input_buffer()
    port.reset_output_buffer()

    written = port.write(request)
    port.flush()

    if written != len(request):
        raise PaceError(
            f"wrote {written} of {len(request)} request bytes"
        )

    response = port.read_until(expected=b"\r", size=4096)
    parsed = parse_response(response)

    if parsed["version"] != PROTOCOL_VERSION:
        raise PaceError(
            f"unexpected protocol version {parsed['version_hex']}"
        )

    if parsed["cid1"] != CID1_LITHIUM_IRON:
        raise PaceError(f"unexpected CID1 {parsed['cid1_hex']}")

    payload_bytes = parsed.pop("payload")

    result = {
        "request_ascii": request.decode("ascii").replace(
            "\r",
            "\\r",
        ),
        "request_hex": request.hex(" ").upper(),
        "response": parsed,
        "payload_bytes": payload_bytes,
    }

    time.sleep(0.25)
    return result


def serialisable(
    query_result: dict[str, Any],
) -> dict[str, Any]:
    result = dict(query_result)
    payload = result.pop("payload_bytes")
    result["payload_hex"] = payload.hex(" ").upper()
    result["payload_byte_length"] = len(payload)
    return result


def write_evidence(
    path: Path,
    report: dict[str, Any],
) -> Path:
    content = (
        json.dumps(report, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")

    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    checksum_path = Path(f"{path}.sha256")
    checksum_path.write_text(
        f"{digest}  {path.name}\n",
        encoding="utf-8",
    )
    return checksum_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Directly revalidate LVTOPSUN LV48100 battery 2 "
            "telemetry and reject zero-filled PACE responses."
        )
    )
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--addresses",
        default="2,1",
        help="Comma-separated direct PACE addresses to probe.",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=3,
        help="Samples per responding address. Default: 3.",
    )
    parser.add_argument(
        "--sample-delay",
        type=float,
        default=5.0,
        help="Seconds between samples. Default: 5.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        addresses = [
            int(item.strip())
            for item in args.addresses.split(",")
            if item.strip()
        ]
    except ValueError:
        print(
            "FAIL: addresses must be comma-separated integers",
            file=sys.stderr,
        )
        return 10

    if not addresses or any(
        address < 1 or address > 15
        for address in addresses
    ):
        print(
            "FAIL: addresses must be in the range 1 through 15",
            file=sys.stderr,
        )
        return 11

    if args.samples < 1 or args.samples > 10:
        print(
            "FAIL: samples must be in the range 1 through 10",
            file=sys.stderr,
        )
        return 12

    verify_request_generation()

    device_link = Path(args.device)

    if not device_link.exists():
        print(
            f"FAIL: serial device is absent: {device_link}",
            file=sys.stderr,
        )
        return 20

    resolved_device = device_link.resolve(strict=True)

    if not os.access(resolved_device, os.R_OK | os.W_OK):
        print(
            f"FAIL: current user cannot access {resolved_device}",
            file=sys.stderr,
        )
        return 21

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = filename_timestamp()
    json_path = output_dir / (
        "lvtopsun-lv48100-battery2-live-validation-"
        f"{timestamp}.json"
    )
    log_path = output_dir / (
        "lvtopsun-lv48100-battery2-live-validation-"
        f"{timestamp}.log"
    )

    modemmanager_was_active = service_active()
    original_stdout = sys.stdout
    original_stderr = sys.stderr

    report: dict[str, Any] = {
        "capture_started_at_utc": utc_now(),
        "device_link": str(device_link),
        "resolved_device": str(resolved_device),
        "candidate_addresses": addresses,
        "samples_per_address": args.samples,
        "sample_delay_seconds": args.sample_delay,
        "safety": {
            "read_only": True,
            "commands": [
                "0x42 read analogue information",
                "0x44 read status information",
                "0xC1 read hardware version",
                "0xC2 read serial number",
            ],
            "write_or_control_commands_sent": False,
        },
        "addresses": {},
        "conclusion": None,
    }

    class Tee:
        def __init__(self, *streams: Any) -> None:
            self.streams = streams

        def write(self, data: str) -> int:
            for stream in self.streams:
                stream.write(data)
                stream.flush()
            return len(data)

        def flush(self) -> None:
            for stream in self.streams:
                stream.flush()

    with log_path.open("w", encoding="utf-8") as log_file:
        sys.stdout = Tee(original_stdout, log_file)
        sys.stderr = Tee(original_stderr, log_file)

        try:
            print()
            print(
                "### LVTOPSUN LV48100 battery 2 "
                "live-telemetry validation"
            )
            print(f"time UTC: {utc_now()}")
            print(f"device: {device_link}")
            print(f"addresses: {addresses}")
            print(f"samples per address: {args.samples}")
            print(f"log: {log_path}")

            print()
            print("### operator precondition")
            print(
                "The blue RS232 service cable must be connected "
                "directly to battery 2."
            )
            print(
                "Battery 2 should show its normal powered or active "
                "front-panel indication before continuing."
            )
            print(
                "Do not change DIP switches, power cables, the grey "
                "inverter cable, or the black inter-battery cable."
            )

            confirmation = input(
                "Type BATTERY2-ACTIVE to continue: "
            ).strip()

            if confirmation != "BATTERY2-ACTIVE":
                raise PaceError(
                    "operator did not confirm battery 2 active state"
                )

            if modemmanager_was_active:
                print()
                print("### temporarily stop ModemManager")
                run(["sudo", "-v"], check=True)
                run(
                    [
                        "sudo",
                        "systemctl",
                        "stop",
                        "ModemManager.service",
                    ],
                    check=True,
                )
                time.sleep(1)

                if service_active():
                    raise PaceError(
                        "ModemManager remained active"
                    )

                print(
                    "PASS: ModemManager stopped temporarily"
                )

            if serial_busy(str(resolved_device)):
                raise PaceError(
                    f"another process owns {resolved_device}"
                )

            any_plausible = False

            for address in addresses:
                address_key = str(address)
                address_report: dict[str, Any] = {
                    "address": address,
                    "identity": {},
                    "samples": [],
                    "transport_errors": [],
                }
                report["addresses"][address_key] = address_report

                print()
                print(
                    f"### probe direct PACE address {address}"
                )

                try:
                    port = serial.Serial(
                        port=str(device_link),
                        baudrate=9600,
                        bytesize=serial.EIGHTBITS,
                        parity=serial.PARITY_NONE,
                        stopbits=serial.STOPBITS_ONE,
                        timeout=3.0,
                        write_timeout=3.0,
                        xonxoff=False,
                        rtscts=False,
                        dsrdtr=False,
                        exclusive=True,
                    )
                    port.dtr = False
                    port.rts = False

                    try:
                        hardware = query(
                            port,
                            address=address,
                            cid2=CID2_READ_HARDWARE_VERSION,
                            payload=b"",
                        )
                        serial_number = query(
                            port,
                            address=address,
                            cid2=CID2_READ_SERIAL_NUMBER,
                            payload=b"",
                        )

                        address_report["identity"] = {
                            "hardware_version": decode_identity(
                                hardware["payload_bytes"]
                            ),
                            "serial_number": decode_identity(
                                serial_number["payload_bytes"]
                            ),
                            "hardware_query": serialisable(hardware),
                            "serial_query": serialisable(
                                serial_number
                            ),
                        }

                        print(
                            "identity: "
                            f"{address_report['identity']['hardware_version']}"
                        )
                        print(
                            "serial: "
                            f"{address_report['identity']['serial_number']}"
                        )

                        for sample_number in range(
                            1,
                            args.samples + 1,
                        ):
                            print(
                                f"sample {sample_number} of "
                                f"{args.samples}"
                            )

                            analog_query = query(
                                port,
                                address=address,
                                cid2=(
                                    CID2_READ_ANALOG_INFORMATION
                                ),
                                payload=bytes([address]),
                            )
                            status_query = query(
                                port,
                                address=address,
                                cid2=(
                                    CID2_READ_STATUS_INFORMATION
                                ),
                                payload=bytes([address]),
                            )

                            analog = decode_analog(
                                analog_query["payload_bytes"],
                                expected_address=address,
                            )
                            status = decode_status(
                                status_query["payload_bytes"],
                                expected_address=address,
                            )
                            assessment = semantic_assessment(
                                analog,
                                status,
                            )

                            sample = {
                                "sample_number": sample_number,
                                "captured_at_utc": utc_now(),
                                "analog": analog,
                                "status": status,
                                "semantic_assessment": assessment,
                                "analog_query": serialisable(
                                    analog_query
                                ),
                                "status_query": serialisable(
                                    status_query
                                ),
                            }
                            address_report["samples"].append(sample)

                            print(
                                f"  pack voltage: "
                                f"{analog['pack_voltage_v']} V"
                            )
                            print(
                                f"  nonzero cells: "
                                f"{assessment['nonzero_cell_count']}"
                                f"/{assessment['reported_cell_count']}"
                            )
                            print(
                                f"  cycle count: "
                                f"{analog['cycle_count']}"
                            )
                            print(
                                f"  plausible live telemetry: "
                                f"{assessment['plausible_live_telemetry']}"
                            )

                            if assessment[
                                "plausible_live_telemetry"
                            ]:
                                any_plausible = True
                                print(
                                    "PASS: plausible live battery "
                                    "telemetry received"
                                )
                            else:
                                print(
                                    "REJECTED: "
                                    + "; ".join(
                                        assessment[
                                            "rejection_reasons"
                                        ]
                                    )
                                )

                            if sample_number < args.samples:
                                time.sleep(args.sample_delay)

                    finally:
                        port.close()

                except Exception as exc:
                    address_report["transport_errors"].append(
                        {
                            "error": str(exc),
                            "type": type(exc).__name__,
                        }
                    )
                    print(
                        f"NOTICE: address {address} did not "
                        f"complete: {exc}",
                        file=sys.stderr,
                    )

            report["capture_completed_at_utc"] = utc_now()
            report["plausible_live_telemetry_observed"] = (
                any_plausible
            )

            if any_plausible:
                report["conclusion"] = (
                    "At least one direct address returned plausible "
                    "live battery 2 telemetry suitable for report use."
                )
                result_status = 0
                print()
                print(
                    "PASS: battery 2 live telemetry was validated"
                )
            else:
                report["conclusion"] = (
                    "Battery 2 responded to PACE queries, but no "
                    "sample passed semantic plausibility checks. "
                    "Zero-filled telemetry must not be reported as "
                    "real voltage, capacity, temperature, SOC, SOH, "
                    "or cycle count."
                )
                result_status = 2
                print()
                print(
                    "FAIL: no plausible live battery 2 telemetry "
                    "was observed",
                    file=sys.stderr,
                )

        except Exception as exc:
            report["capture_completed_at_utc"] = utc_now()
            report["plausible_live_telemetry_observed"] = False
            report["conclusion"] = (
                f"Validation did not complete: {exc}"
            )
            result_status = 3
            print(f"FAIL: {exc}", file=sys.stderr)

        finally:
            checksum_path = write_evidence(
                json_path,
                report,
            )

            print()
            print("### evidence")
            print(f"JSON: {json_path}")
            print(f"SHA-256: {checksum_path}")
            print(f"log: {log_path}")

            if modemmanager_was_active:
                print()
                print("### restore ModemManager")
                try:
                    run(
                        [
                            "sudo",
                            "systemctl",
                            "start",
                            "ModemManager.service",
                        ],
                        check=True,
                    )
                    time.sleep(1)

                    if service_active():
                        print(
                            "PASS: ModemManager is active"
                        )
                    else:
                        print(
                            "WARNING: ModemManager is not active",
                            file=sys.stderr,
                        )
                except Exception as exc:
                    print(
                        f"WARNING: failed to restore "
                        f"ModemManager: {exc}",
                        file=sys.stderr,
                    )

            print()
            print("### safety statement")
            print(
                "Only PACE read commands 0x42, 0x44, 0xC1, "
                "and 0xC2 were used."
            )
            print(
                "No configuration, calibration, MOSFET control, "
                "shutdown, addressing, clock-setting, or firmware "
                "command was sent."
            )

            sys.stdout = original_stdout
            sys.stderr = original_stderr

    return result_status


if __name__ == "__main__":
    raise SystemExit(main())
