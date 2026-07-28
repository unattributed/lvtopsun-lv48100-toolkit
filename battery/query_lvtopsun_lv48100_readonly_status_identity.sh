#!/usr/bin/env bash

# Safely run the read-only collector and restore ModemManager.

set -u
set -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_COLLECTOR="${SCRIPT_DIR}/query_lvtopsun_lv48100_readonly_status_identity.py"
SERIAL_LINK="${SERIAL_LINK:-${LV48100_DEVICE:-/dev/ttyUSB0}}"
OUTPUT_DIR="${OUTPUT_DIR:-${LV48100_OUTPUT_DIR:-${PWD}/reports}}"
PACK_ADDRESS="${1:-1}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUTPUT_FILE="${OUTPUT_DIR}/lvtopsun-lv48100-pack$(printf '%02d' "$PACK_ADDRESS")-status-identity-${TIMESTAMP}.json"
CHECKSUM_FILE="${OUTPUT_FILE}.sha256"
LOG_FILE="${OUTPUT_DIR}/lvtopsun-lv48100-status-identity-${TIMESTAMP}.log"

MODEMMANAGER_WAS_ACTIVE=0
MODEMMANAGER_RESTORED=0
COLLECTOR_STATUS=255

mkdir -p "$OUTPUT_DIR"
exec > >(tee "$LOG_FILE") 2>&1

restore_modemmanager() {
    if [[ "$MODEMMANAGER_WAS_ACTIVE" -eq 1 && "$MODEMMANAGER_RESTORED" -eq 0 ]]; then
        echo
        echo "### restore ModemManager"
        if sudo systemctl start ModemManager.service; then
            sleep 1
            if systemctl is-active --quiet ModemManager.service; then
                MODEMMANAGER_RESTORED=1
                echo "PASS: ModemManager is active"
            else
                echo "WARNING: ModemManager did not reach active state"
            fi
        else
            echo "WARNING: failed to start ModemManager"
        fi
    fi
}

# Called indirectly by trap.
# shellcheck disable=SC2317
cleanup() {
    restore_modemmanager
}

trap cleanup EXIT INT TERM HUP

echo
echo "### LVTOPSUN LV48100 read-only status and identity collector"
echo "time UTC: $(date -u --iso-8601=seconds)"
echo "pack address: $PACK_ADDRESS"
echo "log: $LOG_FILE"

if [[ ! "$PACK_ADDRESS" =~ ^[0-9]+$ ]] \
    || (( PACK_ADDRESS < 1 || PACK_ADDRESS > 15 )); then
    echo "FAIL: pack address must be an integer from 1 through 15"
    exit 10
fi

for command_name in python3 sha256sum readlink systemctl fuser timeout; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "FAIL: required command is unavailable: $command_name"
        exit 20
    fi
done

if ! python3 -c 'import serial' >/dev/null 2>&1; then
    echo "FAIL: Python serial module is unavailable"
    echo "Install it with: sudo apt install python3-serial"
    exit 21
fi

if [[ ! -f "$PYTHON_COLLECTOR" ]]; then
    echo "FAIL: Python collector is missing: $PYTHON_COLLECTOR"
    exit 22
fi

if ! python3 -m py_compile "$PYTHON_COLLECTOR"; then
    echo "FAIL: Python collector does not compile"
    exit 23
fi

if [[ ! -e "$SERIAL_LINK" ]]; then
    echo "FAIL: serial device is absent: $SERIAL_LINK"
    exit 30
fi

SERIAL_DEVICE="$(readlink -f "$SERIAL_LINK")"

if [[ ! -c "$SERIAL_DEVICE" ]]; then
    echo "FAIL: resolved path is not a character device: $SERIAL_DEVICE"
    exit 31
fi

if [[ ! -r "$SERIAL_DEVICE" || ! -w "$SERIAL_DEVICE" ]]; then
    echo "FAIL: current user cannot access $SERIAL_DEVICE"
    exit 32
fi

if systemctl is-active --quiet ModemManager.service; then
    MODEMMANAGER_WAS_ACTIVE=1
    echo
    echo "### temporarily stop ModemManager"
    if ! sudo systemctl stop ModemManager.service; then
        echo "FAIL: ModemManager could not be stopped"
        exit 40
    fi
    sleep 1
    echo "PASS: ModemManager stopped temporarily"
fi

echo
echo "### verify exclusive serial access"

if sudo fuser "$SERIAL_DEVICE" >/dev/null 2>&1; then
    echo "FAIL: another process currently owns $SERIAL_DEVICE"
    sudo fuser -v "$SERIAL_DEVICE" || true
    exit 41
fi

echo "PASS: serial device is unowned"

echo
echo "### execute read-only PACE queries"

timeout \
    --signal=TERM \
    --kill-after=5s \
    30s \
    python3 "$PYTHON_COLLECTOR" \
        --device "$SERIAL_LINK" \
        --pack-address "$PACK_ADDRESS" \
        --output "$OUTPUT_FILE"

COLLECTOR_STATUS=$?

echo
echo "collector process status: $COLLECTOR_STATUS"

restore_modemmanager

if [[ -f "$OUTPUT_FILE" ]]; then
    sha256sum "$OUTPUT_FILE" > "$CHECKSUM_FILE"
    echo
    echo "### evidence"
    echo "JSON: $OUTPUT_FILE"
    echo "SHA-256: $CHECKSUM_FILE"
    echo "log: $LOG_FILE"
    sha256sum --check "$CHECKSUM_FILE"
else
    echo "FAIL: expected JSON evidence was not created"
fi

echo
echo "### safety statement"
echo "Attempted commands:"
echo "  0x44, read status information"
echo "  0xC1, read hardware version"
echo "  0xC2, read serial number"
echo "No configuration, MOSFET control, shutdown, date-setting, or firmware command was sent."

trap - EXIT INT TERM HUP

if [[ "$COLLECTOR_STATUS" -eq 0 && -f "$OUTPUT_FILE" ]]; then
    echo
    echo "PASS: status and identity collection completed"
    exit 0
fi

if [[ "$COLLECTOR_STATUS" -eq 2 && -f "$OUTPUT_FILE" ]]; then
    echo
    echo "PARTIAL: evidence was preserved, but one or more read queries failed"
    exit 2
fi

echo
echo "FAIL: collection did not complete successfully"
exit 1
