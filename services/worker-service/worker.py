"""
services/worker-service/worker.py

A background job worker that processes tasks in a loop.
Simulates real failure patterns: memory leaks, job timeouts, queue backlogs.
"""

import json
import logging
import os
import random
import time
from datetime import datetime


class JSONFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "service": "worker-service",
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        if hasattr(record, "extra"):
            entry.update(record.extra)
        return json.dumps(entry)


logging.basicConfig(level=logging.INFO)
for h in logging.root.handlers:
    h.setFormatter(JSONFormatter())

logger = logging.getLogger("worker-service")

FAIL_EVERY_N = int(os.environ.get("FAIL_EVERY_N_JOBS", "5"))
job_count = 0


def process_job(job_id: int):
    global job_count
    job_count += 1

    # Simulate occasional failures
    if job_count % FAIL_EVERY_N == 0:
        failure = random.choice([
            "out_of_memory",
            "job_timeout",
            "dependency_unavailable",
        ])
        logger.error(
            "Job failed",
            extra={"job_id": job_id, "failure_reason": failure, "jobs_processed": job_count}
        )
        raise RuntimeError(failure)

    duration = random.uniform(0.2, 1.5)
    time.sleep(duration)
    logger.info(
        "Job completed",
        extra={"job_id": job_id, "duration_ms": round(duration * 1000), "jobs_processed": job_count}
    )


def main():
    logger.info("Worker service starting", extra={"fail_every_n": FAIL_EVERY_N})
    job_id = 0

    while True:
        job_id += 1
        try:
            process_job(job_id)
        except Exception as e:
            logger.warning("Retrying after failure", extra={"job_id": job_id, "error": str(e)})
            time.sleep(2)

        time.sleep(random.uniform(0.5, 2.0))


if __name__ == "__main__":
    main()
