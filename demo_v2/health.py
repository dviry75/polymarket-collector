from dataclasses import dataclass
from enum import StrEnum


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

    def snapshot(self) -> HealthSnapshot:
        components = tuple(self._components.values())
        if not components or any(c.critical and c.state != HealthState.READY for c in components):
            state = HealthState.NOT_READY
        elif any(c.state != HealthState.READY for c in components):
            state = HealthState.DEGRADED
        else:
            state = HealthState.READY
        return HealthSnapshot(state, components)
