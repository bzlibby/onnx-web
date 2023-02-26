from logging import getLogger
from torch.multiprocessing import Lock
from time import sleep

from .context import WorkerContext

logger = getLogger(__name__)

def logger_init(lock: Lock, context: WorkerContext):
    logger.info("checking in from logger")

    with open("worker.log", "w") as f:
        while True:
            if context.pending.empty():
                logger.info("no logs, sleeping")
                sleep(5)
            else:
                job = context.pending.get()
                logger.info("got log: %s", job)
                f.write(str(job) + "\n\n")


def worker_init(lock: Lock, context: WorkerContext):
    logger.info("checking in from worker")

    while True:
        if context.pending.empty():
            logger.info("no jobs, sleeping")
            sleep(5)
        else:
            job = context.pending.get()
            logger.info("got job: %s", job)
