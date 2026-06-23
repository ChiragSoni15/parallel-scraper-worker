"""Subprocess manager: launch and supervise `python -m parallel_scraper` runs."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ManagedRun:
    run_id: str
    pid: int
    started_at: float
    cmd: list[str]
    cwd: str
    process: subprocess.Popen = field(repr=False)
    log_reader: Optional[threading.Thread] = field(default=None, repr=False)
    log_handlers: list[logging.Handler] = field(default_factory=list, repr=False)
    stop_requested: bool = False
    exit_code: Optional[int] = None
    error_msg: Optional[str] = None


class RunRegistry:
    """In-memory registry of subprocess-launched runs.

    The frontend uses this to start/stop runs; the on-disk run_dir is the source
    of truth for state and progress (heartbeat.jsonl, state_*.csv).
    """

    def __init__(self) -> None:
        self._runs: dict[str, ManagedRun] = {}
        self._lock = threading.Lock()

    def list_active(self) -> list[ManagedRun]:
        with self._lock:
            return [r for r in self._runs.values() if r.exit_code is None]

    def get(self, run_id: str) -> Optional[ManagedRun]:
        with self._lock:
            return self._runs.get(run_id)

    def start(
        self,
        run_id: str,
        args: list[str],
        cwd: str | Path,
        env: Optional[dict[str, str]] = None,
    ) -> ManagedRun:
        """Launch `python -m parallel_scraper <args>` as a subprocess."""
        import time

        cmd = [sys.executable, "-m", "parallel_scraper", *args]
        cwd_str = str(cwd)
        full_env = os.environ.copy()
        if env:
            full_env.update(env)

        # Prepend src/ to PYTHONPATH for editable-install-free dev runs
        src_dir = Path(__file__).resolve().parents[2]
        prev = full_env.get("PYTHONPATH", "")
        full_env["PYTHONPATH"] = (str(src_dir) + os.pathsep + prev) if prev else str(src_dir)

        # Capture child stdout+stderr to a capped log so silent crashes are
        # debuggable without letting subprocess.log grow unbounded.
        log_dir = self._output_dir_from_args(args, cwd_str) / run_id
        log_dir.mkdir(parents=True, exist_ok=True)
        proc_logger, proc_handlers = self._make_subprocess_logger(run_id, log_dir)
        proc = subprocess.Popen(
            cmd,
            cwd=cwd_str,
            env=full_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,  # type: ignore
        )
        reader = threading.Thread(
            target=self._drain_subprocess_output,
            args=(proc.stdout, proc_logger),
            name=f"subprocess-log[{run_id}]",
            daemon=True,
        )
        reader.start()
        managed = ManagedRun(
            run_id=run_id,
            pid=proc.pid,
            started_at=time.time(),
            cmd=cmd,
            cwd=cwd_str,
            process=proc,
            log_reader=reader,
            log_handlers=proc_handlers,
        )
        with self._lock:
            self._runs[run_id] = managed
        threading.Thread(target=self._reap, args=(managed,), daemon=True).start()
        logger.info("runner.started run_id=%s pid=%d", run_id, proc.pid)
        return managed

    @staticmethod
    def _output_dir_from_args(args: list[str], cwd: str) -> Path:
        try:
            idx = args.index("--output-dir")
            return Path(args[idx + 1]).resolve()
        except Exception:
            return Path(cwd) / "outputs"

    @staticmethod
    def _make_subprocess_logger(run_id: str, log_dir: Path) -> tuple[logging.Logger, list[logging.Handler]]:
        proc_logger = logging.getLogger(f"parallel_scraper.subprocess.{run_id}.{id(log_dir)}")
        proc_logger.handlers.clear()
        proc_logger.propagate = False
        proc_logger.setLevel(logging.INFO)
        handler = RotatingFileHandler(
            log_dir / "subprocess.log",
            maxBytes=25 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        proc_logger.addHandler(handler)
        return proc_logger, [handler]

    @staticmethod
    def _drain_subprocess_output(stream, proc_logger: logging.Logger) -> None:
        if stream is None:
            return
        try:
            for line in stream:
                try:
                    proc_logger.info(line.rstrip("\r\n"))
                except Exception:
                    # Keep draining the pipe even if the file sink is wedged.
                    continue
        except Exception:
            logger.debug("runner.subprocess_log_reader_failed", exc_info=True)
            try:
                for _ in stream:
                    pass
            except Exception:
                pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def stop(self, run_id: str) -> bool:
        run = self.get(run_id)
        if not run:
            return False
        if run.exit_code is not None:
            return False
        run.stop_requested = True
        try:
            if os.name == "nt":
                run.process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore
            else:
                run.process.send_signal(signal.SIGINT)
            return True
        except Exception:
            logger.exception("runner.stop_failed run_id=%s", run_id)
            return False

    def _reap(self, run: ManagedRun) -> None:
        try:
            run.exit_code = run.process.wait()
            if run.log_reader is not None:
                run.log_reader.join(timeout=2)
            for handler in run.log_handlers:
                try:
                    handler.close()
                except Exception:
                    pass
            logger.info("runner.exited run_id=%s pid=%d code=%s",
                        run.run_id, run.pid, run.exit_code)
        except Exception as e:
            run.error_msg = str(e)
            logger.exception("runner.reap_failed run_id=%s", run.run_id)
