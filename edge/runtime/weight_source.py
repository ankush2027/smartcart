"""Explicit development input. Stage 11 can implement the same poll/close API."""
from datetime import datetime
import math
import os
import select
import sys
from typing import Protocol

from edge.weight.weight import WeightSensor
from edge.weight.weight_event import WeightEvent, detect_weight_change


class WeightSource(Protocol):
    def poll(self) -> WeightEvent | None: ...
    def close(self) -> None: ...


class SimulatedWeightSource:
    """Convert injected signed gram changes through the existing weight boundary."""
    def __init__(self, initial_weight=500.0, clock=datetime.now):
        if not math.isfinite(initial_weight) or initial_weight < 0:
            raise ValueError('Initial simulated weight must be finite and nonnegative')
        self.clock = clock
        self.sensor = WeightSensor()
        self.sensor.current_weight = initial_weight
        self.baseline = self.sensor.read_weight()

    def inject_delta(self, delta):
        if not math.isfinite(delta):
            raise ValueError('Weight change must be finite')
        if self.sensor.read_weight() + delta < 0:
            raise ValueError('Simulated weight cannot become negative')
        self.sensor.simulate_change(delta)
        current = self.sensor.read_weight()
        event = detect_weight_change(self.baseline, current)
        if event is not None:
            event.timestamp = self.clock()
            self.baseline = current
        return event

    def close(self):
        pass


class StdinSimulatedWeightSource(SimulatedWeightSource):
    """Nonblocking terminal polling; enter one signed gram delta per line."""
    def __init__(self, initial_weight=500.0, clock=datetime.now):
        super().__init__(initial_weight, clock)
        self._pending = ''

    def poll(self):
        # readline() can block on a partial pipe line even after select().
        # Read available bytes only so input never stalls camera acquisition.
        if '\n' not in self._pending:
            readable, _, _ = select.select([sys.stdin], [], [], 0)
            if not readable:
                return None
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk:
                raise EOFError('Simulation input closed')
            self._pending += chunk.decode('ascii')
            if len(self._pending) > 8192:
                raise ValueError('Simulation input line too long')
        if '\n' not in self._pending:
            return None
        line, self._pending = self._pending.split('\n', 1)
        return self.inject_delta(float(line.strip()))
