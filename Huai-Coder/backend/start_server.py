"""
Windows-compatible uvicorn launcher.
psycopg (used by langgraph-checkpoint-postgres) requires SelectorEventLoop,
but Windows Python 3.12 defaults to ProactorEventLoop.
This script explicitly creates a SelectorEventLoop and runs uvicorn on it.
"""

import asyncio
import selectors
import sys


def main():
    if sys.platform == "win32":
        # Create SelectorEventLoop explicitly
        selector = selectors.SelectSelector()
        loop = asyncio.SelectorEventLoop(selector)
        asyncio.set_event_loop(loop)
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        import uvicorn
        from uvicorn.config import Config
        from uvicorn.server import Server

        config = Config(app="app.main:app", host="0.0.0.0", port=8000, log_level="info")
        server = Server(config)
        loop.run_until_complete(server.serve())
    else:
        import uvicorn

        uvicorn.run("app.main:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
