import asyncio
import json
import unittest

import websockets

from codex_partner.app_server import AppServerClient


class AppServerTransportTests(unittest.TestCase):
    def test_websocket_transport_reconnects_without_owning_daemon(self):
        async def exercise():
            calls = []

            async def handler(connection):
                async for raw in connection:
                    request = json.loads(raw)
                    calls.append(request["method"])
                    if "id" not in request:
                        continue
                    result = {} if request["method"] == "initialize" else {"data": []}
                    await connection.send(json.dumps({"id": request["id"], "result": result}))

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                endpoint = f"ws://127.0.0.1:{port}"
                for _ in range(2):
                    client = AppServerClient({}, "persistent-test", websocket_url=endpoint)
                    await client.start()
                    self.assertEqual({"data": []}, await client.request("thread/list", {"limit": 1}))
                    self.assertIs(client, client.owner)
                    await client.close()

            self.assertEqual(
                ["initialize", "initialized", "thread/list", "initialize", "initialized", "thread/list"],
                calls,
            )

        asyncio.run(exercise())

    def test_websocket_transport_accepts_full_history_larger_than_64_mib(self):
        async def exercise():
            history = "x" * (65 * 1024 * 1024)

            async def handler(connection):
                async for raw in connection:
                    request = json.loads(raw)
                    if "id" not in request:
                        continue
                    result = {} if request["method"] == "initialize" else {"history": history}
                    await connection.send(json.dumps({"id": request["id"], "result": result}))

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                client = AppServerClient({}, "large-history", websocket_url=f"ws://127.0.0.1:{port}")
                try:
                    await client.start()
                    result = await client.request("thread/read", {"includeTurns": True})
                    self.assertEqual(len(history), len(result["history"]))
                    self.assertIsNone(client.reader_error)
                finally:
                    await client.close()

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
