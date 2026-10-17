from __future__ import annotations

import argparse
import asyncio

from .application import Application
from .config import Settings
from .server import main as server_main


async def chat() -> None:
    app = Application(Settings())
    await app.start()
    conversation = (await app.conversation_create({"title": "CLI Chat"}))["conversation"]
    print("KoreAgent Next CLI（输入 /quit 退出）")
    try:
        while True:
            text = input("You> ").strip()
            if text in {"/quit", "/exit"}:
                break
            if not text:
                continue
            result = await app.conversation_send({"conversation_id": conversation["id"], "content": text})
            print(f"KoreAgent Next> {result['content'] or ''}")
    finally:
        await app.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", default="chat", choices=["chat", "serve"])
    args, rest = parser.parse_known_args()
    if args.command == "serve":
        server_main()
    else:
        asyncio.run(chat())


if __name__ == "__main__":
    main()
