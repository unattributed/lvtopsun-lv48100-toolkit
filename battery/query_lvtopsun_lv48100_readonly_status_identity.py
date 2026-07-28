#!/usr/bin/env python3
"""Collect read-only LV48100 status, protection, and identity data."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import serial


PROTOCOL_VERSION = 0x25
CID1_LITHIUM_IRON = 0x46
CID2_READ_STATUS_INFORMATION = 0x44
CID2_READ_HARDWARE_VERSION = 0xC1
CID2_READ_SERIAL_NUMBER = 0xC2


class PaceError(RuntimeError):
    """PACE frame or transport validation error."""


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


def build_request(address: int, cid2: int, payload: bytes = b"") -> bytes:
    info_ascii = payload.hex().upper()
    length_field = calculate_length_field(len(info_ascii))

    frame_body = (
        f"{PROTOCOL_VERSION:02X}"
        f"{address:02X}"
        f"{CID1_LITHIUM_IRON:02X}"
        f"{cid2:02X}"
        f"{length_field:04X}"
        f"{info_ascii}"
    ).encode("ascii")

    checksum = calculate_frame_checksum(frame_body)
    return b"~" + frame_body + f"{checksum:04X}".encode("ascii") + b"\r"


def parse_response(response: bytes) -> dict[str, Any]:
    if not response:
        raise PaceError("no response was received")
    if not response.startswith(b"~"):
        raise PaceError("response does not begin with PACE SOI 0x7E")
    if not response.endswith(b"\r"):
        raise PaceError("response does not end with PACE EOI 0x0D")

    body = response[1:-1]
    if len(body) < 16:
        raise PaceError(f"response is too short: {len(body)} bytes")

    try:
        text = body.decode("ascii")
    except UnicodeDecodeError as exc:
        raise PaceError("response contains non-ASCII frame data") from exc

    try:
        version = int(text[0:2], 16)
        address = int(text[2:4], 16)
        cid1 = int(text[4:6], 16)
        return_code = int(text[6:8], 16)
        length_field = int(text[8:12], 16)
        received_checksum = int(text[-4:], 16)
    except ValueError as exc:
        raise PaceError("response header contains invalid hexadecimal") from exc

    info_ascii = text[12:-4]
    calculated_checksum = calculate_frame_checksum(body[:-4])
    if received_checksum != calculated_checksum:
        raise PaceError(
            "checksum mismatch, "
            f"received {received_checksum:04X}, "
            f"calculated {calculated_checksum:04X}"
        )

    declared_length = length_field & 0x0FFF
    if len(info_ascii) != declared_length:
        raise PaceError(
            "INFO length mismatch, "
            f"declared {declared_length}, received {len(info_ascii)}"
        )

    expected_length_field = calculate_length_field(declared_length)
    if length_field != expected_length_field:
        raise PaceError(
            "LENGTH checksum mismatch, "
            f"received {length_field:04X}, "
            f"calculated {expected_length_field:04X}"
        )

    if len(info_ascii) % 2:
        raise PaceError("INFO contains an odd number of hexadecimal characters")

    try:
        payload = bytes.fromhex(info_ascii)
    except ValueError as exc:
        raise PaceError("INFO contains invalid hexadecimal") from exc

    if return_code != 0:
        raise PaceError(f"BMS returned RTN={return_code:02X}")

    return {
        "version": version,
        "version_hex": f"{version:02X}",
        "address": address,
        "address_hex": f"{address:02X}",
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
            "ascii", errors="backslashreplace"
        ).replace("\r", "\\r"),
        "response_hex": response.hex(" ").upper(),
        "response_byte_length": len(response),
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
        for mask, description in definitions
        if value & mask
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


def decode_status(payload: bytes, expected_address: int) -> dict[str, Any]:
    offset = 0

    def take(label: str) -> int:
        nonlocal offset
        if offset >= len(payload):
            raise PaceError(f"status payload ended while reading {label}")
        value = payload[offset]
        offset += 1
        return value

    leading_byte = take("leading protocol byte")
    responding_address = take("responding address")
    if responding_address != expected_address:
        raise PaceError(
            "status payload address mismatch, "
            f"expected {expected_address}, received {responding_address}"
        )

    cell_count = take("cell-warning count")
    if not 1 <= cell_count <= 32:
        raise PaceError(f"unreasonable cell-warning count: {cell_count}")

    cell_warnings = []
    for number in range(1, cell_count + 1):
        raw = take(f"cell {number} warning")
        cell_warnings.append(
            {
                "cell": number,
                "raw_hex": f"{raw:02X}",
                "state": decode_limit_value(raw),
            }
        )

    temperature_count = take("temperature-warning count")
    if not 0 <= temperature_count <= 16:
        raise PaceError(
            f"unreasonable temperature-warning count: {temperature_count}"
        )

    temperature_warnings = []
    for number in range(1, temperature_count + 1):
        raw = take(f"temperature {number} warning")
        temperature_warnings.append(
            {
                "sensor": number,
                "raw_hex": f"{raw:02X}",
                "state": decode_limit_value(raw),
            }
        )

    charge_current_warning = take("charge-current warning")
    total_voltage_warning = take("total-voltage warning")
    discharge_current_warning = take("discharge-current warning")
    protection_1 = take("protection status 1")
    protection_2 = take("protection status 2")
    system_status = take("system status")
    configuration_status = take("configuration status")
    fault_status = take("fault status")
    balance_high = take("balance high byte")
    balance_low = take("balance low byte")
    warning_1 = take("warning status 1")
    warning_2 = take("warning status 2")

    balancing_mask = (balance_high << 8) | balance_low
    balancing_cells = [
        cell
        for cell in range(1, cell_count + 1)
        if balancing_mask & (1 << (cell - 1))
    ]

    active_cell_warnings = [
        item for item in cell_warnings if item["raw_hex"] != "00"
    ]
    active_temperature_warnings = [
        item for item in temperature_warnings if item["raw_hex"] != "00"
    ]

    scalar_warnings = {
        "charge_current": {
            "raw_hex": f"{charge_current_warning:02X}",
            "state": decode_limit_value(charge_current_warning),
        },
        "total_voltage": {
            "raw_hex": f"{total_voltage_warning:02X}",
            "state": decode_limit_value(total_voltage_warning),
        },
        "discharge_current": {
            "raw_hex": f"{discharge_current_warning:02X}",
            "state": decode_limit_value(discharge_current_warning),
        },
    }

    protection_1_active = decode_flags(
        protection_1, PROTECTION_1_FLAGS
    )
    protection_2_active = decode_flags(
        protection_2, PROTECTION_2_FLAGS
    )
    system_active = decode_flags(system_status, SYSTEM_FLAGS)
    configuration_active = decode_flags(
        configuration_status, CONFIGURATION_FLAGS
    )
    fault_active = decode_flags(fault_status, FAULT_FLAGS)
    warning_1_active = decode_flags(warning_1, WARNING_1_FLAGS)
    warning_2_active = decode_flags(warning_2, WARNING_2_FLAGS)

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
        protection_1_active or protection_2_active
    )
    faults_clear = not fault_active

    trailing = payload[offset:]

    return {
        "leading_protocol_byte_hex": f"{leading_byte:02X}",
        "responding_pack_address": responding_address,
        "cell_count": cell_count,
        "cell_warnings": cell_warnings,
        "active_cell_warnings": active_cell_warnings,
        "temperature_count": temperature_count,
        "temperature_warnings": temperature_warnings,
        "active_temperature_warnings": active_temperature_warnings,
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
            "charge_mosfet_on": bool(system_status & 0x02),
            "discharge_mosfet_on": bool(system_status & 0x04),
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
            "raw_hex": f"{balancing_mask:04X}",
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
            "byte_length": len(trailing),
            "hex": trailing.hex(" ").upper(),
        },
        "summary": {
            "warnings_clear": warnings_clear,
            "protections_clear": protections_clear,
            "faults_clear": faults_clear,
            "balancing_active": bool(balancing_cells),
            "all_clear": (
                warnings_clear
                and protections_clear
                and faults_clear
            ),
        },
    }


def decode_identity(payload: bytes) -> dict[str, Any]:
    trimmed = payload.rstrip(b"\x00 ")
    return {
        "text": trimmed.decode("ascii", errors="backslashreplace"),
        "raw_hex": payload.hex(" ").upper(),
        "raw_byte_length": len(payload),
        "trimmed_byte_length": len(trimmed),
    }


def query_once(
    port: serial.Serial,
    name: str,
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
            f"{name}: wrote {written} of {len(request)} request bytes"
        )

    response = port.read_until(expected=b"\r", size=4096)
    parsed = parse_response(response)

    if parsed["version"] != PROTOCOL_VERSION:
        raise PaceError(
            f"{name}: protocol version {parsed['version_hex']} "
            f"was not expected"
        )
    if parsed["address"] != address:
        raise PaceError(
            f"{name}: response address {parsed['address_hex']} "
            f"did not match {address:02X}"
        )
    if parsed["cid1"] != CID1_LITHIUM_IRON:
        raise PaceError(
            f"{name}: CID1 {parsed['cid1_hex']} "
            f"did not match {CID1_LITHIUM_IRON:02X}"
        )

    raw_payload = parsed.pop("payload")
    result: dict[str, Any] = {
        "name": name,
        "cid2_hex": f"{cid2:02X}",
        "request_ascii": request.decode("ascii").replace("\r", "\\r"),
        "request_hex": request.hex(" ").upper(),
        "response": parsed,
    }

    try:
        if cid2 == CID2_READ_STATUS_INFORMATION:
            result["decoded"] = decode_status(raw_payload, address)
        else:
            result["decoded"] = decode_identity(raw_payload)
    except Exception as exc:
        result["decode_error"] = str(exc)
        result["decoded"] = None

    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True)
    parser.add_argument("--pack-address", type=int, default=1)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not 1 <= args.pack_address <= 15:
        print("FAIL: pack address must be from 1 through 15", file=sys.stderr)
        return 1

    output_path = Path(args.output)
    report: dict[str, Any] = {
        "capture_started_at_utc": utc_now(),
        "device": str(Path(args.device).resolve()),
        "battery": {
            "manufacturer": "LVTOPSUN",
            "model": "LV48100",
            "pack_address": args.pack_address,
            "pack_address_hex": f"{args.pack_address:02X}",
        },
        "transport": {
            "interface": "RS232",
            "baudrate": 9600,
            "data_bits": 8,
            "parity": "none",
            "stop_bits": 1,
        },
        "safety": {
            "read_only": True,
            "commands": [
                "0x44 read status information",
                "0xC1 read hardware version",
                "0xC2 read serial number",
            ],
            "write_or_control_commands_sent": False,
        },
        "queries": {},
        "failures": [],
    }

    definitions = (
        (
            "status_information",
            CID2_READ_STATUS_INFORMATION,
            bytes([args.pack_address]),
        ),
        ("hardware_version", CID2_READ_HARDWARE_VERSION, b""),
        ("serial_number", CID2_READ_SERIAL_NUMBER, b""),
    )

    port = serial.Serial(
        port=args.device,
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

    try:
        port.dtr = False
        port.rts = False

        for name, cid2, payload in definitions:
            print(f"querying {name}, CID2=0x{cid2:02X}", flush=True)
            try:
                result = query_once(
                    port=port,
                    name=name,
                    address=args.pack_address,
                    cid2=cid2,
                    payload=payload,
                )
                report["queries"][name] = result
                print(
                    f"PASS: {name} returned a valid PACE frame",
                    flush=True,
                )
            except Exception as exc:
                report["queries"][name] = {
                    "name": name,
                    "cid2_hex": f"{cid2:02X}",
                    "error": str(exc),
                }
                report["failures"].append(
                    {
                        "query": name,
                        "cid2_hex": f"{cid2:02X}",
                        "error": str(exc),
                    }
                )
                print(f"FAIL: {name}: {exc}", file=sys.stderr, flush=True)

            time.sleep(0.25)
    finally:
        port.close()

    report["capture_completed_at_utc"] = utc_now()
    report["complete_success"] = not report["failures"]

    output_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    print(f"evidence: {output_path}")

    status = report["queries"].get("status_information", {}).get("decoded")
    if status:
        summary = status["summary"]
        system = status["system_status"]
        print("status summary:")
        print(f"  warnings clear: {summary['warnings_clear']}")
        print(f"  protections clear: {summary['protections_clear']}")
        print(f"  faults clear: {summary['faults_clear']}")
        print(f"  balancing cells: {status['balancing']['active_cells']}")
        print(f"  charging: {system['charging']}")
        print(f"  discharging: {system['discharging']}")
        print(f"  charge MOSFET on: {system['charge_mosfet_on']}")
        print(f"  discharge MOSFET on: {system['discharge_mosfet_on']}")
        print(
            "  vendor trailing bytes: "
            f"{status['vendor_trailing_data']['hex']}"
        )

    hardware = report["queries"].get("hardware_version", {}).get("decoded")
    serial_number = report["queries"].get("serial_number", {}).get("decoded")

    if hardware:
        print(f"hardware version: {hardware['text']}")
    if serial_number:
        print(f"serial number: {serial_number['text']}")

    if report["failures"]:
        print(
            "PARTIAL: at least one read-only query failed, "
            "and all available evidence was preserved",
            file=sys.stderr,
        )
        return 2

    print("PASS: all three read-only PACE queries completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
