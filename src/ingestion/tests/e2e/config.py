"""Static test configuration — ports, admin creds, URLs.

Every value here is test-scoped; nothing is read from the runtime env.
Ports default to 0 (OS-assigned) and are resolved by fixtures after bind.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AdminCreds:
    email: str = "e2e-admin@test.local"
    password: str = "e2e-password-DO-NOT-USE-IN-PROD"


@dataclass
class StubPorts:
    """Filled in by fixtures once stub servers bind."""

    teams_csa: int = 0
    teams_graph: int = 0
    moodle: int = 0
    telegram: int = 0


@dataclass
class HarnessAddresses:
    """Filled in by fixtures once daemon binds."""

    daemon_base_url: str = ""
    daemon_ws_base_url: str = ""
    stub_ports: StubPorts = field(default_factory=StubPorts)


ADMIN = AdminCreds()
