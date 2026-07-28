#!/usr/bin/env bash

# Inspect an interrupted collector without opening the serial port.

set +e

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COLLECTOR_PATH="${SCRIPT_DIR}/query_lvtopsun_lv48100_readonly_status_identity.sh"
SERIAL_LINK="${SERIAL_LINK:-${LV48100_DEVICE:-/dev/ttyUSB0}}"
DOWNLOADS_DIR="${OUTPUT_DIR:-${LV48100_OUTPUT_DIR:-${PWD}/reports}}"

echo
echo "### interrupted-query recovery audit"
echo "time UTC: $(date -u --iso-8601=seconds)"
echo

echo "### collector installation state"

if [[ -f "$COLLECTOR_PATH" ]]; then
    echo "PRESENT: $COLLECTOR_PATH"
    ls -l "$COLLECTOR_PATH"
    sha256sum "$COLLECTOR_PATH" 2>/dev/null || true
    wc -l "$COLLECTOR_PATH" 2>/dev/null || true

    if bash -n "$COLLECTOR_PATH"; then
        echo "PASS: collector shell syntax is valid"
    else
        echo "FAIL: collector shell syntax is invalid"
    fi
else
    echo "ABSENT: $COLLECTOR_PATH"
    echo "ASSESSMENT: the previous wrapper probably reached its exit 1 condition"
fi

echo
echo "### recent LV48100 evidence files"

find "$DOWNLOADS_DIR" \
    -maxdepth 1 \
    -type f \
    \( \
        -name 'lvtopsun-lv48100-pack01-status-identity-*.json' \
        -o \
        -name 'lvtopsun-lv48100-pack01-status-identity-*.json.sha256' \
        -o \
        -name 'lvtopsun-lv48100-pack01-status-identity-*.log' \
    \) \
    -printf '%TY-%Tm-%Td %TH:%TM:%TS %p\n' \
    2>/dev/null \
    | sort

EVIDENCE_COUNT="$(
    find "$DOWNLOADS_DIR" \
        -maxdepth 1 \
        -type f \
        -name 'lvtopsun-lv48100-pack01-status-identity-*.json' \
        2>/dev/null \
        | wc -l
)"

echo "JSON evidence count: $EVIDENCE_COUNT"

echo
echo "### collector process state"

PROCESS_OUTPUT="$(
    pgrep -af \
        'query_lvtopsun_lv48100_readonly_status_identity|lvtopsun-lv48100-pack01-status-identity' \
        2>/dev/null
)"

if [[ -n "$PROCESS_OUTPUT" ]]; then
    echo "WARNING: a matching process remains:"
    printf '%s\n' "$PROCESS_OUTPUT"
else
    echo "PASS: no matching collector process is running"
fi

echo
echo "### serial-device state"

if [[ -e "$SERIAL_LINK" ]]; then
    SERIAL_DEVICE="$(readlink -f "$SERIAL_LINK")"

    echo "persistent path: $SERIAL_LINK"
    echo "resolved device: $SERIAL_DEVICE"
    ls -l "$SERIAL_LINK"
    ls -l "$SERIAL_DEVICE"

    FUSER_OUTPUT="$(
        sudo fuser -v "$SERIAL_DEVICE" 2>&1
    )"
    FUSER_STATUS=$?

    if [[ "$FUSER_STATUS" -eq 0 ]]; then
        echo "WARNING: serial device is currently owned:"
        printf '%s\n' "$FUSER_OUTPUT"
        SERIAL_BUSY=1
    else
        echo "PASS: no process currently owns $SERIAL_DEVICE"
        SERIAL_BUSY=0
    fi
else
    echo "WARNING: serial device is absent"
    SERIAL_DEVICE=""
    SERIAL_BUSY=0
fi

echo
echo "### ModemManager state"

if systemctl is-active --quiet ModemManager.service; then
    echo "PASS: ModemManager is active"
else
    echo "NOTICE: ModemManager is not active"

    if [[ "$SERIAL_BUSY" -eq 0 ]]; then
        echo "Restoring ModemManager because the serial device is unowned"

        if sudo systemctl start ModemManager.service; then
            sleep 1

            if systemctl is-active --quiet ModemManager.service; then
                echo "PASS: ModemManager was restored"
            else
                echo "WARNING: ModemManager start returned without reaching active state"
            fi
        else
            echo "WARNING: failed to start ModemManager"
        fi
    else
        echo "WARNING: ModemManager was not restarted because the serial device is busy"
    fi
fi

echo
echo "### final assessment"

if [[ ! -f "$COLLECTOR_PATH" ]]; then
    echo "RESULT: collector was not installed"
    echo "IMPACT: the wrapper could not have executed a battery query"
elif [[ "$EVIDENCE_COUNT" -eq 0 ]]; then
    echo "RESULT: collector exists, but no completed JSON evidence was found"
    echo "IMPACT: completion is not proven"
    echo "NOTE: this audit did not transmit anything to the battery"
else
    echo "RESULT: one or more completed JSON evidence files exist"
fi

echo
echo "No serial port was opened by this audit."
echo "No bytes were transmitted to the battery."
echo "The audit completed without intentionally terminating the parent shell."

exit 0
