from collections import Counter
from logging import getLogger
from multiprocessing import Queue
from torch.multiprocessing import Lock, Process, Value
from typing import Callable, Dict, List, Optional, Tuple

from ..params import DeviceParams
from .context import WorkerContext
from .worker import logger_init, worker_init

logger = getLogger(__name__)


class DevicePoolExecutor:
    devices: List[DeviceParams] = None
    finished: List[Tuple[str, int]] = None
    pending: Dict[str, "Queue[WorkerContext]"] = None
    progress: Dict[str, Value] = None
    workers: Dict[str, Process] = None

    def __init__(
        self,
        devices: List[DeviceParams],
        finished_limit: int = 10,
    ):
        self.devices = devices
        self.finished = []
        self.finished_limit = finished_limit
        self.lock = Lock()
        self.pending = {}
        self.progress = {}
        self.workers = {}

        log_queue = Queue()
        logger_context = WorkerContext("logger", None, None, log_queue, None)

        logger.debug("starting log worker")
        self.logger = Process(target=logger_init, args=(self.lock, logger_context))
        self.logger.start()

        # create a pending queue and progress value for each device
        for device in devices:
            name = device.device
            cancel = Value("B", False, lock=self.lock)
            progress = Value("I", 0, lock=self.lock)
            pending = Queue()
            context = WorkerContext(name, cancel, device, pending, progress)
            self.pending[name] = pending
            self.progress[name] = pending

            logger.debug("starting worker for device %s", device)
            self.workers[name] = Process(target=worker_init, args=(self.lock, context))
            self.workers[name].start()

        logger.debug("testing log worker")
        log_queue.put("testing")

    def cancel(self, key: str) -> bool:
        """
        Cancel a job. If the job has not been started, this will cancel
        the future and never execute it. If the job has been started, it
        should be cancelled on the next progress callback.
        """
        raise NotImplementedError()

    def done(self, key: str) -> Tuple[Optional[bool], int]:
        for k, progress in self.finished:
            if key == k:
                return (True, progress)

        logger.warn("checking status for unknown key: %s", key)
        return (None, 0)

    def get_next_device(self, needs_device: Optional[DeviceParams] = None) -> int:
        # respect overrides if possible
        if needs_device is not None:
            for i in range(len(self.devices)):
                if self.devices[i].device == needs_device.device:
                    return i

        pending = [
            self.pending[d.device].qsize() for d in self.devices
        ]
        jobs = Counter(range(len(self.devices)))
        jobs.update(pending)

        queued = jobs.most_common()
        logger.debug("jobs queued by device: %s", queued)

        lowest_count = queued[-1][1]
        lowest_devices = [d[0] for d in queued if d[1] == lowest_count]
        lowest_devices.sort()

        return lowest_devices[0]

    def prune(self):
        finished_count = len(self.finished)
        if finished_count > self.finished_limit:
            logger.debug(
                "pruning %s of %s finished jobs",
                finished_count - self.finished_limit,
                finished_count,
            )
            self.finished[:] = self.finished[-self.finished_limit:]

    def submit(
        self,
        key: str,
        fn: Callable[..., None],
        /,
        *args,
        needs_device: Optional[DeviceParams] = None,
        **kwargs,
    ) -> None:
        self.prune()
        device_idx = self.get_next_device(needs_device=needs_device)
        logger.info(
            "assigning job %s to device %s: %s", key, device_idx, self.devices[device_idx]
        )

        device = self.devices[device_idx]
        queue = self.pending[device.device]
        queue.put((fn, args, kwargs))


    def status(self) -> List[Tuple[str, int, bool, int]]:
        pending = [
            (
                device.device,
                self.pending[device.device].qsize(),
                self.workers[device.device].is_alive(),
            )
            for device in self.devices
        ]
        pending.extend(self.finished)
        return pending
