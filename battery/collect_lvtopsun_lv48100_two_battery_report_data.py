#!/usr/bin/env python3
"""Guided read-only collection and reporting for two LV48100 batteries.

The operator is prompted to move one RS232 service cable between packs.
The collector writes combined JSON, Markdown, logs, and SHA-256 sidecars.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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
CID2_READ_HARDWARE_VERSION = 0xC1
CID2_READ_SERIAL_NUMBER = 0xC2

DEFAULT_SERIAL_LINK = os.environ.get(
    "LV48100_DEVICE",
    "/dev/ttyUSB0",
)
DEFAULT_OUTPUT_DIR = os.environ.get(
    "LV48100_OUTPUT_DIR",
    "reports",
)


class PaceError(RuntimeError):
    """PACE transport, framing, or payload validation error."""


class OperatorAbort(RuntimeError):
    """Operator intentionally stopped the guided workflow."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_filename_timestamp() -> str:
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


def verify_request_generation() -> None:
    expected = {
        "count_address_1": b"~250146900000FDA5\r",
        "analog_address_1": b"~25014642E00201FD30\r",
        "status_address_1": b"~25014644E00201FD2E\r",
        "hardware_address_1": b"~250146C10000FD9A\r",
        "serial_address_1": b"~250146C20000FD99\r",
        "analog_address_2": b"~25024642E00202FD2E\r",
        "status_address_2": b"~25024644E00202FD2C\r",
        "hardware_address_2": b"~250246C10000FD99\r",
        "serial_address_2": b"~250246C20000FD98\r",
    }

    actual = {
        "count_address_1": build_request(1, CID2_READ_BMS_COUNT),
        "analog_address_1": build_request(
            1,
            CID2_READ_ANALOG_INFORMATION,
            b"\x01",
        ),
        "status_address_1": build_request(
            1,
            CID2_READ_STATUS_INFORMATION,
            b"\x01",
        ),
        "hardware_address_1": build_request(
            1,
            CID2_READ_HARDWARE_VERSION,
        ),
        "serial_address_1": build_request(
            1,
            CID2_READ_SERIAL_NUMBER,
        ),
        "analog_address_2": build_request(
            2,
            CID2_READ_ANALOG_INFORMATION,
            b"\x02",
        ),
        "status_address_2": build_request(
            2,
            CID2_READ_STATUS_INFORMATION,
            b"\x02",
        ),
        "hardware_address_2": build_request(
            2,
            CID2_READ_HARDWARE_VERSION,
        ),
        "serial_address_2": build_request(
            2,
            CID2_READ_SERIAL_NUMBER,
        ),
    }

    for name, expected_frame in expected.items():
        actual_frame = actual[name]
        if actual_frame != expected_frame:
            raise PaceError(
                f"request-generation self-test failed for {name}, "
                f"actual={actual_frame!r}, "
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


def unsigned_int16(value: bytes) -> int:
    if len(value) != 2:
        raise PaceError(
            f"expected two bytes for unsigned int16, got {len(value)}"
        )
    return int.from_bytes(value, byteorder="big", signed=False)


def signed_int16(value: bytes) -> int:
    if len(value) != 2:
        raise PaceError(
            f"expected two bytes for signed int16, got {len(value)}"
        )
    return int.from_bytes(value, byteorder="big", signed=True)


def decode_analog_payload(
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
    responding_address = take_byte("responding pack address")

    if responding_address != expected_address:
        raise PaceError(
            "analogue payload address mismatch, "
            f"expected {expected_address}, "
            f"received {responding_address}"
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
    voltage_v = round(total_voltage_mv / 1000.0, 3)
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

    soc_percent = (
        round(
            remaining_capacity_raw
            / full_capacity_raw
            * 100.0,
            2,
        )
        if full_capacity_raw
        else None
    )

    soh_percent = (
        round(
            full_capacity_raw
            / design_capacity_raw
            * 100.0,
            2,
        )
        if design_capacity_raw
        else None
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
        "responding_pack_address": responding_address,
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
        "minimum_temperature_c": (
            min(temperatures_c) if temperatures_c else None
        ),
        "maximum_temperature_c": (
            max(temperatures_c) if temperatures_c else None
        ),
        "current_raw": current_raw,
        "current_a": current_a,
        "total_voltage_mv": total_voltage_mv,
        "total_voltage_v": voltage_v,
        "power_w": round(voltage_v * current_a, 2),
        "operating_direction": (
            "charging"
            if current_raw > 0
            else "discharging"
            if current_raw < 0
            else "idle"
        ),
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
    expected_address: int,
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
    responding_address = take_byte("responding pack address")

    if responding_address != expected_address:
        raise PaceError(
            "status payload address mismatch, "
            f"expected {expected_address}, "
            f"received {responding_address}"
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
        "responding_pack_address": responding_address,
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


def decode_identity_payload(payload: bytes) -> dict[str, Any]:
    trimmed = payload.rstrip(b"\x00 ")

    return {
        "text": trimmed.decode(
            "ascii",
            errors="backslashreplace",
        ),
        "raw_hex": payload.hex(" ").upper(),
        "raw_byte_length": len(payload),
        "trimmed_byte_length": len(trimmed),
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
    return (
        run_command(
            [
                "systemctl",
                "is-active",
                "--quiet",
                service_name,
            ]
        ).returncode
        == 0
    )


def serial_device_is_busy(device: str) -> bool:
    return (
        run_command(
            [
                "sudo",
                "fuser",
                device,
            ],
            capture_output=True,
        ).returncode
        == 0
    )


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
        "payload_hex": payload_bytes.hex(" ").upper(),
        "payload_byte_length": len(payload_bytes),
        "_payload_bytes": payload_bytes,
    }

    time.sleep(0.25)
    return result


def open_port(device_link: Path) -> serial.Serial:
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
    return port


def collect_pack(
    device_link: Path,
    *,
    pack_label: str,
    candidate_addresses: Iterable[int],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "pack_label": pack_label,
        "candidate_addresses": list(candidate_addresses),
        "detected_address": None,
        "queries": {},
        "probe_failures": [],
        "complete_success": False,
    }

    if serial_device_is_busy(str(device_link.resolve())):
        raise PaceError(
            f"another process currently owns {device_link.resolve()}"
        )

    selected_address: int | None = None
    analog_result: dict[str, Any] | None = None
    analog_decoded: dict[str, Any] | None = None

    with open_port(device_link) as port:
        for candidate in result["candidate_addresses"]:
            print(
                f"probing {pack_label} at PACE address "
                f"{candidate}, read-only CID2=0x42",
                flush=True,
            )

            try:
                probe = send_query(
                    port,
                    name="analog_information",
                    header_address=candidate,
                    cid2=CID2_READ_ANALOG_INFORMATION,
                    payload=bytes([candidate]),
                )

                decoded = decode_analog_payload(
                    probe["_payload_bytes"],
                    expected_address=candidate,
                )

                selected_address = candidate
                analog_result = probe
                analog_decoded = decoded

                print(
                    f"PASS: {pack_label} responded at "
                    f"PACE address {candidate}",
                    flush=True,
                )
                break

            except Exception as exc:
                result["probe_failures"].append(
                    {
                        "candidate_address": candidate,
                        "error": str(exc),
                        "type": type(exc).__name__,
                    }
                )
                print(
                    f"NOTICE: no valid {pack_label} response "
                    f"at address {candidate}: {exc}",
                    flush=True,
                )

        if selected_address is None:
            raise PaceError(
                f"{pack_label} did not return a valid analogue "
                "response at any candidate address"
            )

        assert analog_result is not None
        assert analog_decoded is not None

        analog_result.pop("_payload_bytes", None)
        analog_result["decoded"] = analog_decoded

        result["detected_address"] = selected_address
        result["queries"]["analog_information"] = analog_result

        count_query = send_query(
            port,
            name="bms_count",
            header_address=selected_address,
            cid2=CID2_READ_BMS_COUNT,
            payload=b"",
        )

        count_payload = count_query.pop("_payload_bytes")

        count_query["decoded"] = {
            "reported_bms_count": (
                count_payload[0]
                if count_payload
                else None
            )
        }

        result["queries"]["bms_count"] = count_query

        status_query = send_query(
            port,
            name="status_information",
            header_address=selected_address,
            cid2=CID2_READ_STATUS_INFORMATION,
            payload=bytes([selected_address]),
        )

        status_payload = status_query.pop("_payload_bytes")

        status_query["decoded"] = decode_status_payload(
            status_payload,
            expected_address=selected_address,
        )

        result["queries"]["status_information"] = status_query

        hardware_query = send_query(
            port,
            name="hardware_version",
            header_address=selected_address,
            cid2=CID2_READ_HARDWARE_VERSION,
            payload=b"",
        )

        hardware_payload = hardware_query.pop("_payload_bytes")
        hardware_query["decoded"] = decode_identity_payload(
            hardware_payload
        )

        result["queries"]["hardware_version"] = hardware_query

        serial_query = send_query(
            port,
            name="serial_number",
            header_address=selected_address,
            cid2=CID2_READ_SERIAL_NUMBER,
            payload=b"",
        )

        serial_payload = serial_query.pop("_payload_bytes")
        serial_query["decoded"] = decode_identity_payload(
            serial_payload
        )

        result["queries"]["serial_number"] = serial_query

    result["complete_success"] = True
    result["capture_completed_at_utc"] = utc_now()
    return result


def battery_summary(pack: dict[str, Any]) -> dict[str, Any]:
    analog = pack["queries"]["analog_information"]["decoded"]
    status = pack["queries"]["status_information"]["decoded"]
    hardware = pack["queries"]["hardware_version"]["decoded"]
    serial_number = pack["queries"]["serial_number"]["decoded"]
    count = pack["queries"]["bms_count"]["decoded"]

    return {
        "pack_label": pack["pack_label"],
        "detected_address": pack["detected_address"],
        "reported_bms_count": count["reported_bms_count"],
        "hardware_version": hardware["text"],
        "serial_number": serial_number["text"],
        "cycle_count": analog["cycle_count"],
        "total_voltage_v": analog["total_voltage_v"],
        "current_a": analog["current_a"],
        "operating_direction": analog["operating_direction"],
        "remaining_capacity_ah": analog["remaining_capacity_ah"],
        "full_capacity_ah": analog["full_capacity_ah"],
        "design_capacity_ah": analog["design_capacity_ah"],
        "calculated_soc_percent": (
            analog["calculated_soc_percent"]
        ),
        "calculated_soh_percent": (
            analog["calculated_soh_percent"]
        ),
        "minimum_cell_mv": (
            analog["cell_summary"]["minimum_mv"]
        ),
        "maximum_cell_mv": (
            analog["cell_summary"]["maximum_mv"]
        ),
        "cell_differential_mv": (
            analog["cell_summary"]["differential_mv"]
        ),
        "temperatures_c": analog["temperatures_c"],
        "maximum_temperature_c": (
            analog["maximum_temperature_c"]
        ),
        "warnings_clear": (
            status["summary"]["warnings_clear"]
        ),
        "protections_clear": (
            status["summary"]["protections_clear"]
        ),
        "faults_clear": (
            status["summary"]["faults_clear"]
        ),
        "balancing_cells": (
            status["balancing"]["active_cells"]
        ),
        "charge_mosfet_on": (
            status["system_status"]["charge_mosfet_on"]
        ),
        "discharge_mosfet_on": (
            status["system_status"]["discharge_mosfet_on"]
        ),
        "system_flags": (
            status["system_status"]["active"]
        ),
        "configuration_flags": (
            status["configuration_status"]["active"]
        ),
    }


def format_value(value: Any, suffix: str = "") -> str:
    if value is None:
        return "not available"
    return f"{value}{suffix}"


def build_markdown_report_source(
    report: dict[str, Any],
) -> str:
    battery_1 = report["summary"]["battery_1"]
    battery_2 = report["summary"]["battery_2"]

    lines = [
        "# LVTOPSUN LV48100 Two-Battery Diagnostic Data",
        "",
        (
            "Read-only PACE BMS data collected directly from each "
            "battery using the same USB-to-RS232 service cable."
        ),
        "",
        f"Capture completed: {report['capture_completed_at_utc']}",
        "",
        "## Collection result",
        "",
        "| Control | Result |",
        "|---|---|",
        "| Battery 1 collection | PASS |",
        "| Battery 2 collection | PASS |",
        "| Write or control commands | None sent |",
        "| ModemManager restoration | Verified by collector |",
        "",
        "## Battery comparison",
        "",
        "| Metric | Battery 1 | Battery 2 |",
        "|---|---:|---:|",
        (
            f"| Detected PACE address | "
            f"{battery_1['detected_address']} | "
            f"{battery_2['detected_address']} |"
        ),
        (
            f"| Hardware version | "
            f"{battery_1['hardware_version']} | "
            f"{battery_2['hardware_version']} |"
        ),
        (
            f"| Serial identity | "
            f"{battery_1['serial_number']} | "
            f"{battery_2['serial_number']} |"
        ),
        (
            f"| Cycle count | "
            f"{battery_1['cycle_count']} | "
            f"{battery_2['cycle_count']} |"
        ),
        (
            f"| Pack voltage | "
            f"{battery_1['total_voltage_v']} V | "
            f"{battery_2['total_voltage_v']} V |"
        ),
        (
            f"| Pack current | "
            f"{battery_1['current_a']} A | "
            f"{battery_2['current_a']} A |"
        ),
        (
            f"| Operating direction | "
            f"{battery_1['operating_direction']} | "
            f"{battery_2['operating_direction']} |"
        ),
        (
            f"| Remaining capacity | "
            f"{battery_1['remaining_capacity_ah']} Ah | "
            f"{battery_2['remaining_capacity_ah']} Ah |"
        ),
        (
            f"| Learned full capacity | "
            f"{battery_1['full_capacity_ah']} Ah | "
            f"{battery_2['full_capacity_ah']} Ah |"
        ),
        (
            f"| Design capacity | "
            f"{battery_1['design_capacity_ah']} Ah | "
            f"{battery_2['design_capacity_ah']} Ah |"
        ),
        (
            f"| Calculated SOC | "
            f"{format_value(battery_1['calculated_soc_percent'], '%')} | "
            f"{format_value(battery_2['calculated_soc_percent'], '%')} |"
        ),
        (
            f"| Calculated SOH | "
            f"{format_value(battery_1['calculated_soh_percent'], '%')} | "
            f"{format_value(battery_2['calculated_soh_percent'], '%')} |"
        ),
        (
            f"| Cell differential | "
            f"{battery_1['cell_differential_mv']} mV | "
            f"{battery_2['cell_differential_mv']} mV |"
        ),
        (
            f"| Maximum temperature | "
            f"{format_value(battery_1['maximum_temperature_c'], ' C')} | "
            f"{format_value(battery_2['maximum_temperature_c'], ' C')} |"
        ),
        (
            f"| Warnings clear | "
            f"{battery_1['warnings_clear']} | "
            f"{battery_2['warnings_clear']} |"
        ),
        (
            f"| Protections clear | "
            f"{battery_1['protections_clear']} | "
            f"{battery_2['protections_clear']} |"
        ),
        (
            f"| Faults clear | "
            f"{battery_1['faults_clear']} | "
            f"{battery_2['faults_clear']} |"
        ),
        "",
        "## Battery 1 details",
        "",
        f"- Cell voltages: {report['packs']['battery_1']['queries']['analog_information']['decoded']['cell_voltages_v']}",
        f"- Temperatures: {battery_1['temperatures_c']} C",
        f"- System flags: {battery_1['system_flags']}",
        f"- Configuration flags: {battery_1['configuration_flags']}",
        f"- Balancing cells: {battery_1['balancing_cells']}",
        "",
        "## Battery 2 details",
        "",
        f"- Cell voltages: {report['packs']['battery_2']['queries']['analog_information']['decoded']['cell_voltages_v']}",
        f"- Temperatures: {battery_2['temperatures_c']} C",
        f"- System flags: {battery_2['system_flags']}",
        f"- Configuration flags: {battery_2['configuration_flags']}",
        f"- Balancing cells: {battery_2['balancing_cells']}",
        "",
        "## Interpretation limitations",
        "",
        (
            "- SOC and SOH are calculated from BMS-reported capacity "
            "fields and are not independent capacity-test results."
        ),
        (
            "- Cycle count is the value maintained by each BMS and "
            "may not represent a universal full-equivalent-cycle method."
        ),
        (
            "- The two batteries are captured sequentially, so current, "
            "temperature, and operational state are not simultaneous."
        ),
        (
            "- Unknown vendor trailing bytes are preserved in the JSON "
            "without assigning unsupported meanings."
        ),
        "",
        "## Safety and command inventory",
        "",
        "- `0x90`, read BMS count",
        "- `0x42`, read analogue information",
        "- `0x44`, read status information",
        "- `0xC1`, read hardware version",
        "- `0xC2`, read serial number",
        "",
        (
            "No configuration, calibration, MOSFET-control, shutdown, "
            "addressing, date-setting, or firmware command was sent."
        ),
        "",
    ]

    return "\n".join(lines)


def write_file_with_checksum(
    path: Path,
    content: bytes,
) -> Path:
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    checksum_path = Path(f"{path}.sha256")
    checksum_path.write_text(
        f"{digest}  {path.name}\n",
        encoding="utf-8",
    )
    return checksum_path


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect complete read-only diagnostic data from two "
            "LVTOPSUN LV48100 batteries by moving only the existing "
            "RS232 service cable between packs."
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
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "Evidence output directory. "
            f"Default: {DEFAULT_OUTPUT_DIR}"
        ),
    )
    parser.add_argument(
        "--battery-1-address",
        type=int,
        default=1,
        help="Expected direct PACE address for battery 1. Default: 1",
    )
    parser.add_argument(
        "--battery-2-addresses",
        default="2,1",
        help=(
            "Comma-separated direct addresses to try for battery 2. "
            "Default: 2,1"
        ),
    )

    return parser.parse_args()


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


def main() -> int:
    args = parse_arguments()

    if not 1 <= args.battery_1_address <= 15:
        print(
            "FAIL: battery 1 address must be from 1 through 15",
            file=sys.stderr,
        )
        return 10

    try:
        battery_2_addresses = [
            int(item.strip())
            for item in args.battery_2_addresses.split(",")
            if item.strip()
        ]
    except ValueError:
        print(
            "FAIL: battery 2 addresses must be comma-separated integers",
            file=sys.stderr,
        )
        return 11

    if not battery_2_addresses:
        print(
            "FAIL: at least one battery 2 candidate address is required",
            file=sys.stderr,
        )
        return 12

    if any(
        address < 1 or address > 15
        for address in battery_2_addresses
    ):
        print(
            "FAIL: battery 2 candidate addresses must be from 1 through 15",
            file=sys.stderr,
        )
        return 13

    try:
        verify_request_generation()
    except Exception as exc:
        print(
            f"FAIL: request-generation self-test failed: {exc}",
            file=sys.stderr,
        )
        return 14

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

    if not os.access(resolved_device, os.R_OK | os.W_OK):
        print(
            f"FAIL: current user cannot read and write "
            f"{resolved_device}",
            file=sys.stderr,
        )
        return 22

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = utc_filename_timestamp()
    json_path = output_dir / (
        "lvtopsun-lv48100-two-battery-report-data-"
        f"{timestamp}.json"
    )
    markdown_path = output_dir / (
        "lvtopsun-lv48100-two-battery-report-source-"
        f"{timestamp}.md"
    )
    log_path = output_dir / (
        "lvtopsun-lv48100-two-battery-collection-"
        f"{timestamp}.log"
    )

    modemmanager_was_active = service_is_active(
        "ModemManager.service"
    )
    modemmanager_restored = False

    original_stdout = sys.stdout
    original_stderr = sys.stderr

    report: dict[str, Any] = {
        "capture_started_at_utc": utc_now(),
        "device_link": str(device_link),
        "resolved_device": str(resolved_device),
        "collection_method": (
            "Sequential direct RS232 service-port capture using "
            "the same USB-to-RS232 cable."
        ),
        "relay_discovery_context": {
            "previous_master_reported_bms_count": 1,
            "relay_collection_used": False,
            "reason": (
                "The directly connected battery reported one visible "
                "BMS, so battery 2 is collected by direct service-port "
                "connection instead of master relay."
            ),
        },
        "safety": {
            "read_only": True,
            "commands": [
                "0x90 read BMS count",
                "0x42 read analogue information",
                "0x44 read status information",
                "0xC1 read hardware version",
                "0xC2 read serial number",
            ],
            "write_or_control_commands_sent": False,
            "operator_instruction": (
                "Move only the blue RS232 service cable. "
                "Do not move power, inverter CAN, or inter-battery cables."
            ),
        },
        "transport": {
            "interface": "USB-to-RS232 serial adapter",
            "baudrate": 9600,
            "data_bits": 8,
            "parity": "none",
            "stop_bits": 1,
        },
        "packs": {},
        "summary": {},
        "failures": [],
        "complete_success": False,
    }

    def restore_modemmanager() -> None:
        nonlocal modemmanager_restored

        if not modemmanager_was_active or modemmanager_restored:
            return

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

            if service_is_active("ModemManager.service"):
                modemmanager_restored = True
                print("PASS: ModemManager is active")
            else:
                print(
                    "WARNING: ModemManager did not return to active state",
                    file=sys.stderr,
                )

        except Exception as exc:
            print(
                f"WARNING: failed to restore ModemManager: {exc}",
                file=sys.stderr,
            )

    def signal_handler(
        signum: int,
        frame: Any,
    ) -> None:
        del frame
        restore_modemmanager()
        raise OperatorAbort(
            f"received signal {signum}, collection stopped"
        )

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)

    with log_path.open("w", encoding="utf-8") as log_file:
        sys.stdout = Tee(original_stdout, log_file)
        sys.stderr = Tee(original_stderr, log_file)

        try:
            print()
            print(
                "### LVTOPSUN LV48100 two-battery "
                "read-only report-data collection"
            )
            print(f"time UTC: {utc_now()}")
            print(f"serial link: {device_link}")
            print(f"resolved device: {resolved_device}")
            print(f"log: {log_path}")

            print()
            print("### safety boundary")
            print(
                "Leave all high-current battery power cables connected."
            )
            print(
                "Leave the grey inverter communication cable connected."
            )
            print(
                "Leave the black inter-battery cable connected."
            )
            print(
                "Move only the blue RS232 service cable when instructed."
            )
            print(
                "The script sends only PACE read commands "
                "0x90, 0x42, 0x44, 0xC1, and 0xC2."
            )

            if modemmanager_was_active:
                print()
                print("### temporarily stop ModemManager")

                run_command(["sudo", "-v"], check=True)

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

                if service_is_active("ModemManager.service"):
                    raise PaceError(
                        "ModemManager remained active"
                    )

                print(
                    "PASS: ModemManager stopped temporarily"
                )

            print()
            print("### collect battery 1")
            print(
                "Confirm the blue RS232 service cable is connected "
                "to battery 1."
            )

            confirmation = input(
                "Type BATTERY1 to begin battery 1 collection: "
            ).strip()

            if confirmation != "BATTERY1":
                raise OperatorAbort(
                    "battery 1 confirmation was not provided"
                )

            battery_1 = collect_pack(
                device_link,
                pack_label="battery_1",
                candidate_addresses=[
                    args.battery_1_address
                ],
            )

            report["packs"]["battery_1"] = battery_1
            report["summary"]["battery_1"] = battery_summary(
                battery_1
            )

            summary_1 = report["summary"]["battery_1"]

            print()
            print("PASS: battery 1 collection completed")
            print(
                f"battery 1 serial: "
                f"{summary_1['serial_number']}"
            )
            print(
                f"battery 1 cycles: "
                f"{summary_1['cycle_count']}"
            )
            print(
                f"battery 1 SOC: "
                f"{summary_1['calculated_soc_percent']}%"
            )
            print(
                f"battery 1 SOH: "
                f"{summary_1['calculated_soh_percent']}%"
            )

            print()
            print("### move the blue service cable")
            print(
                "1. Do not move the grey inverter cable."
            )
            print(
                "2. Do not move the black inter-battery cable."
            )
            print(
                "3. Do not disconnect either battery's power cables."
            )
            print(
                "4. Unplug only the blue RS232 service cable from "
                "battery 1."
            )
            print(
                "5. Plug that same blue cable into battery 2's "
                "RS232 service port."
            )
            print(
                "6. Ensure the connector is fully seated."
            )

            confirmation = input(
                "Type BATTERY2 after the blue cable is connected "
                "to battery 2: "
            ).strip()

            if confirmation != "BATTERY2":
                raise OperatorAbort(
                    "battery 2 confirmation was not provided"
                )

            print()
            print("### collect battery 2")

            battery_2 = collect_pack(
                device_link,
                pack_label="battery_2",
                candidate_addresses=battery_2_addresses,
            )

            report["packs"]["battery_2"] = battery_2
            report["summary"]["battery_2"] = battery_summary(
                battery_2
            )

            summary_2 = report["summary"]["battery_2"]

            print()
            print("PASS: battery 2 collection completed")
            print(
                f"battery 2 detected address: "
                f"{summary_2['detected_address']}"
            )
            print(
                f"battery 2 serial: "
                f"{summary_2['serial_number']}"
            )
            print(
                f"battery 2 cycles: "
                f"{summary_2['cycle_count']}"
            )
            print(
                f"battery 2 SOC: "
                f"{summary_2['calculated_soc_percent']}%"
            )
            print(
                f"battery 2 SOH: "
                f"{summary_2['calculated_soh_percent']}%"
            )

            report["complete_success"] = True
            report["capture_completed_at_utc"] = utc_now()

            json_content = (
                json.dumps(
                    report,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")

            markdown_content = (
                build_markdown_report_source(report)
                + "\n"
            ).encode("utf-8")

            json_checksum_path = write_file_with_checksum(
                json_path,
                json_content,
            )

            markdown_checksum_path = write_file_with_checksum(
                markdown_path,
                markdown_content,
            )

            print()
            print("### combined evidence")
            print(f"JSON: {json_path}")
            print(f"JSON SHA-256: {json_checksum_path}")
            print(f"Markdown: {markdown_path}")
            print(
                f"Markdown SHA-256: "
                f"{markdown_checksum_path}"
            )
            print(f"log: {log_path}")

            print()
            print(
                "PASS: complete two-battery report data "
                "was collected"
            )
            print(
                "The JSON and Markdown files contain the data "
                "needed to generate the updated PDF report."
            )

        except Exception as exc:
            report["complete_success"] = False
            report["capture_completed_at_utc"] = utc_now()
            report["failures"].append(
                {
                    "error": str(exc),
                    "type": type(exc).__name__,
                }
            )

            print()
            print(
                f"FAIL: {exc}",
                file=sys.stderr,
            )

            try:
                failure_content = (
                    json.dumps(
                        report,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8")

                failure_checksum_path = (
                    write_file_with_checksum(
                        json_path,
                        failure_content,
                    )
                )

                print()
                print("### partial evidence")
                print(f"JSON: {json_path}")
                print(
                    f"JSON SHA-256: "
                    f"{failure_checksum_path}"
                )
                print(f"log: {log_path}")

            except Exception as evidence_exc:
                print(
                    f"WARNING: partial evidence write failed: "
                    f"{evidence_exc}",
                    file=sys.stderr,
                )

        finally:
            restore_modemmanager()

            print()
            print("### safety statement")
            print("Read-only commands used or permitted:")
            print("  0x90, read BMS count")
            print("  0x42, read analogue information")
            print("  0x44, read status information")
            print("  0xC1, read hardware version")
            print("  0xC2, read serial number")
            print(
                "No configuration, calibration, MOSFET control, "
                "shutdown, addressing, date-setting, or firmware "
                "command was sent."
            )
            print()
            print(
                "You may reconnect the blue service cable to "
                "battery 1 after the script exits."
            )

            sys.stdout = original_stdout
            sys.stderr = original_stderr

    return 0 if report["complete_success"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
