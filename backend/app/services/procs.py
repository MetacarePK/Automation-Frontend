"""Find and stop every process a run started (the runner and all its browsers).

A browser that outlives its run keeps using memory and may keep an Office Ally
session open, so cancelling or finishing a run must verify that nothing is left.
Descendants are snapshotted while the run is alive (an orphaned browser has no
parent left to find it from), identified by (pid, create_time) so a reused pid is
never killed by mistake.
"""

import threading

import psutil


class ProcessTracker:
    def __init__(self, root_pid: int):
        self.root_pid = root_pid
        self._seen: dict[int, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.snapshot()

    def snapshot(self) -> None:
        try:
            root = psutil.Process(self.root_pid)
            procs = [root] + root.children(recursive=True)
        except psutil.Error:
            return
        with self._lock:
            for p in procs:
                try:
                    self._seen.setdefault(p.pid, p.create_time())
                except psutil.Error:
                    pass

    def start_monitor(self, interval: float = 2.0) -> None:
        def loop():
            while not self._stop.wait(interval):
                self.snapshot()
        self._thread = threading.Thread(target=loop, name=f"proc-monitor-{self.root_pid}", daemon=True)
        self._thread.start()

    def stop_monitor(self) -> None:
        self._stop.set()

    def alive(self) -> list[psutil.Process]:
        with self._lock:
            seen = dict(self._seen)
        out = []
        for pid, created in seen.items():
            try:
                p = psutil.Process(pid)
                if p.create_time() == created and p.status() != psutil.STATUS_ZOMBIE:
                    out.append(p)
            except psutil.Error:
                continue
        return out

    def kill_all(self, grace: float = 5.0) -> list[int]:
        """Stop everything this run started. Returns the pids that could NOT be stopped (normally [])."""
        self.snapshot()
        victims = self.alive()
        for p in victims:
            try:
                p.terminate()
            except psutil.Error:
                pass
        _, still = psutil.wait_procs(victims, timeout=grace)
        for p in still:
            try:
                p.kill()
            except psutil.Error:
                pass
        _, survivors = psutil.wait_procs(still, timeout=3)
        self.stop_monitor()
        return [p.pid for p in survivors if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]
