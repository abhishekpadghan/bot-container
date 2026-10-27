#!/usr/bin/env python

import subprocess
import sys
import time

MACHINE = "podman-machine-default"
RETRIES = 15
WAIT = 2


def run(cmd):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )


def main():
    # Check machine state
    result = run([
        "podman", "machine", "inspect",
        MACHINE,
        "--format", "{{.State}}"
    ])

    if result.returncode != 0:
        print(f"ERROR: Podman machine '{MACHINE}' not found.")
        print("Create it once with: podman machine init")
        sys.exit(1)

    state = result.stdout.strip()

    if state != "running":
        print("Podman machine is stopped. Starting...")
        result = run(["podman", "machine", "start", MACHINE])

        if result.returncode != 0:
            print("ERROR: Failed to start Podman machine.")
            print(result.stderr.strip())
            sys.exit(1)
    else:
        print("Podman machine: already running")

    # Wait until Podman responds
    print("Checking Podman connection...")

    for _ in range(RETRIES):
        result = run(["podman", "info"])

        if result.returncode == 0:
            print("Podman connection: OK")
            return

        time.sleep(WAIT)

    print("ERROR: Podman is not responding.")
    sys.exit(1)


if __name__ == "__main__":
    main()