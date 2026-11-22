#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""PSR identification script for AMD systems"""

import os
import sys

PSR_SUPPORT = {
    0: "PSR Unsupported",
    1: "PSR 1",
    2: "PSR 2 (eDP 1.4)",
    3: "PSR 2 with Y coordinates (eDP 1.4a)",
    4: "PSR 2 with Y coordinates (eDP 1.4b or eDP 1.5)",
}

TCON = {0x001CF8: "Parade"}


def decode_psr_support(f):
    f.seek(0x70)
    v = int.from_bytes(f.read(1), "little")
    print(f"○ {PSR_SUPPORT[v]} [{v}]")


def get_id_string(f):
    f.seek(0x400)
    oui = f.read(3)
    id = f.read(2)
    f.seek(0x40F)
    resv_40f = f.read(1)
    v = int.from_bytes(oui, "big")
    if v in TCON:
        oui_str = TCON[v]
    else:
        oui_str = "-".join(f"{c:02x}" for c in oui)
    print(f"○ Sink OUI: {oui_str}")
    print("○ resv_40f: " + ":".join(f"{c:02x}" for c in resv_40f))
    print("○ ID String: " + "-".join(f"{c:02x}" for c in reversed(id)))


def get_psr_error(f):
    f.seek(0x2006)
    err = f.read(3)
    print("○ PSR Status: " + "-".join(f"{c:02x}" for c in err))


def get_dmcub():
    base = "/sys/kernel/debug/dri"
    for num in range(3):
        fw_info = os.path.join(base, f"{num}", "amdgpu_firmware_info")
        if not os.path.exists(fw_info):
            continue
        with open(fw_info, "r") as f:
            for line in f.read().split("\n"):
                if "DMCUB" in line:
                    print(f"DRI device {num} DMCUB F/W version: {line.split()[-1]}")


def discover_gpu():
    gpus = []
    try:
        from pyudev import Context
    except ModuleNotFoundError:
        sys.exit("Missing pyudev, please install")
    context = Context()
    for dev in context.list_devices(subsystem="drm_dp_aux_dev"):
        if not "eDP" in dev.sys_path:
            continue
        gpus += [dev.device_node]
    return gpus


if __name__ == "__main__":
    gpus = discover_gpu()
    if not gpus:
        sys.exit("failed to find drm_dp_aux_dev")
    get_dmcub()
    for gpu in gpus:
        try:
            with open(gpu, "rb") as f:
                try:
                    decode_psr_support(f)
                    get_id_string(f)
                    get_psr_error(f)
                except OSError:
                    print(
                        "Could not read DPCD, skipping. If the panel is off, please turn on and try again."
                    )
                    continue
        except PermissionError:
            sys.exit("run as root")
