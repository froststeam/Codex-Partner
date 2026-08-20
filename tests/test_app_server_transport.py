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


if __name__ == "__main__":
    unittest.main()
