from dataclasses import dataclass
from enum import StrEnum
import time


class HealthState(StrEnum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    NOT_READY = "NOT_READY"


@dataclass(frozen=True)
class ComponentHealth:
    component: str
    state: HealthState
    reason: str = ""
    critical: bool = True


@dataclass(frozen=True)
class HealthSnapshot:
    state: HealthState
    components: tuple[ComponentHealth, ...]

    @property
    def entries_allowed(self) -> bool:
        return self.state == HealthState.READY


class HealthRegistry:
    def __init__(self) -> None:
        self._components: dict[str, ComponentHealth] = {}

    def record(self, component: ComponentHealth) -> None:
        self._components[component.component] = component

    def forget(self, component: str) -> None:
        self._components.pop(component,None)

    def snapshot(self) -> HealthSnapshot:
        components = tuple(self._components.values())
        if not components or any(c.critical and c.state != HealthState.READY for c in components):
            state = HealthState.NOT_READY
        elif any(c.state != HealthState.READY for c in components):
            state = HealthState.DEGRADED
        else:
            state = HealthState.READY
        return HealthSnapshot(state, components)


class StabilityGate:
    """A healthy interval must be continuous; every failed sample clears it."""

    def __init__(self, seconds=2.0, *, clock=time.monotonic):
        self.seconds, self.clock, self.since = seconds, clock, {}

    def ready(self, key, healthy):
        if not healthy:
            self.since.pop(key,None)
            return False
        started = self.since.setdefault(key,self.clock())
        return self.clock()-started >= self.seconds

    def clear(self):
        self.since.clear()
