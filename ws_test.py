import asyncio
import websockets

async def test():
    uri = "wss://drone-cloud-railway-production.up.railway.app/ws?token=change-this-secret"
    try:
        async with websockets.connect(uri) as ws:
            print("CONNECTED")
            await ws.send("hello")
            msg = await ws.recv()
            print("RECV:", msg)
    except Exception as e:
        print("ERROR:", type(e).__name__, str(e))

asyncio.run(test())
