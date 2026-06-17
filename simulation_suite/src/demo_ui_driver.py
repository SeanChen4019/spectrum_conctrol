from __future__ import annotations

import argparse
import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from common import TIMELINE_DIR, ensure_output_dirs, iter_scenarios, load_config
from generate_jammer_data import generate_scenario_timeline, read_timeline, write_timeline


TX_URL = "http://127.0.0.1:5001/api/data"
RX_URL = "http://127.0.0.1:5000/api/data"
JAMMER_HOST = "127.0.0.1"
JAMMER_PORT = 5555


class JsonHttpPoster:
    def __init__(self, url: str, timeout: float = 0.25):
        self.url = url
        self.timeout = timeout
        self.ok = 0
        self.fail = 0

    def post(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read(32)
            self.ok += 1
        except (urllib.error.URLError, TimeoutError, OSError):
            self.fail += 1


class JammerTelemetryClient:
    def __init__(self, host: str, port: int, timeout: float = 0.25):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.ok = 0
        self.fail = 0

    def send(self, payload: dict[str, Any]) -> None:
        if self.sock is None:
            self._connect()
        if self.sock is None:
            self.fail += 1
            return
        try:
            self.sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            self.ok += 1
        except OSError:
            self.close()
            self.fail += 1

    def _connect(self) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            sock.settimeout(self.timeout)
            self.sock = sock
        except OSError:
            self.sock = None

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


def ensure_timeline(scenario: dict[str, Any], global_cfg: dict[str, Any]) -> Path:
    path = TIMELINE_DIR / f"{scenario['id']}.jsonl"
    if not path.exists():
        timeline = generate_scenario_timeline(scenario, global_cfg, strategy="智能决策策略")
        write_timeline(timeline, path)
    return path


def play_timeline(path: Path, speed: float, dry_run: bool, pause_between_scenes: float,
                  no_jammer: bool = False, no_tx: bool = False, no_rx: bool = False) -> dict[str, int]:
    records = read_timeline(path)
    if not records:
        return {"frames": 0, "tx_ok": 0, "rx_ok": 0, "jammer_ok": 0, "tx_fail": 0, "rx_fail": 0, "jammer_fail": 0}

    tx = None if no_tx else JsonHttpPoster(TX_URL)
    rx = None if no_rx else JsonHttpPoster(RX_URL)
    jammer = None if no_jammer else JammerTelemetryClient(JAMMER_HOST, JAMMER_PORT)
    interval = records[1]["time_s"] - records[0]["time_s"] if len(records) > 1 else 0.12
    sleep_s = max(0.0, interval / max(speed, 0.01))

    targets = []
    if not no_tx: targets.append("TX")
    if not no_rx: targets.append("RX")
    if not no_jammer: targets.append("Jammer")
    print(f"[PLAY] {records[0]['short_name']} | {len(records)} frames | dry_run={dry_run} | targets={','.join(targets) or 'none'}")
    for rec in records:
        payloads = rec["ui_payloads"]
        if not dry_run:
            if tx: tx.post(payloads["tx"])
            if rx: rx.post(payloads["rx"])
            if jammer: jammer.send(payloads["jammer"])
        if rec["frame"] % 40 == 0:
            print(
                f"  t={rec['time_s']:5.2f}s snr={rec['metrics']['snr_db']:5.2f}dB "
                f"mode={rec['decision']['anti_jamming_mode_name']} task={rec['decision']['tx_task']}"
            )
        time.sleep(sleep_s)

    if jammer: jammer.close()
    if pause_between_scenes > 0:
        time.sleep(pause_between_scenes)
    return {
        "frames": len(records),
        "tx_ok": tx.ok if tx else 0,
        "rx_ok": rx.ok if rx else 0,
        "jammer_ok": jammer.ok if jammer else 0,
        "tx_fail": tx.fail if tx else 0,
        "rx_fail": rx.fail if rx else 0,
        "jammer_fail": jammer.fail if jammer else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay simulation timelines into the existing three PyQt UIs.")
    parser.add_argument("--scenario", default="all", help="Scenario id or 'all'.")
    parser.add_argument("--speed", type=float, default=1.0, help="Replay speed multiplier.")
    parser.add_argument("--dry-run", action="store_true", help="Read and print timelines without sending to UI.")
    parser.add_argument("--loop", action="store_true", help="Loop forever for long screen recording.")
    parser.add_argument("--pause-between-scenes", type=float, default=1.0)
    parser.add_argument("--no-jammer", action="store_true", help="Skip pushing telemetry to Jammer UI (5555).")
    parser.add_argument("--no-tx", action="store_true", help="Skip pushing data to TX UI (5001).")
    parser.add_argument("--no-rx", action="store_true", help="Skip pushing data to RX UI (5000).")
    args = parser.parse_args()

    config = load_config()
    ensure_output_dirs()
    scenarios = iter_scenarios(config, args.scenario)

    round_idx = 0
    while True:
        round_idx += 1
        total = {"frames": 0, "tx_ok": 0, "rx_ok": 0, "jammer_ok": 0, "tx_fail": 0, "rx_fail": 0, "jammer_fail": 0}
        print(f"[ROUND] {round_idx}")
        for scenario in scenarios:
            path = ensure_timeline(scenario, config["global"])
            stats = play_timeline(path, args.speed, args.dry_run, args.pause_between_scenes,
                                  no_jammer=args.no_jammer, no_tx=args.no_tx, no_rx=args.no_rx)
            for key, value in stats.items():
                total[key] += value
        print(
            "[SUMMARY] frames={frames} tx={tx_ok}/{tx_fail} rx={rx_ok}/{rx_fail} jammer={jammer_ok}/{jammer_fail}".format(
                **total
            )
        )
        if not args.loop:
            break


if __name__ == "__main__":
    main()
