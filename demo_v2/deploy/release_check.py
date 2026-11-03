"""Disposable release startup check. No env config, credentials or public I/O."""
import asyncio
import os
from pathlib import Path
import tempfile

from ..app import create_app
from ..config import DemoConfig


async def check():
    with tempfile.TemporaryDirectory(prefix="demo-release-check-") as folder:
        app=create_app(DemoConfig(data_dir=Path(folder)))
        async with app.router.lifespan_context(app):
            runtime=app.state.runtime
            assert runtime.owner.alive and runtime.projection['pid']!=os.getpid()
            assert runtime.database_ok and runtime.engine_ok
            assert app.state.resources.snapshot()['process']['scope']=='DEMO_SERVICE_PROCESSES'
            assert str(app.state.config.flat_fee_per_action)=='0.068'
            assert runtime.providers.discovery is None
        assert not runtime.owner.alive
    print('Isolated release startup, decision process and resources verified')


if __name__=='__main__':
    asyncio.run(check())
