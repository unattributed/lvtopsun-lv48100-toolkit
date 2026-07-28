#!/usr/bin/env bash

# Validate and run the read-only status and identity collector.

set +e
set -u
set -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COLLECTOR_PATH="${SCRIPT_DIR}/query_lvtopsun_lv48100_readonly_status_identity.sh"
PYTHON_COLLECTOR="${SCRIPT_DIR}/query_lvtopsun_lv48100_readonly_status_identity.py"
SERIAL_LINK="${SERIAL_LINK:-${LV48100_DEVICE:-/dev/ttyUSB0}}"
OUTPUT_DIR="${OUTPUT_DIR:-${LV48100_OUTPUT_DIR:-${PWD}/reports}}"
PACK_ADDRESS="${1:-1}"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="${OUTPUT_DIR}/lvtopsun-lv48100-guarded-collector-${TIMESTAMP}.log"
MARKER_FILE="${OUTPUT_DIR}/.lvtopsun-lv48100-guarded-collector-${TIMESTAMP}.marker"

MODEMMANAGER_WAS_ACTIVE=0
COLLECTOR_STATUS=255
SERIAL_BUSY=0

mkdir -p "$OUTPUT_DIR"
touch "$MARKER_FILE"

exec > >(tee "$LOG_FILE") 2>&1

restore_modemmanager() {
    if [[ "$MODEMMANAGER_WAS_ACTIVE" -eq 0 ]]; then
        return 0
    fi

    if systemctl is-active --quiet ModemManager.service; then
        return 0
    fi

    if [[ "$SERIAL_BUSY" -eq 1 ]]; then
        echo "WARNING: ModemManager was not restarted because the serial device remains busy"
        return 1
    fi

    echo
    echo "### restore ModemManager"

    sudo systemctl start ModemManager.service
    local start_status=$?

    if [[ "$start_status" -ne 0 ]]; then
        echo "WARNING: systemctl could not start ModemManager"
        return 1
    fi

    sleep 1

    if systemctl is-active --quiet ModemManager.service; then
        echo "PASS: ModemManager is active"
        return 0
    fi

    echo "WARNING: ModemManager did not reach active state"
    return 1
}

# Called indirectly by trap.
# shellcheck disable=SC2317
cleanup() {
    if [[ -e "$SERIAL_LINK" ]]; then
        local serial_device
        serial_device="$(readlink -f "$SERIAL_LINK")"

        if sudo fuser "$serial_device" >/dev/null 2>&1; then
            SERIAL_BUSY=1
        else
            SERIAL_BUSY=0
        fi
    fi

    restore_modemmanager || true
    rm -f "$MARKER_FILE"
}

trap cleanup EXIT INT TERM HUP

echo
echo "### guarded LVTOPSUN LV48100 collector"
echo "time UTC: $(date -u --iso-8601=seconds)"
echo "collector: $COLLECTOR_PATH"
echo "pack address: $PACK_ADDRESS"
echo "log: $LOG_FILE"

echo
echo "### validate pack address"

if [[ ! "$PACK_ADDRESS" =~ ^[0-9]+$ ]]; then
    echo "STOP: pack address must be an integer"
    COLLECTOR_STATUS=10
else
    if (( PACK_ADDRESS < 1 || PACK_ADDRESS > 15 )); then
        echo "STOP: pack address must be from 1 through 15"
        COLLECTOR_STATUS=11
    fi
fi

if [[ "$COLLECTOR_STATUS" -ne 255 ]]; then
    echo
    echo "No battery query was attempted."
else
    echo "PASS: pack address is valid"
fi

echo
echo "### validate collector installation"

if [[ "$COLLECTOR_STATUS" -eq 255 ]]; then
    if [[ ! -f "$COLLECTOR_PATH" ]]; then
        echo "STOP: collector does not exist"
        COLLECTOR_STATUS=20
    elif [[ ! -r "$COLLECTOR_PATH" ]]; then
        echo "STOP: collector is not readable"
        COLLECTOR_STATUS=21
    elif ! bash -n "$COLLECTOR_PATH"; then
        echo "STOP: collector has invalid shell syntax"
        COLLECTOR_STATUS=22
    else
        chmod 0755 "$COLLECTOR_PATH"
        echo "PASS: collector exists and has valid shell syntax"
        ls -l "$COLLECTOR_PATH"
        sha256sum "$COLLECTOR_PATH"
    fi
fi

echo
echo "### verify read-command allowlist"

if [[ "$COLLECTOR_STATUS" -eq 255 ]]; then
    ALLOWLIST_OUTPUT="$(
        grep -E '^[[:space:]]*CID2_[A-Z0-9_]+[[:space:]]*=[[:space:]]*0x[0-9A-Fa-f]+' \
            "$PYTHON_COLLECTOR" \
            | sed -E 's/^[[:space:]]+//' \
            | sort -u
    )"

    EXPECTED_ALLOWLIST="$(
        printf '%s\n' \
            'CID2_READ_HARDWARE_VERSION = 0xC1' \
            'CID2_READ_SERIAL_NUMBER = 0xC2' \
            'CID2_READ_STATUS_INFORMATION = 0x44' \
            | sort
    )"

    echo "Detected CID2 assignments:"
    printf '%s\n' "$ALLOWLIST_OUTPUT"

    if [[ "$ALLOWLIST_OUTPUT" != "$EXPECTED_ALLOWLIST" ]]; then
        echo "STOP: collector CID2 command assignments do not match the read-only allowlist"
        echo
        echo "Expected only:"
        printf '%s\n' "$EXPECTED_ALLOWLIST"
        COLLECTOR_STATUS=30
    else
        echo "PASS: collector contains only the expected read-command assignments"
    fi
fi

echo
echo "### verify serial device"

if [[ "$COLLECTOR_STATUS" -eq 255 ]]; then
    if [[ ! -e "$SERIAL_LINK" ]]; then
        echo "STOP: serial device does not exist"
        COLLECTOR_STATUS=40
    else
        SERIAL_DEVICE="$(readlink -f "$SERIAL_LINK")"

        if [[ ! -c "$SERIAL_DEVICE" ]]; then
            echo "STOP: resolved serial path is not a character device"
            COLLECTOR_STATUS=41
        else
            echo "persistent path: $SERIAL_LINK"
            echo "resolved device: $SERIAL_DEVICE"
            ls -l "$SERIAL_LINK"
            ls -l "$SERIAL_DEVICE"

            if sudo fuser "$SERIAL_DEVICE" >/dev/null 2>&1; then
                SERIAL_BUSY=1
                echo "STOP: serial device is already owned"
                sudo fuser -v "$SERIAL_DEVICE" || true
                COLLECTOR_STATUS=42
            else
                SERIAL_BUSY=0
                echo "PASS: serial device is unowned"
            fi
        fi
    fi
fi

echo
echo "### record initial ModemManager state"

if systemctl is-active --quiet ModemManager.service; then
    MODEMMANAGER_WAS_ACTIVE=1
    echo "ModemManager initial state: active"
else
    MODEMMANAGER_WAS_ACTIVE=0
    echo "ModemManager initial state: inactive"
fi

echo
echo "### execute collector in a protected subprocess"

if [[ "$COLLECTOR_STATUS" -eq 255 ]]; then
    echo "Execution timeout: 45 seconds"
    echo

    timeout \
        --signal=TERM \
        --kill-after=5s \
        45s \
        "$COLLECTOR_PATH" "$PACK_ADDRESS"

    COLLECTOR_STATUS=$?

    echo
    echo "collector process status: $COLLECTOR_STATUS"

    case "$COLLECTOR_STATUS" in
        0)
            echo "PASS: collector reported complete success"
            ;;
        2)
            echo "PARTIAL: collector preserved evidence but reported one or more failed queries"
            ;;
        124)
            echo "FAIL: collector exceeded the 45-second timeout"
            ;;
        137)
            echo "FAIL: collector required forced termination after the timeout"
            ;;
        143)
            echo "FAIL: collector terminated after receiving SIGTERM"
            ;;
        *)
            echo "FAIL: collector returned an unexpected nonzero status"
            ;;
    esac
else
    echo "SKIPPED: pre-execution validation did not pass"
fi

echo
echo "### inspect serial ownership after execution"

if [[ -e "$SERIAL_LINK" ]]; then
    SERIAL_DEVICE="$(readlink -f "$SERIAL_LINK")"

    if sudo fuser "$SERIAL_DEVICE" >/dev/null 2>&1; then
        SERIAL_BUSY=1
        echo "WARNING: serial device remains owned"
        sudo fuser -v "$SERIAL_DEVICE" || true
    else
        SERIAL_BUSY=0
        echo "PASS: serial device is unowned"
    fi
else
    SERIAL_BUSY=0
    echo "NOTICE: serial device is no longer present"
fi

restore_modemmanager || true

echo
echo "### newly created LV48100 files"

NEW_FILES="$(
    find "$OUTPUT_DIR" \
        -maxdepth 1 \
        -type f \
        -newer "$MARKER_FILE" \
        \( \
            -name 'lvtopsun-lv48100-pack01-status-identity-*.json' \
            -o \
            -name 'lvtopsun-lv48100-pack01-status-identity-*.json.sha256' \
        \) \
        -printf '%TY-%Tm-%Td %TH:%TM:%TS %s bytes %p\n' \
        2>/dev/null \
        | sort
)"

if [[ -n "$NEW_FILES" ]]; then
    printf '%s\n' "$NEW_FILES"
else
    echo "No new status and identity evidence files were created."
fi

echo
echo "### verify newly created checksums"

CHECKSUM_FILES="$(
    find "$OUTPUT_DIR" \
        -maxdepth 1 \
        -type f \
        -newer "$MARKER_FILE" \
        -name 'lvtopsun-lv48100-pack01-status-identity-*.json.sha256' \
        -print \
        2>/dev/null \
        | sort
)"

if [[ -n "$CHECKSUM_FILES" ]]; then
    while IFS= read -r checksum_file; do
        [[ -n "$checksum_file" ]] || continue
        sha256sum --check "$checksum_file" || true
    done <<< "$CHECKSUM_FILES"
else
    echo "No new checksum sidecar was available for verification."
fi

echo
echo "### final result"

echo "collector status: $COLLECTOR_STATUS"
echo "ModemManager active: $(systemctl is-active ModemManager.service 2>/dev/null || true)"
echo "serial device busy: $SERIAL_BUSY"
echo "complete log: $LOG_FILE"

if [[ "$COLLECTOR_STATUS" -eq 0 ]]; then
    echo "RESULT: collector completed successfully"
elif [[ "$COLLECTOR_STATUS" -eq 2 ]]; then
    echo "RESULT: collector completed partially, inspect the JSON and log"
else
    echo "RESULT: collector did not complete successfully, inspect the log"
fi

echo
echo "This guarded runner will return control to the terminal."
echo "It will not intentionally terminate the parent shell."

rm -f "$MARKER_FILE"
trap - EXIT INT TERM HUP

exit 0
