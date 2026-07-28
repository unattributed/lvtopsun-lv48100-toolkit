#!/usr/bin/env python3
"""Read-only discovery and telemetry query for a second LV48100 pack."""

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

CID2_READ_BMS_COUNT = 0x90
CID2_READ_ANALOG_INFORMATION = 0x42
CID2_READ_STATUS_INFORMATION = 0x44

DEFAULT_SERIAL_LINK = os.environ.get(
    "LV48100_DEVICE",
    "/dev/ttyUSB0",
)
DEFAULT_MASTER_ADDRESS = 1
DEFAULT_TARGET_ADDRESS = 2
DEFAULT_OUTPUT_DIR = os.environ.get(
    "LV48100_OUTPUT_DIR",
    "reports",
)


class PaceError(RuntimeError):
    """PACE transport, framing, or payload validation error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    length_checksum = (-nibble_sum) & 0x0F
    return (length_checksum << 12) | length_id


def calculate_frame_checksum(frame_body: bytes) -> int:
    return (-sum(frame_body)) & 0xFFFF


def build_request(
    header_address: int,
    cid2: int,
    payload: bytes = b"",
) -> bytes:
    info_ascii = payload.hex().upper()
    length_field = calculate_length_field(len(info_ascii))

    frame_body = (
        f"{PROTOCOL_VERSION:02X}"
        f"{header_address:02X}"
        f"{CID1_LITHIUM_IRON:02X}"
        f"{cid2:02X}"
        f"{length_field:04X}"
        f"{info_ascii}"
    ).encode("ascii")

    checksum = calculate_frame_checksum(frame_body)

    return (
        b"~"
        + frame_body
        + f"{checksum:04X}".encode("ascii")
        + b"\r"
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
            f"response is too short for a valid PACE frame: "
            f"{len(body)} bytes"
        )

    try:
        text = body.decode("ascii")
    except UnicodeDecodeError as exc:
        raise PaceError(
            "response contains non-ASCII frame data"
        ) from exc

    try:
        version = int(text[0:2], 16)
        address = int(text[2:4], 16)
        cid1 = int(text[4:6], 16)
        return_code = int(text[6:8], 16)
        length_field = int(text[8:12], 16)
        received_checksum = int(text[-4:], 16)
    except ValueError as exc:
        raise PaceError(
            "response header contains invalid hexadecimal ASCII"
        ) from exc

    info_ascii = text[12:-4]

    calculated_checksum = calculate_frame_checksum(body[:-4])

    if received_checksum != calculated_checksum:
        raise PaceError(
            "PACE frame checksum mismatch, "
            f"received {received_checksum:04X}, "
            f"calculated {calculated_checksum:04X}"
        )

    declared_info_ascii_length = length_field & 0x0FFF

    if len(info_ascii) != declared_info_ascii_length:
        raise PaceError(
            "PACE INFO length mismatch, "
            f"declared {declared_info_ascii_length}, "
            f"received {len(info_ascii)}"
        )

    expected_length_field = calculate_length_field(
        declared_info_ascii_length
    )

    if length_field != expected_length_field:
        raise PaceError(
            "PACE LENGTH checksum mismatch, "
            f"received {length_field:04X}, "
            f"calculated {expected_length_field:04X}"
        )

    if len(info_ascii) % 2:
        raise PaceError(
            "PACE INFO field has an odd number of hexadecimal characters"
        )

    try:
        payload = bytes.fromhex(info_ascii)
    except ValueError as exc:
        raise PaceError(
            "PACE INFO field contains invalid hexadecimal ASCII"
        ) from exc

    if return_code != 0:
        raise PaceError(
            f"BMS returned non-zero status RTN={return_code:02X}"
        )

    return {
        "version": version,
        "version_hex": f"{version:02X}",
        "header_address": address,
        "header_address_hex": f"{address:02X}",
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
        "response_byte_length": len(response),
    }


def signed_int16(value: bytes) -> int:
    if len(value) != 2:
        raise PaceError(
            f"expected two bytes for signed int16, got {len(value)}"
        )
    return int.from_bytes(value, byteorder="big", signed=True)


def unsigned_int16(value: bytes) -> int:
    if len(value) != 2:
        raise PaceError(
            f"expected two bytes for unsigned int16, got {len(value)}"
        )
    return int.from_bytes(value, byteorder="big", signed=False)


def decode_analog_payload(
    payload: bytes,
    expected_target_address: int,
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

        value = unsigned_int16(payload[offset:end])
        offset = end
        return value

    def take_i16(label: str) -> int:
        nonlocal offset

        end = offset + 2

        if end > len(payload):
            raise PaceError(
                f"analogue payload ended while reading {label}"
            )

        value = signed_int16(payload[offset:end])
        offset = end
        return value

    leading_protocol_byte = take_byte("leading protocol byte")
    responding_pack_address = take_byte("responding pack address")

    if responding_pack_address != expected_target_address:
        raise PaceError(
            "analogue payload was not returned for the requested target, "
            f"expected {expected_target_address}, "
            f"received {responding_pack_address}"
        )

    cell_count = take_byte("cell count")

    if not 1 <= cell_count <= 32:
        raise PaceError(
            f"analogue payload reports unreasonable cell count: "
            f"{cell_count}"
        )

    cell_voltages_mv = [
        take_u16(f"cell {cell_number} voltage")
        for cell_number in range(1, cell_count + 1)
    ]

    temperature_count = take_byte("temperature count")

    if not 0 <= temperature_count <= 16:
        raise PaceError(
            "analogue payload reports unreasonable temperature count: "
            f"{temperature_count}"
        )

    temperature_raw = [
        take_u16(f"temperature sensor {sensor_number}")
        for sensor_number in range(1, temperature_count + 1)
    ]

    current_raw = take_i16("pack current")
    total_voltage_mv = take_u16("total voltage")
    remaining_capacity_raw = take_u16("remaining capacity")
    user_defined_value = take_byte("user-defined protocol value")
    full_capacity_raw = take_u16("full capacity")
    cycle_count = take_u16("cycle count")
    design_capacity_raw = take_u16("design capacity")

    trailing_vendor_data = payload[offset:]

    temperatures_c = [
        round((raw_value - 2730) / 10.0, 1)
        for raw_value in temperature_raw
    ]

    current_a = round(current_raw / 100.0, 2)
    total_voltage_v = round(total_voltage_mv / 1000.0, 3)
    remaining_capacity_ah = round(
        remaining_capacity_raw / 100.0,
        2,
    )
    full_capacity_ah = round(
        full_capacity_raw / 100.0,
        2,
    )
    design_capacity_ah = round(
        design_capacity_raw / 100.0,
        2,
    )

    soc_percent = None
    soh_percent = None

    if full_capacity_raw:
        soc_percent = round(
            remaining_capacity_raw
            / full_capacity_raw
            * 100.0,
            2,
        )

    if design_capacity_raw:
        soh_percent = round(
            full_capacity_raw
            / design_capacity_raw
            * 100.0,
            2,
        )

    minimum_cell_mv = min(cell_voltages_mv)
    maximum_cell_mv = max(cell_voltages_mv)
    average_cell_mv = round(
        sum(cell_voltages_mv) / len(cell_voltages_mv),
        2,
    )
    differential_mv = maximum_cell_mv - minimum_cell_mv
    cell_sum_mv = sum(cell_voltages_mv)

    return {
        "leading_protocol_byte_hex": (
            f"{leading_protocol_byte:02X}"
        ),
        "responding_pack_address": responding_pack_address,
        "cell_count": cell_count,
        "cell_voltages_mv": cell_voltages_mv,
        "cell_voltages_v": [
            round(value / 1000.0, 3)
            for value in cell_voltages_mv
        ],
        "cell_summary": {
            "minimum_mv": minimum_cell_mv,
            "maximum_mv": maximum_cell_mv,
            "average_mv": average_cell_mv,
            "differential_mv": differential_mv,
            "sum_mv": cell_sum_mv,
            "sum_v": round(cell_sum_mv / 1000.0, 3),
        },
        "temperature_count": temperature_count,
        "temperature_raw": temperature_raw,
        "temperatures_c": temperatures_c,
        "current_raw": current_raw,
        "current_a": current_a,
        "power_w": round(total_voltage_v * current_a, 2),
        "operating_direction": (
            "charging"
            if current_raw > 0
            else "discharging"
            if current_raw < 0
            else "idle"
        ),
        "total_voltage_mv": total_voltage_mv,
        "total_voltage_v": total_voltage_v,
        "remaining_capacity_raw": remaining_capacity_raw,
        "remaining_capacity_ah": remaining_capacity_ah,
        "user_defined_protocol_value": user_defined_value,
        "user_defined_protocol_value_hex": (
            f"{user_defined_value:02X}"
        ),
        "full_capacity_raw": full_capacity_raw,
        "full_capacity_ah": full_capacity_ah,
        "cycle_count": cycle_count,
        "design_capacity_raw": design_capacity_raw,
        "design_capacity_ah": design_capacity_ah,
        "calculated_soc_percent": soc_percent,
        "calculated_soh_percent": soh_percent,
        "vendor_trailing_data": {
            "byte_length": len(trailing_vendor_data),
            "hex": trailing_vendor_data.hex(" ").upper(),
        },
    }


def decode_limit_value(value: int) -> str:
    if value == 0:
        return "normal"
    if value == 1:
        return "below lower limit"
    if value == 2:
        return "above upper limit"
    if 0x80 <= value <= 0xEF:
        return f"user-defined fault 0x{value:02X}"
    if value == 0xF0:
        return "other fault"
    return f"unknown value 0x{value:02X}"


def decode_flags(
    value: int,
    definitions: tuple[tuple[int, str], ...],
) -> list[str]:
    return [
        description
        for bit_mask, description in definitions
        if value & bit_mask
    ]


PROTECTION_1_FLAGS = (
    (0x80, "charger high-voltage input protection"),
    (0x40, "short-circuit protection"),
    (0x20, "discharge over-current protection"),
    (0x10, "charge over-current protection"),
    (0x08, "low total-voltage protection"),
    (0x04, "high total-voltage protection"),
    (0x02, "low cell-voltage protection"),
    (0x01, "high cell-voltage protection"),
)

PROTECTION_2_FLAGS = (
    (0x80, "fully protected state"),
    (0x40, "low environmental-temperature protection"),
    (0x20, "high environmental-temperature protection"),
    (0x10, "high MOSFET-temperature protection"),
    (0x08, "low discharge-temperature protection"),
    (0x04, "low charge-temperature protection"),
    (0x02, "high discharge-temperature protection"),
    (0x01, "high charge-temperature protection"),
)

SYSTEM_FLAGS = (
    (0x80, "heater active"),
    (0x40, "alternate current input"),
    (0x20, "charging"),
    (0x10, "terminals reversed"),
    (0x08, "discharging"),
    (0x04, "discharge MOSFET on"),
    (0x02, "charge MOSFET on"),
    (0x01, "charge-current limiter off"),
)

CONFIGURATION_FLAGS = (
    (0x80, "undefined configuration bit 7"),
    (0x40, "static balancing enabled"),
    (0x20, "warning LED enabled"),
    (0x10, "charge-current limiter enabled"),
    (0x08, "charge-current limiter low gear"),
    (0x04, "discharge MOSFET commanded off"),
    (0x02, "charge MOSFET commanded off"),
    (0x01, "warning buzzer enabled"),
)

FAULT_FLAGS = (
    (0x80, "heater fault"),
    (0x40, "CCB fault"),
    (0x20, "VCC sampling fault"),
    (0x10, "cell fault"),
    (0x08, "communication fault"),
    (0x04, "NTC fault"),
    (0x02, "discharge MOSFET fault"),
    (0x01, "charge MOSFET fault"),
)

WARNING_1_FLAGS = (
    (0x80, "undefined warning-status-1 bit 7"),
    (0x40, "undefined warning-status-1 bit 6"),
    (0x20, "discharge-current warning"),
    (0x10, "charge-current warning"),
    (0x08, "low total-voltage warning"),
    (0x04, "high total-voltage warning"),
    (0x02, "low cell-voltage warning"),
    (0x01, "high cell-voltage warning"),
)

WARNING_2_FLAGS = (
    (0x80, "low-power warning"),
    (0x40, "high MOSFET-temperature warning"),
    (0x20, "low environmental-temperature warning"),
    (0x10, "high environmental-temperature warning"),
    (0x08, "low discharge-temperature warning"),
    (0x04, "low charge-temperature warning"),
    (0x02, "high discharge-temperature warning"),
    (0x01, "high charge-temperature warning"),
)


def decode_status_payload(
    payload: bytes,
    expected_target_address: int,
) -> dict[str, Any]:
    offset = 0

    def take_byte(label: str) -> int:
        nonlocal offset

        if offset >= len(payload):
            raise PaceError(
                f"status payload ended while reading {label}"
            )

        value = payload[offset]
        offset += 1
        return value

    leading_protocol_byte = take_byte("leading protocol byte")
    responding_pack_address = take_byte("responding pack address")

    if responding_pack_address != expected_target_address:
        raise PaceError(
            "status payload was not returned for the requested target, "
            f"expected {expected_target_address}, "
            f"received {responding_pack_address}"
        )

    cell_count = take_byte("cell-warning count")

    if not 1 <= cell_count <= 32:
        raise PaceError(
            f"status payload reports unreasonable cell count: "
            f"{cell_count}"
        )

    cell_warnings = []

    for cell_number in range(1, cell_count + 1):
        raw_value = take_byte(
            f"cell {cell_number} warning"
        )
        cell_warnings.append(
            {
                "cell": cell_number,
                "raw_hex": f"{raw_value:02X}",
                "state": decode_limit_value(raw_value),
            }
        )

    temperature_count = take_byte(
        "temperature-warning count"
    )

    if not 0 <= temperature_count <= 16:
        raise PaceError(
            "status payload reports unreasonable temperature count: "
            f"{temperature_count}"
        )

    temperature_warnings = []

    for sensor_number in range(1, temperature_count + 1):
        raw_value = take_byte(
            f"temperature sensor {sensor_number} warning"
        )
        temperature_warnings.append(
            {
                "sensor": sensor_number,
                "raw_hex": f"{raw_value:02X}",
                "state": decode_limit_value(raw_value),
            }
        )

    charge_current_warning = take_byte(
        "charge-current warning"
    )
    total_voltage_warning = take_byte(
        "total-voltage warning"
    )
    discharge_current_warning = take_byte(
        "discharge-current warning"
    )

    protection_1 = take_byte("protection status 1")
    protection_2 = take_byte("protection status 2")
    system_status = take_byte("system status")
    configuration_status = take_byte(
        "configuration status"
    )
    fault_status = take_byte("fault status")

    balance_high = take_byte("balance status high byte")
    balance_low = take_byte("balance status low byte")
    balance_value = (balance_high << 8) | balance_low

    warning_1 = take_byte("warning status 1")
    warning_2 = take_byte("warning status 2")

    trailing_vendor_data = payload[offset:]

    active_cell_warnings = [
        item
        for item in cell_warnings
        if item["raw_hex"] != "00"
    ]

    active_temperature_warnings = [
        item
        for item in temperature_warnings
        if item["raw_hex"] != "00"
    ]

    scalar_warnings = {
        "charge_current": {
            "raw_hex": f"{charge_current_warning:02X}",
            "state": decode_limit_value(
                charge_current_warning
            ),
        },
        "total_voltage": {
            "raw_hex": f"{total_voltage_warning:02X}",
            "state": decode_limit_value(
                total_voltage_warning
            ),
        },
        "discharge_current": {
            "raw_hex": f"{discharge_current_warning:02X}",
            "state": decode_limit_value(
                discharge_current_warning
            ),
        },
    }

    protection_1_active = decode_flags(
        protection_1,
        PROTECTION_1_FLAGS,
    )
    protection_2_active = decode_flags(
        protection_2,
        PROTECTION_2_FLAGS,
    )
    system_active = decode_flags(
        system_status,
        SYSTEM_FLAGS,
    )
    configuration_active = decode_flags(
        configuration_status,
        CONFIGURATION_FLAGS,
    )
    fault_active = decode_flags(
        fault_status,
        FAULT_FLAGS,
    )
    warning_1_active = decode_flags(
        warning_1,
        WARNING_1_FLAGS,
    )
    warning_2_active = decode_flags(
        warning_2,
        WARNING_2_FLAGS,
    )

    balancing_cells = [
        cell_number
        for cell_number in range(1, cell_count + 1)
        if balance_value & (1 << (cell_number - 1))
    ]

    warnings_clear = not (
        active_cell_warnings
        or active_temperature_warnings
        or any(
            item["raw_hex"] != "00"
            for item in scalar_warnings.values()
        )
        or warning_1_active
        or warning_2_active
    )

    protections_clear = not (
        protection_1_active
        or protection_2_active
    )

    faults_clear = not fault_active

    return {
        "leading_protocol_byte_hex": (
            f"{leading_protocol_byte:02X}"
        ),
        "responding_pack_address": responding_pack_address,
        "cell_count": cell_count,
        "cell_warnings": cell_warnings,
        "active_cell_warnings": active_cell_warnings,
        "temperature_count": temperature_count,
        "temperature_warnings": temperature_warnings,
        "active_temperature_warnings": (
            active_temperature_warnings
        ),
        "scalar_warnings": scalar_warnings,
        "protection_status_1": {
            "raw_hex": f"{protection_1:02X}",
            "active": protection_1_active,
        },
        "protection_status_2": {
            "raw_hex": f"{protection_2:02X}",
            "active": protection_2_active,
        },
        "system_status": {
            "raw_hex": f"{system_status:02X}",
            "active": system_active,
            "charging": bool(system_status & 0x20),
            "discharging": bool(system_status & 0x08),
            "charge_mosfet_on": bool(
                system_status & 0x02
            ),
            "discharge_mosfet_on": bool(
                system_status & 0x04
            ),
        },
        "configuration_status": {
            "raw_hex": f"{configuration_status:02X}",
            "active": configuration_active,
        },
        "fault_status": {
            "raw_hex": f"{fault_status:02X}",
            "active": fault_active,
        },
        "balancing": {
            "raw_hex": f"{balance_value:04X}",
            "active_cells": balancing_cells,
        },
        "warning_status_1": {
            "raw_hex": f"{warning_1:02X}",
            "active": warning_1_active,
        },
        "warning_status_2": {
            "raw_hex": f"{warning_2:02X}",
            "active": warning_2_active,
        },
        "vendor_trailing_data": {
            "byte_length": len(trailing_vendor_data),
            "hex": trailing_vendor_data.hex(" ").upper(),
        },
        "summary": {
            "warnings_clear": warnings_clear,
            "protections_clear": protections_clear,
            "faults_clear": faults_clear,
            "balancing_active": bool(
                balancing_cells
            ),
            "all_clear": (
                warnings_clear
                and protections_clear
                and faults_clear
            ),
        },
    }


def run_command(
    command: list[str],
    *,
    check: bool = False,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        text=True,
        capture_output=capture_output,
    )


def service_is_active(service_name: str) -> bool:
    result = run_command(
        [
            "systemctl",
            "is-active",
            "--quiet",
            service_name,
        ]
    )
    return result.returncode == 0


def serial_device_is_busy(device: str) -> bool:
    result = run_command(
        [
            "sudo",
            "fuser",
            device,
        ],
        capture_output=True,
    )
    return result.returncode == 0


def send_query(
    port: serial.Serial,
    *,
    name: str,
    header_address: int,
    cid2: int,
    payload: bytes,
) -> dict[str, Any]:
    request = build_request(
        header_address=header_address,
        cid2=cid2,
        payload=payload,
    )

    port.reset_input_buffer()
    port.reset_output_buffer()

    written = port.write(request)
    port.flush()

    if written != len(request):
        raise PaceError(
            f"{name}: wrote {written} of "
            f"{len(request)} request bytes"
        )

    response = port.read_until(
        expected=b"\r",
        size=4096,
    )

    parsed = parse_response(response)

    if parsed["version"] != PROTOCOL_VERSION:
        raise PaceError(
            f"{name}: unexpected protocol version "
            f"{parsed['version_hex']}"
        )

    if parsed["cid1"] != CID1_LITHIUM_IRON:
        raise PaceError(
            f"{name}: unexpected CID1 "
            f"{parsed['cid1_hex']}"
        )

    payload_bytes = parsed.pop("payload")

    result = {
        "name": name,
        "cid2_hex": f"{cid2:02X}",
        "request": {
            "ascii": request.decode("ascii").replace(
                "\r",
                "\\r",
            ),
            "hex": request.hex(" ").upper(),
            "byte_length": len(request),
        },
        "response": parsed,
        "payload_bytes": payload_bytes,
    }

    time.sleep(0.25)

    return result


def serialise_query_result(
    query_result: dict[str, Any],
) -> dict[str, Any]:
    result = dict(query_result)
    payload_bytes = result.pop("payload_bytes")
    result["payload_hex"] = payload_bytes.hex(" ").upper()
    result["payload_byte_length"] = len(payload_bytes)
    return result


def write_json_and_checksum(
    output_path: Path,
    report: dict[str, Any],
) -> tuple[Path, Path]:
    output_path.write_text(
        json.dumps(
            report,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    digest = hashlib.sha256(
        output_path.read_bytes()
    ).hexdigest()

    checksum_path = Path(
        f"{output_path}.sha256"
    )

    checksum_path.write_text(
        f"{digest}  {output_path.name}\n",
        encoding="utf-8",
    )

    return output_path, checksum_path


def verify_exact_request_generation(
    master_address: int,
    target_address: int,
) -> None:
    if master_address != 1 or target_address != 2:
        return

    expected = {
        "bms_count": b"~250146900000FDA5\r",
        "target_analog": b"~25014642E00202FD2F\r",
        "target_status": b"~25014644E00202FD2D\r",
    }

    actual = {
        "bms_count": build_request(
            master_address,
            CID2_READ_BMS_COUNT,
            b"",
        ),
        "target_analog": build_request(
            master_address,
            CID2_READ_ANALOG_INFORMATION,
            bytes([target_address]),
        ),
        "target_status": build_request(
            master_address,
            CID2_READ_STATUS_INFORMATION,
            bytes([target_address]),
        ),
    }

    for name, expected_frame in expected.items():
        if actual[name] != expected_frame:
            raise PaceError(
                f"request-generation self-test failed for {name}, "
                f"actual={actual[name]!r}, "
                f"expected={expected_frame!r}"
            )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Query a directly connected LVTOPSUN LV48100 master "
            "for its reported BMS count, then read analogue and "
            "status information for a target slave battery."
        )
    )

    parser.add_argument(
        "--device",
        default=DEFAULT_SERIAL_LINK,
        help=(
            "Persistent serial device path. "
            f"Default: {DEFAULT_SERIAL_LINK}"
        ),
    )
    parser.add_argument(
        "--master-address",
        type=int,
        default=DEFAULT_MASTER_ADDRESS,
        help="Directly connected PACE master address. Default: 1",
    )
    parser.add_argument(
        "--target-address",
        type=int,
        default=DEFAULT_TARGET_ADDRESS,
        help="Target slave battery address. Default: 2",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "Evidence output directory. "
            f"Default: {DEFAULT_OUTPUT_DIR}"
        ),
    )

    return parser.parse_args()


def main() -> int:
    args = parse_arguments()

    if not 1 <= args.master_address <= 15:
        print(
            "FAIL: master address must be from 1 through 15",
            file=sys.stderr,
        )
        return 10

    if not 1 <= args.target_address <= 15:
        print(
            "FAIL: target address must be from 1 through 15",
            file=sys.stderr,
        )
        return 11

    if args.target_address == args.master_address:
        print(
            "FAIL: target address must differ from master address",
            file=sys.stderr,
        )
        return 12

    try:
        verify_exact_request_generation(
            args.master_address,
            args.target_address,
        )
    except Exception as exc:
        print(
            f"FAIL: request self-test failed: {exc}",
            file=sys.stderr,
        )
        return 13

    device_link = Path(args.device)

    if not device_link.exists():
        print(
            f"FAIL: serial device does not exist: "
            f"{device_link}",
            file=sys.stderr,
        )
        return 20

    try:
        resolved_device = device_link.resolve(strict=True)
    except FileNotFoundError:
        print(
            f"FAIL: serial device path cannot be resolved: "
            f"{device_link}",
            file=sys.stderr,
        )
        return 21

    if not resolved_device.exists():
        print(
            f"FAIL: resolved serial device does not exist: "
            f"{resolved_device}",
            file=sys.stderr,
        )
        return 22

    if not os.access(resolved_device, os.R_OK | os.W_OK):
        print(
            f"FAIL: current user cannot read and write "
            f"{resolved_device}",
            file=sys.stderr,
        )
        return 23

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y%m%dT%H%M%SZ")

    output_path = output_dir / (
        "lvtopsun-lv48100-pack"
        f"{args.target_address:02d}"
        "-relay-discovery-"
        f"{timestamp}.json"
    )

    log_path = output_dir / (
        "lvtopsun-lv48100-pack"
        f"{args.target_address:02d}"
        "-relay-discovery-"
        f"{timestamp}.log"
    )

    modemmanager_was_active = service_is_active(
        "ModemManager.service"
    )

    report: dict[str, Any] = {
        "capture_started_at_utc": utc_now(),
        "device_link": str(device_link),
        "resolved_device": str(resolved_device),
        "master_address": args.master_address,
        "target_address": args.target_address,
        "assumptions_under_test": [
            (
                "The directly connected battery at address 1 is "
                "the PACE master."
            ),
            (
                "The master can see at least two BMS packs over "
                "the inter-battery communication link."
            ),
            (
                "The second battery is reachable as target "
                "address 2."
            ),
            (
                "Analogue command 0x42 and status command 0x44 "
                "can be relayed through the master."
            ),
        ],
        "safety": {
            "read_only": True,
            "commands": [
                "0x90 read BMS count",
                "0x42 read analogue information for target pack",
                "0x44 read status information for target pack",
            ],
            "write_or_control_commands_sent": False,
        },
        "transport": {
            "interface": "RS232 to directly connected master",
            "baudrate": 9600,
            "data_bits": 8,
            "parity": "none",
            "stop_bits": 1,
        },
        "queries": {},
        "assumption_results": {},
        "failures": [],
    }

    original_stdout = sys.stdout
    original_stderr = sys.stderr

    with log_path.open(
        "w",
        encoding="utf-8",
    ) as log_file:
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

        sys.stdout = Tee(original_stdout, log_file)
        sys.stderr = Tee(original_stderr, log_file)

        try:
            print()
            print(
                "### LVTOPSUN LV48100 second-pack "
                "read-only discovery"
            )
            print(f"time UTC: {utc_now()}")
            print(f"master address: {args.master_address}")
            print(f"target address: {args.target_address}")
            print(f"serial link: {device_link}")
            print(f"resolved device: {resolved_device}")
            print(f"log: {log_path}")

            if modemmanager_was_active:
                print()
                print("### temporarily stop ModemManager")

                run_command(
                    ["sudo", "-v"],
                    check=True,
                )

                run_command(
                    [
                        "sudo",
                        "systemctl",
                        "stop",
                        "ModemManager.service",
                    ],
                    check=True,
                )

                time.sleep(1)

                if service_is_active(
                    "ModemManager.service"
                ):
                    raise PaceError(
                        "ModemManager remained active"
                    )

                print(
                    "PASS: ModemManager stopped temporarily"
                )

            print()
            print("### verify exclusive serial access")

            if serial_device_is_busy(
                str(resolved_device)
            ):
                raise PaceError(
                    f"another process currently owns "
                    f"{resolved_device}"
                )

            print(
                f"PASS: no process currently owns "
                f"{resolved_device}"
            )

            with serial.Serial(
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
            ) as port:
                port.dtr = False
                port.rts = False

                print()
                print(
                    "### query master-reported BMS count, "
                    "CID2=0x90"
                )

                count_result = send_query(
                    port,
                    name="bms_count",
                    header_address=args.master_address,
                    cid2=CID2_READ_BMS_COUNT,
                    payload=b"",
                )

                count_payload = count_result[
                    "payload_bytes"
                ]

                if len(count_payload) < 1:
                    raise PaceError(
                        "BMS count response contained no payload"
                    )

                bms_count = count_payload[0]

                count_serialised = serialise_query_result(
                    count_result
                )
                count_serialised["decoded"] = {
                    "reported_bms_count": bms_count
                }

                report["queries"]["bms_count"] = (
                    count_serialised
                )

                report["assumption_results"][
                    "master_responded_to_count_query"
                ] = True

                report["assumption_results"][
                    "reported_bms_count"
                ] = bms_count

                print(
                    f"PASS: master reports {bms_count} "
                    f"BMS pack(s)"
                )

                if bms_count < args.target_address:
                    report["assumption_results"][
                        "target_pack_reported_present"
                    ] = False

                    raise PaceError(
                        "master-reported pack count is lower than "
                        f"target address {args.target_address}, "
                        "so no target relay query was attempted"
                    )

                report["assumption_results"][
                    "target_pack_reported_present"
                ] = True

                print()
                print(
                    "### query target analogue information, "
                    "CID2=0x42"
                )

                analog_result = send_query(
                    port,
                    name="target_analog_information",
                    header_address=args.master_address,
                    cid2=CID2_READ_ANALOG_INFORMATION,
                    payload=bytes(
                        [args.target_address]
                    ),
                )

                analog_payload = analog_result[
                    "payload_bytes"
                ]

                analog_decoded = decode_analog_payload(
                    analog_payload,
                    expected_target_address=(
                        args.target_address
                    ),
                )

                analog_serialised = serialise_query_result(
                    analog_result
                )
                analog_serialised["decoded"] = (
                    analog_decoded
                )

                report["queries"][
                    "target_analog_information"
                ] = analog_serialised

                report["assumption_results"][
                    "target_analog_query_succeeded"
                ] = True

                print(
                    "PASS: target analogue information "
                    "returned and decoded"
                )
                print(
                    f"target pack voltage: "
                    f"{analog_decoded['total_voltage_v']} V"
                )
                print(
                    f"target current: "
                    f"{analog_decoded['current_a']} A"
                )
                print(
                    f"target cycle count: "
                    f"{analog_decoded['cycle_count']}"
                )
                print(
                    f"target calculated SOC: "
                    f"{analog_decoded['calculated_soc_percent']}%"
                )
                print(
                    f"target calculated SOH: "
                    f"{analog_decoded['calculated_soh_percent']}%"
                )
                print(
                    "target cell differential: "
                    f"{analog_decoded['cell_summary']['differential_mv']} "
                    "mV"
                )

                print()
                print(
                    "### query target status information, "
                    "CID2=0x44"
                )

                status_result = send_query(
                    port,
                    name="target_status_information",
                    header_address=args.master_address,
                    cid2=CID2_READ_STATUS_INFORMATION,
                    payload=bytes(
                        [args.target_address]
                    ),
                )

                status_payload = status_result[
                    "payload_bytes"
                ]

                status_decoded = decode_status_payload(
                    status_payload,
                    expected_target_address=(
                        args.target_address
                    ),
                )

                status_serialised = serialise_query_result(
                    status_result
                )
                status_serialised["decoded"] = (
                    status_decoded
                )

                report["queries"][
                    "target_status_information"
                ] = status_serialised

                report["assumption_results"][
                    "target_status_query_succeeded"
                ] = True

                print(
                    "PASS: target status information "
                    "returned and decoded"
                )
                print(
                    "target warnings clear: "
                    f"{status_decoded['summary']['warnings_clear']}"
                )
                print(
                    "target protections clear: "
                    f"{status_decoded['summary']['protections_clear']}"
                )
                print(
                    "target faults clear: "
                    f"{status_decoded['summary']['faults_clear']}"
                )
                print(
                    "target balancing cells: "
                    f"{status_decoded['balancing']['active_cells']}"
                )

            report["complete_success"] = True
            report["conclusion"] = (
                "Battery 2 is reachable through the directly "
                "connected PACE master using read-only relay "
                "queries."
            )

            print()
            print(
                "PASS: battery 2 is readable through "
                "battery 1"
            )

        except Exception as exc:
            report["complete_success"] = False
            report["failures"].append(
                {
                    "error": str(exc),
                    "type": type(exc).__name__,
                }
            )
            report["conclusion"] = (
                "The current test did not prove that battery 2 "
                "is readable through battery 1. Review the "
                "master-reported count, target addressing, "
                "inter-battery cable, and DIP-switch assignment."
            )

            print()
            print(
                f"FAIL: {exc}",
                file=sys.stderr,
            )

        finally:
            report["capture_completed_at_utc"] = utc_now()

            try:
                output_file, checksum_file = (
                    write_json_and_checksum(
                        output_path,
                        report,
                    )
                )

                print()
                print("### evidence")
                print(f"JSON: {output_file}")
                print(f"SHA-256: {checksum_file}")
                print(f"log: {log_path}")

            except Exception as evidence_exc:
                print(
                    f"WARNING: evidence write failed: "
                    f"{evidence_exc}",
                    file=sys.stderr,
                )

            if modemmanager_was_active:
                print()
                print("### restore ModemManager")

                try:
                    run_command(
                        [
                            "sudo",
                            "systemctl",
                            "start",
                            "ModemManager.service",
                        ],
                        check=True,
                    )

                    time.sleep(1)

                    if service_is_active(
                        "ModemManager.service"
                    ):
                        print(
                            "PASS: ModemManager is active"
                        )
                    else:
                        print(
                            "WARNING: ModemManager did not "
                            "return to active state",
                            file=sys.stderr,
                        )

                except Exception as restore_exc:
                    print(
                        f"WARNING: failed to restore "
                        f"ModemManager: {restore_exc}",
                        file=sys.stderr,
                    )

            print()
            print("### safety statement")
            print("Attempted commands:")
            print("  0x90, read BMS count")
            print(
                "  0x42, read analogue information "
                f"for target pack {args.target_address}"
            )
            print(
                "  0x44, read status information "
                f"for target pack {args.target_address}"
            )
            print(
                "No configuration, MOSFET control, shutdown, "
                "date-setting, addressing, DIP-switch, or "
                "firmware command was sent."
            )

            sys.stdout = original_stdout
            sys.stderr = original_stderr

    return 0 if report.get("complete_success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
