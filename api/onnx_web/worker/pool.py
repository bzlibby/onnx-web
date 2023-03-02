from collections import Counter
from logging import getLogger
from queue import Empty
from threading import Thread
from typing import Any, Callable, Dict, List, Optional, Tuple

from torch.multiprocessing import Process, Queue, Value

from ..params import DeviceParams
from ..server import ServerContext
from .context import WorkerContext
from .worker import worker_main

logger = getLogger(__name__)


class DevicePoolExecutor:
    server: ServerContext
    devices: List[DeviceParams]
    max_jobs_per_worker: int
    max_pending_per_worker: int
    join_timeout: float

    context: Dict[str, WorkerContext]  # Device -> Context
    pending: Dict[str, "Queue[Tuple[str, Callable[..., None], Any, Any]]"]
    threads: Dict[str, Thread]
    workers: Dict[str, Process]

    active_jobs: Dict[str, Tuple[str, int]]  # should be Dict[Device, JobStatus]
    cancelled_jobs: List[str]
    finished_jobs: List[Tuple[str, int, bool]]  # should be List[JobStatus]
    total_jobs: Dict[str, int]  # Device -> job count

    logs: "Queue"
    progress: "Queue[Tuple[str, str, int]]"
    finished: "Queue[Tuple[str, str]]"

    def __init__(
        self,
        server: ServerContext,
        devices: List[DeviceParams],
        max_jobs_per_worker: int = 10,
        max_pending_per_worker: int = 100,
        join_timeout: float = 1.0,
    ):
        self.server = server
        self.devices = devices
        self.max_jobs_per_worker = max_jobs_per_worker
        self.max_pending_per_worker = max_pending_per_worker
        self.join_timeout = join_timeout

        self.context = {}
        self.pending = {}
        self.threads = {}
        self.workers = {}

        self.active_jobs = {}
        self.cancelled_jobs = []
        self.finished_jobs = []
        self.total_jobs = {}

        self.logs = Queue(self.max_pending_per_worker)
        self.progress = Queue(self.max_pending_per_worker)
        self.finished = Queue(self.max_pending_per_worker)

        # TODO: these should be part of a start method
        self.create_logger_worker()
        self.create_progress_worker()
        self.create_finished_worker()

        for device in devices:
            self.create_device_worker(device)

    def create_device_worker(self, device: DeviceParams) -> None:
        name = device.device

        # reuse the queue if possible, to keep queued jobs
        if name in self.pending:
            logger.debug("using existing pending job queue")
            pending = self.pending[name]
        else:
            logger.debug("creating new pending job queue")
            pending = Queue(self.max_pending_per_worker)
            self.pending[name] = pending

        context = WorkerContext(
            name,
            device,
            cancel=Value("B", False),
            progress=self.progress,
            finished=self.finished,
            logs=self.logs,
            pending=pending,
        )
        self.context[name] = context
        self.workers[name] = Process(
            name=f"onnx-web worker: {name}",
            target=worker_main,
            args=(context, self.server),
        )

        logger.debug("starting worker for device %s", device)
        self.workers[name].start()

    def create_logger_worker(self) -> None:
        def logger_worker(logs: Queue):
            logger.info("checking in from logger worker thread")

            while True:
                try:
                    job = logs.get(timeout=(self.join_timeout / 2))
                    with open("worker.log", "w") as f:
                        logger.info("got log: %s", job)
                        f.write(str(job) + "\n\n")
                except Empty:
                    pass
                except ValueError:
                    break
                except Exception as err:
                    logger.error("error in log worker: %s", err)

        logger_thread = Thread(
            name="onnx-web logger", target=logger_worker, args=(self.logs,), daemon=True
        )
        self.threads["logger"] = logger_thread

        logger.debug("starting logger worker")
        logger_thread.start()

    def create_progress_worker(self) -> None:
        def progress_worker(progress: Queue):
            logger.info("checking in from progress worker thread")
            while True:
                try:
                    job, device, value = progress.get(timeout=(self.join_timeout / 2))
                    logger.info("progress update for job: %s to %s", job, value)
                    self.active_jobs[job] = (device, value)
                    if job in self.cancelled_jobs:
                        logger.debug(
                            "setting flag for cancelled job: %s on %s", job, device
                        )
                        self.context[device].set_cancel()
                except Empty:
                    pass
                except ValueError:
                    break
                except Exception as err:
                    logger.error("error in progress worker: %s", err)

        progress_thread = Thread(
            name="onnx-web progress",
            target=progress_worker,
            args=(self.progress,),
            daemon=True,
        )
        self.threads["progress"] = progress_thread

        logger.debug("starting progress worker")
        progress_thread.start()

    def create_finished_worker(self) -> None:
        def finished_worker(finished: Queue):
            logger.info("checking in from finished worker thread")
            while True:
                try:
                    job, device = finished.get(timeout=(self.join_timeout / 2))
                    logger.info("job has been finished: %s", job)
                    context = self.context[device]
                    _device, progress = self.active_jobs[job]
                    self.finished_jobs.append((job, progress, context.cancel.value))
                    del self.active_jobs[job]
                except Empty:
                    pass
                except ValueError:
                    break
                except Exception as err:
                    logger.error("error in finished worker: %s", err)

        finished_thread = Thread(
            name="onnx-web finished",
            target=finished_worker,
            args=(self.finished,),
            daemon=True,
        )
        self.threads["finished"] = finished_thread

        logger.debug("started finished worker")
        finished_thread.start()

    def get_job_context(self, key: str) -> WorkerContext:
        device, _progress = self.active_jobs[key]
        return self.context[device]

    def get_next_device(self, needs_device: Optional[DeviceParams] = None) -> int:
        # respect overrides if possible
        if needs_device is not None:
            for i in range(len(self.devices)):
                if self.devices[i].device == needs_device.device:
                    return i

        jobs = Counter(range(len(self.devices)))
        jobs.update([self.pending[d.device].qsize() for d in self.devices])

        queued = jobs.most_common()
        logger.debug("jobs queued by device: %s", queued)

        lowest_count = queued[-1][1]
        lowest_devices = [d[0] for d in queued if d[1] == lowest_count]
        lowest_devices.sort()

        return lowest_devices[0]

    def cancel(self, key: str) -> bool:
        """
        Cancel a job. If the job has not been started, this will cancel
        the future and never execute it. If the job has been started, it
        should be cancelled on the next progress callback.
        """

        self.cancelled_jobs.append(key)

        if key not in self.active_jobs:
            logger.debug("cancelled job has not been started yet: %s", key)
            return True

        device, _progress = self.active_jobs[key]
        logger.info("cancelling job %s, active on device %s", key, device)

        context = self.context[device]
        context.set_cancel()

        return True

    def done(self, key: str) -> Tuple[Optional[bool], int]:
        """
        Check if a job has been finished and report the last progress update.

        If the job is still active or pending, the first item will be False.
        If the job is not finished or active, the first item will be None.
        """
        for k, p, c in self.finished_jobs:
            if k == key:
                return (True, p)

        if key not in self.active_jobs:
            logger.warn("checking status for unknown job: %s", key)
            return (None, 0)

        _device, progress = self.active_jobs[key]
        return (False, progress)

    def join(self):
        logger.info("stopping worker pool")

        logger.debug("closing queues")
        self.logs.close()
        self.finished.close()
        self.progress.close()
        for queue in self.pending.values():
            queue.close()

        self.pending.clear()

        logger.debug("stopping device workers")
        for device, worker in self.workers.items():
            if worker.is_alive():
                logger.debug("stopping worker for device %s", device)
                worker.join(self.join_timeout)
                if worker.is_alive():
                    logger.warning(
                        "worker for device %s could not be stopped in time", device
                    )
            else:
                logger.debug("worker for device %s has died", device)

        for name, thread in self.threads.items():
            logger.debug("stopping worker thread: %s", name)
            thread.join(self.join_timeout)

        logger.debug("worker pool stopped")

    def recycle(self):
        logger.debug("recycling worker pool")
        needs_restart = []

        for device, proc in self.workers.items():
            jobs = self.total_jobs.get(device, 0)
            if not proc.is_alive():
                logger.warning("worker for device %s has died", device)
                needs_restart.append(device)
            elif jobs > self.max_jobs_per_worker:
                logger.info(
                    "shutting down worker for device %s after %s jobs", device, jobs
                )
                proc.join(self.join_timeout)
                if proc.is_alive():
                    logger.warning(
                        "worker for device %s could not be recycled in time", device
                    )

                self.workers[device] = None
                del proc
                needs_restart.append(device)
            else:
                logger.debug(
                    "worker for device %s does not need to be recycled", device
                )

        logger.debug("starting new workers")

        for device in self.devices:
            if device.device in needs_restart:
                self.create_device_worker(device)
                self.total_jobs[device.device] = 0

        logger.debug("worker pool recycled")

    def submit(
        self,
        key: str,
        fn: Callable[..., None],
        /,
        *args,
        needs_device: Optional[DeviceParams] = None,
        **kwargs,
    ) -> None:
        device_idx = self.get_next_device(needs_device=needs_device)
        logger.info(
            "assigning job %s to device %s: %s",
            key,
            device_idx,
            self.devices[device_idx],
        )

        device = self.devices[device_idx].device

        if device in self.total_jobs:
            self.total_jobs[device] += 1
        else:
            self.total_jobs[device] = 1

        logger.debug("device job count: %s", self.total_jobs[device])
        self.recycle()

        self.pending[device].put((key, fn, args, kwargs), block=False)

    def status(self) -> List[Tuple[str, int, bool, bool]]:
        history = [
            (name, progress, False, name in self.cancelled_jobs)
            for name, (_device, progress) in self.active_jobs.items()
        ]
        history.extend(
            [
                (
                    name,
                    progress,
                    True,
                    cancel,
                )
                for name, progress, cancel in self.finished_jobs
            ]
        )
        return history
