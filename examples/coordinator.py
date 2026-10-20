"""Minimal coordinator: start the pool, print status, run a job on whichever PC is free.

    python examples/coordinator.py devices.json

devices.json: copy examples/devices.example.json and fill in your machines.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis_pool import Pool  # noqa: E402
from jarvis_pool.devices import load_registry  # noqa: E402


def main() -> int:
    registry_path = Path(sys.argv[1] if len(sys.argv) > 1 else "devices.json")
    pool = Pool(lambda: load_registry(registry_path), data_dir=Path("data"), notify=print)
    pool.start()
    try:
        time.sleep(3)  # first heartbeat round
        print(pool.status_text())
        # Runs now if a machine with Ollama is up; otherwise wakes one and queues the job.
        job = pool.enqueue("llm.chat", {"malli": "llama3.2",
                                        "viestit": [{"role": "user", "content": "Hello!"}]},
                           description="hello")
        print(pool.queue_text())
        while pool.queue and (pool.queue.hae(job["job_id"]) or {}).get("tila") in (
                "jonossa", "odottaa_heratysta", "kaynnissa"):
            time.sleep(2)
    finally:
        pool.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
