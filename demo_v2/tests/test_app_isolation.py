import ast
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import httpx

from demo_v2.app import create_app
from demo_v2.config import DemoConfig
from demo_v2.health import ComponentHealth, HealthRegistry, HealthState
from demo_v2.providers.contracts import Providers


ROOT = Path(__file__).resolve().parents[2]


@asynccontextmanager
async def running_client(app):
    # Single-loop, in-process ASGI calls; no listening socket or portal thread.
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
            yield client


def test_app_starts_and_stops_with_only_temp_config_db(tmp_path):
    config = DemoConfig(data_dir=tmp_path / "demo")
    app = create_app(config)
    assert not config.data_dir.exists()
    async def run():
        async with running_client(app) as client:
            assert config.db_path.exists()
            assert (await client.get("/health")).json() == {
                "status": "alive", "mode": "SIMULATION", "trading_enabled": False,
            }
            response = await client.get("/ready")
            assert response.status_code == 503
            assert response.json() == {"state": "NOT_READY", "entries_allowed": False}
            assert (await client.get("/demo/api/v1/health")).status_code == 503
            assert (await client.post("/api/rules", json={})).status_code == 404
            assert (await client.get("/live/health")).status_code == 404
            schema = (await client.get("/openapi.json")).json()
            assert {"/health","/ready","/demo/api/v1/rules","/demo/api/v1/health"} <= set(schema["paths"])
            assert all(path in ("/health","/ready") or path.startswith("/demo/api/v1/") for path in schema["paths"])
    asyncio.run(run())
    assert next(c for c in app.state.health.snapshot().components if c.component == "database").reason == "STOPPED"


def test_live_cookie_and_injected_provider_do_not_grant_readiness_or_auth(tmp_path):
    class FakeCredentials:
        async def validate(self, username, password):
            raise AssertionError("auth integration is not implemented")

    app = create_app(DemoConfig(data_dir=tmp_path), providers=Providers(credentials=FakeCredentials()))
    async def run():
        async with running_client(app) as client:
            client.cookies.set("live_session", "fake-unused-live-cookie")
            response = await client.get("/demo/api/v1/health")
            assert response.json()["detail"]["code"] == "DEMO_AUTH_NOT_CONFIGURED"
            assert (await client.get("/ready")).json()["entries_allowed"] is False
    asyncio.run(run())


def test_health_model_handles_all_states_and_critical_blockers():
    registry = HealthRegistry()
    assert registry.snapshot().state == HealthState.NOT_READY
    registry.record(ComponentHealth("market", HealthState.READY))
    assert registry.snapshot().entries_allowed
    registry.record(ComponentHealth("telemetry", HealthState.NOT_READY, critical=False))
    assert registry.snapshot().state == HealthState.DEGRADED
    assert not registry.snapshot().entries_allowed
    registry.record(ComponentHealth("fees", HealthState.DEGRADED))
    assert registry.snapshot().state == HealthState.NOT_READY
    assert "secret" not in json.dumps(asdict(registry.snapshot()))


def test_runtime_import_allowlist_is_explicit():
    for path in (ROOT / "demo_v2").rglob("*.py"):
        if "tests" in path.relative_to(ROOT / "demo_v2").parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("live") for alias in node.names), path
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("live"):
                assert path.name == "book_primitives.py"
                assert node.module in {"live.order_book", "live.strategy"}
                if node.module == "live.strategy":
                    assert [alias.name for alias in node.names] == ["exact_trigger"]


def test_default_collection_from_repo_root_stays_inside_demo():
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-c", "demo_v2/pytest.ini", "--collect-only", "-q"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    nodes = [line for line in result.stdout.splitlines() if "::" in line]
    assert nodes
    assert all(node.startswith("tests/") for node in nodes), nodes


def test_fresh_process_blocks_live_imports_env_reads_and_network(tmp_path):
    # Import every runtime module under guards, then exercise the lifespan.
    # This catches transitive imports, not just forbidden source strings.
    script = textwrap.dedent('''
        import asyncio, builtins, importlib.abc, io, os, pathlib, socket, sqlite3, sys
        root, temp = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
        allowed = {"live", "live.order_book", "live.strategy"}
        class Guard(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if (fullname == "live" or fullname.startswith("live.")) and fullname not in allowed:
                    raise AssertionError("forbidden LIVE import: " + fullname)
                if fullname.split(".")[0] in {"trader_app", "live_app", "dashboard_app", "polymarket", "eth_account"}:
                    raise AssertionError("forbidden execution dependency: " + fullname)
        sys.meta_path.insert(0, Guard())
        original_open, original_io_open = builtins.open, io.open
        def guarded_open(original):
            def call(file, *args, **kwargs):
                if isinstance(file, (str, bytes, os.PathLike)):
                    name = os.fsdecode(file)
                    if name.endswith(".env") or "/etc/polymarket-live/" in name:
                        raise AssertionError("environment file was opened")
                return original(file, *args, **kwargs)
            return call
        builtins.open = guarded_open(original_open)
        io.open = guarded_open(original_io_open)
        def no_network(*args, **kwargs):
            raise AssertionError("network connection attempted")
        socket.create_connection = no_network
        socket.socket.connect = no_network
        os.environ.clear()
        os.environ.update({"DEMO_DATA_DIR": str(temp / "demo"),
                           "LIVE_DB_PATH": str(temp / "poly_live.sqlite3"),
                           "LIVE_LOGIN_PASSWORD_HASH": "unused-fake-canary"})
        # No private key exists in this fresh environment.
        import httpx
        from demo_v2.app import create_app
        from demo_v2.providers import book_primitives
        from decimal import Decimal
        assert book_primitives.exact_trigger("0.7400", Decimal("0.74"))
        assert not book_primitives.exact_trigger("0.73", Decimal("0.74"))
        assert book_primitives.OrderBookSet(["up", "down"]).generation == 1
        for path in (root / "demo_v2").rglob("*.py"):
            relative = path.relative_to(root).with_suffix("")
            if "tests" in relative.parts:
                continue
            module = ".".join(relative.parts)
            if module.endswith(".__init__"):
                module = module[:-9]
            __import__(module)
        app = create_app()
        assert not (temp / "demo").exists()
        async def run():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
                    assert (await client.get("/health")).status_code == 200
                    assert (await client.get("/ready")).status_code == 503
        asyncio.run(run())
        assert {name for name in sys.modules if name == "live" or name.startswith("live.")} == allowed
        assert not (temp / "poly_live.sqlite3").exists()
        assert {p.name for p in (temp / "demo").iterdir()} == {"demo_v2.sqlite3"}
        print("isolated startup and approved pure reuse passed")
    ''')
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": os.pathsep.join([str(ROOT), str(ROOT / "polymarket-collector")])}
    result = subprocess.run([sys.executable, "-B", "-c", script, str(ROOT), str(tmp_path)],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "approved pure reuse passed" in result.stdout
