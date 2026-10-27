"""Test-process event-loop policy for Windows Python/AnyIO compatibility."""

import asyncio
import sys


if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    # Python 3.12's default Proactor loop can hang while creating its
    # self-pipe in this test environment. Starlette TestClient and
    # IsolatedAsyncioTestCase both work with the selector loop here.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
