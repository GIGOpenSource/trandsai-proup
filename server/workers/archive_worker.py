"""可选：循环消费 agent:archive:queue（C4）。"""
from __future__ import annotations

import logging
import time

from services.archive_process import process_archive_job
from services.archive_queue import archive_queue_enabled, pop_archive_job

logger = logging.getLogger(__name__)


def run_forever(poll_s: float = 1.0) -> None:
    if not archive_queue_enabled():
        logger.error("ARCHIVE_QUEUE / Redis not enabled")
        return
    logger.info("archive worker started")
    while True:
        job = pop_archive_job(timeout=max(1, int(poll_s)))
        if not job:
            time.sleep(0.05)
            continue
        process_archive_job(job)
