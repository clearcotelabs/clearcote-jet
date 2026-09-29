"""The smallest useful program: one goal, one result.

    python examples/quickstart.py
"""

import asyncio

from clearcote_jet import Session, run
from clearcote_jet.cli import load_env_file


async def main():
    load_env_file()  # TYPESAFE_API_KEY from ./.env, if it is there
    session = await Session.launch(headless=False)  # persistent profile in ~/.clearcote-jet/profile
    try:
        page = await session.new_tab("https://docs.python.org/3/")
        result = await run(session, "Search the Python documentation for asyncio.gather and open its entry.", page=page)
        print(result["status"], result["url"])
        for step in result["trace"]:
            print(f"  {step['step']}. {step['kind']} {step['action'][:50]!r}  p={step['probability']}")
        print(result["markdown"][:500])
    finally:
        await session.close()


asyncio.run(main())
