"""Runs seed work for the launcher. One task or stream at a time per Rune."""

import asyncio
import time
from typing import Any

from charms_core import protocol
from charms_core.seed import Seed
from pydantic import BaseModel, ValidationError


class SeedExecutor:
    def __init__(self, seed: Seed[Any, Any, Any]) -> None:
        self._seed = seed
        self._current: asyncio.Task[None] | None = None
        self._current_task_id: str | None = None

    @property
    def busy(self) -> bool:
        return self._current is not None and not self._current.done()

    def track(self, task_id: str, runner: asyncio.Task[None]) -> None:
        self._current = runner
        self._current_task_id = task_id

    def cancel(self, task_id: str) -> None:
        if self._current_task_id == task_id and self._current is not None:
            self._current.cancel()

    async def run_task(
        self, task_id: str, input_data: dict[str, Any], config: dict[str, Any] | None
    ) -> protocol.TaskResultMsg | protocol.TaskErrorMsg:
        started = time.perf_counter()
        # Unit-priced seeds (tokens, …) set this during run(); reset per task.
        self._seed.billable_units = None

        def compute_ms() -> float:
            return (time.perf_counter() - started) * 1000

        try:
            input_obj = self._seed.input_model(**input_data)
            config_obj = (
                self._seed.config_model(**config)
                if config is not None and self._seed.config_model is not None
                else None
            )
            output: BaseModel = await self._seed.run(input_obj, config_obj)
            return protocol.TaskResultMsg(
                task_id=task_id,
                output=output.model_dump(mode="json"),
                compute_ms=compute_ms(),
                billable_units=self._seed.billable_units,
            )
        except asyncio.CancelledError:
            raise
        except ValidationError as exc:
            return protocol.TaskErrorMsg(
                task_id=task_id, error=f"invalid input: {exc}", compute_ms=compute_ms()
            )
        except Exception as exc:
            return protocol.TaskErrorMsg(
                task_id=task_id,
                error=str(exc),
                compute_ms=compute_ms(),
                billable_units=self._seed.billable_units,
            )
