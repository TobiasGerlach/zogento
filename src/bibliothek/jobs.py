"""Hintergrundjobs für die Web-Oberfläche (OCR, Aufnahme, Embeddings).

Ein einziger Arbeiter-Thread: OCR und Embeddings teilen sich CPU, GPU und
Neural Engine, parallel laufen sie nur langsamer. Jobs werden nacheinander
abgearbeitet; der Zustand lebt im Speicher und verschwindet mit dem Programm.
"""
from __future__ import annotations

import datetime as dt
import threading
import traceback
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

Progress = Callable[[str], None]

LAEUFT, WARTET, FERTIG, FEHLER = "läuft", "wartet", "fertig", "fehler"


@dataclass
class Job:
    id: str
    titel: str
    art: str = ""
    status: str = WARTET
    log: deque = field(default_factory=lambda: deque(maxlen=400))
    ergebnis: dict = field(default_factory=dict)
    fehler: str = ""
    begonnen: str = ""
    beendet: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "titel": self.titel, "art": self.art, "status": self.status,
            "log": list(self.log), "ergebnis": self.ergebnis, "fehler": self.fehler,
            "begonnen": self.begonnen, "beendet": self.beendet,
        }


class JobRunner:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bibliothek-job")

    def submit(self, titel: str, fn: Callable[[Job, Progress], dict | None], *, art: str = "",
               einmalig: bool = False) -> Job:
        """fn(job, progress) läuft im Hintergrund; sein Rückgabewert landet in job.ergebnis.

        einmalig: wartet schon ein Job derselben Art, wird kein zweiter angelegt.
        """
        with self._lock:
            if einmalig and art:
                for jid in self._order:
                    j = self._jobs[jid]
                    if j.art == art and j.status == WARTET:
                        return j
            job = Job(uuid.uuid4().hex[:12], titel, art)
            self._jobs[job.id] = job
            self._order.append(job.id)

        def progress(message: str) -> None:
            job.log.append(f"{dt.datetime.now():%H:%M:%S}  {message}")

        def run() -> None:
            job.status = LAEUFT
            job.begonnen = dt.datetime.now().strftime("%H:%M:%S")
            try:
                job.ergebnis = fn(job, progress) or {}
                job.status = FERTIG
            except Exception as exc:  # Fehler gehören in den Job, nicht in den Thread
                job.fehler = str(exc) or exc.__class__.__name__
                job.log.append(traceback.format_exc(limit=3).rstrip())
                job.status = FEHLER
            finally:
                job.beendet = dt.datetime.now().strftime("%H:%M:%S")

        self._pool.submit(run)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return [self._jobs[j] for j in reversed(self._order)]

    def active(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j.status in (LAEUFT, WARTET))

    def wait(self, timeout: float | None = None) -> None:
        """Für Tests: blockiert, bis alle bisher eingereichten Jobs fertig sind."""
        done = threading.Event()
        self._pool.submit(done.set)
        done.wait(timeout)
