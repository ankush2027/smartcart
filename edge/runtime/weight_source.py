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
        self._last_event = None

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
            self._last_event = event
        return event

    def poll(self):
        event, self._last_event = self._last_event, None
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
        line = line.strip()
        if not line:
            return None  # Blank record is not a delta or EOF.
        return self.inject_delta(float(line))


class SerialWeightSource:
    """
    Structural boundary shell for future physical load-cell integration:
        Load Cell -> HX711 -> Arduino Mega -> USB Serial -> SerialWeightSource -> WeightEvent

    WARNING: Physical load-cell/HX711 input is NOT implemented in this milestone.
    This class defines the dependency boundary only. It intentionally contains NO
    guessed serial protocol, baud rate, calibration, or filtering logic.
    """

    def __init__(self, port: str | None = None, baudrate: int | None = None) -> None:
        if not port:
            raise NotImplementedError(
                "SerialWeightSource: physical load-cell/HX711 hardware is not implemented "
                "in this milestone. Port configuration is required for future hardware integration."
            )
        # Even if a port string is passed, do not pretend hardware is functional
        raise NotImplementedError(
            "SerialWeightSource: physical load-cell/HX711 hardware driver is not implemented "
            "in this milestone. Use SimulatedWeightSource or StdinSimulatedWeightSource for software testing."
        )

    def poll(self) -> WeightEvent | None:
        raise NotImplementedError("Physical sensor hardware not connected")

    def close(self) -> None:
        pass


def create_weight_source(source_type: str = "simulated", **kwargs) -> WeightSource:
    """
    Factory for constructing WeightSource implementations.

    Supported types:
    - 'simulated' (default): in-memory SimulatedWeightSource
    - 'stdin': StdinSimulatedWeightSource for interactive manual input
    - 'serial': SerialWeightSource (fails clearly as hardware is not yet implemented)
    """
    if source_type == "simulated":
        return SimulatedWeightSource(
            initial_weight=kwargs.get("initial_weight", 500.0),
            clock=kwargs.get("clock", datetime.now),
        )
    elif source_type == "stdin":
        return StdinSimulatedWeightSource(
            initial_weight=kwargs.get("initial_weight", 500.0),
            clock=kwargs.get("clock", datetime.now),
        )
    elif source_type == "serial":
        return SerialWeightSource(
            port=kwargs.get("port"),
            baudrate=kwargs.get("baudrate"),
        )
    else:
        raise ValueError(f"Unknown weight source type: {source_type!r}")
