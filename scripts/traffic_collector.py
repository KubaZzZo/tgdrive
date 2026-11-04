"""Run on the Linux HOST, never inside the application container."""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = timezone.utc  # noqa: UP017 - Ubuntu 22.04 system Python is 3.10.


def read_interfaces(proc: Path = Path("/proc"), selected: list[str] | None = None) -> dict:
    counters = {}
    for line in (proc / "net/dev").read_text().splitlines()[2:]:
        name, values = line.split(":", 1)
        fields = values.split()
        counters[name.strip()] = [int(fields[0]), int(fields[8])]
    if selected is None:
        selected = sorted(
            {
                fields[0]
                for line in (proc / "net/route").read_text().splitlines()[1:]
                if len(fields := line.split()) >= 4
                and fields[1] == "00000000"
                and int(fields[3], 16) & 1
            }
        )
    if not selected or any(name not in counters or name == "lo" for name in selected):
        raise RuntimeError("Cannot identify uplink interfaces; specify --interfaces eth0[,eth1]")
    return {name: counters[name] for name in sorted(set(selected))}


def sample(previous: dict | None, counters: dict, boot_id: str, now: datetime) -> dict:
    day = now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    stamp = now.astimezone(UTC).isoformat()
    if previous is None:
        previous = {"started_at": stamp, "days": {}, "incomplete_days": [day], "gaps": 0}
    # Copy so a failed atomic write cannot modify the previous checkpoint.
    state = json.loads(json.dumps(previous))
    daily = state["days"].setdefault(day, {"inbound": 0, "outbound": 0})
    old = state.get("counters", {})
    incomplete = False
    if old:
        last = datetime.fromisoformat(state["updated_at"])
        last_day = last.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        reboot = state["boot_id"] != boot_id
        # A gap across midnight cannot be apportioned accurately from cumulative counters.
        incomplete = reboot or (last_day != day and (now - last).total_seconds() > 15)
        for name, values in counters.items():
            if name not in old:
                incomplete = True
                continue  # Establish a baseline for newly selected interfaces.
            for index, direction in enumerate(("inbound", "outbound")):
                reset = reboot or values[index] < old[name][index]
                delta = values[index] if reset else values[index] - old[name][index]
                daily[direction] += delta
                incomplete = incomplete or reset
        if set(old) != set(counters):
            incomplete = True
        if incomplete:
            state["gaps"] += 1
            state["incomplete_days"] = sorted(set(state["incomplete_days"]) | {day, last_day})
    cutoff = (now.astimezone(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=34)).isoformat()
    state["days"] = {key: val for key, val in state["days"].items() if key >= cutoff}
    state["incomplete_days"] = [key for key in state["incomplete_days"] if key >= cutoff]
    state.update(
        updated_at=stamp,
        timezone="Asia/Shanghai",
        interfaces=list(counters),
        counters=counters,
        boot_id=boot_id,
        version=1,
    )
    return state


def collect(state_file: Path, interfaces: list[str] | None = None) -> None:
    # Fail on corrupt state rather than silently resetting an existing day's usage.
    previous = json.loads(state_file.read_text()) if state_file.exists() else None
    state = sample(
        previous,
        read_interfaces(selected=interfaces),
        Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        datetime.now(UTC),
    )
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_file.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    temporary.chmod(0o644)
    temporary.replace(state_file)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument(
        "--interfaces", help="Comma-separated HOST uplink names; default: IPv4 default routes"
    )
    args = parser.parse_args()
    # Keep one writer. A second process fails instead of overwriting checkpoints.
    import fcntl

    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            collect(args.state, args.interfaces.split(",") if args.interfaces else None)
            time.sleep(5)


if __name__ == "__main__":
    main()
