#!/usr/bin/env python3
"""WebSocket Proxy Server for Gemini Live API."""

import asyncio
import json
import ssl
import sys

import certifi
import google.auth
import websockets
from google.auth.transport.requests import Request

WS_PORT = 8082
DEBUG = True


def log(msg):
    print(msg, flush=True)


def generate_access_token():
    try:
        creds, _ = google.auth.default()
        if not creds.valid:
            creds.refresh(Request())
        return creds.token
    except Exception as e:
        log(f"Error generating access token: {e}")
        return None


async def proxy_task(source, dest, label):
    try:
        async for message in source:
            try:
                data = json.loads(message)
                if DEBUG:
                    keys = list(data.keys())
                    summary = str(keys)
                    if "serverContent" in data:
                        sc = data["serverContent"]
                        if sc.get("turnComplete"):
                            summary = "TURN_COMPLETE"
                        elif sc.get("interrupted"):
                            summary = "INTERRUPTED"
                        elif sc.get("inputTranscription"):
                            summary = f"INPUT: {sc['inputTranscription'].get('text','')[:80]}"
                        elif sc.get("outputTranscription"):
                            summary = f"OUTPUT: {sc['outputTranscription'].get('text','')[:80]}"
                        elif sc.get("modelTurn"):
                            parts = sc["modelTurn"].get("parts", [])
                            if parts and parts[0].get("text"):
                                summary = f"TEXT: {parts[0]['text'][:80]}"
                            elif parts and parts[0].get("inlineData"):
                                summary = "AUDIO"
                            else:
                                summary = f"modelTurn: {list(sc['modelTurn'].keys())}"
                        else:
                            summary = f"serverContent: {list(sc.keys())}"
                    elif "setupComplete" in data:
                        summary = "SETUP_COMPLETE"
                    elif "toolCall" in data:
                        tc = data["toolCall"]
                        calls = tc.get("functionCalls", [])
                        names = [c.get("name", "?") for c in calls]
                        summary = f"TOOL_CALL: {names}"
                    elif "setup" in data:
                        summary = "SETUP"
                    elif "client_content" in data:
                        turns = data["client_content"].get("turns", [])
                        if turns:
                            parts = turns[0].get("parts", [])
                            if parts and parts[0].get("text"):
                                summary = f"CLIENT: {parts[0]['text'][:80]}"
                            elif parts and len(parts) > 1:
                                summary = f"CLIENT: text+{len(parts)-1} parts"
                            else:
                                summary = "CLIENT (other)"
                        else:
                            summary = "CLIENT (empty)"
                    elif "tool_response" in data:
                        summary = f"TOOL_RESPONSE"
                    elif "realtime_input" in data:
                        summary = None
                    elif "service_url" in data:
                        summary = f"SERVICE_URL"

                    if summary:
                        log(f"[{label}] {summary}")

                await dest.send(json.dumps(data))
            except Exception as e:
                log(f"Error proxying {label}: {e}")
    except websockets.ConnectionClosed as e:
        log(f"{label} closed: code={e.code}")
    except Exception as e:
        log(f"Unexpected error ({label}): {e}")


async def create_proxy(client_ws, bearer_token, service_url):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {bearer_token}",
    }
    ssl_context = ssl.create_default_context(cafile=certifi.where())

    log(f"Connecting to Gemini API...")
    try:
        async with websockets.connect(
            service_url, additional_headers=headers, ssl=ssl_context
        ) as server_ws:
            log("Connected to Gemini API")
            c2s = asyncio.create_task(proxy_task(client_ws, server_ws, "C->S"))
            s2c = asyncio.create_task(proxy_task(server_ws, client_ws, "S->C"))
            done, pending = await asyncio.wait(
                [c2s, s2c], return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
    except websockets.ConnectionClosed as e:
        log(f"Server connection closed: code={e.code}")
        try:
            await client_ws.close(e.code)
        except Exception:
            pass
    except Exception as e:
        log(f"Failed to connect to Gemini: {e}")
        try:
            await client_ws.close(1008, "Upstream failed")
        except Exception:
            pass


async def handle_client(websocket):
    log("New client connection")
    try:
        msg = await asyncio.wait_for(websocket.recv(), timeout=10.0)
        data = json.loads(msg)
        bearer_token = data.get("bearer_token")
        service_url = data.get("service_url")

        if not bearer_token:
            log("Generating access token...")
            bearer_token = generate_access_token()
            if not bearer_token:
                await websocket.close(1008, "Auth failed")
                return
            log("Token OK")

        if not service_url:
            await websocket.close(1008, "No service_url")
            return

        await create_proxy(websocket, bearer_token, service_url)
    except asyncio.TimeoutError:
        await websocket.close(1008, "Timeout")
    except json.JSONDecodeError:
        await websocket.close(1008, "Invalid JSON")
    except Exception as e:
        log(f"Error: {e}")
        try:
            await websocket.close(1011, "Internal error")
        except Exception:
            pass


async def main():
    log(f"WebSocket proxy running on ws://localhost:{WS_PORT}")
    async with websockets.serve(handle_client, "0.0.0.0", WS_PORT):
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("\nServer stopped")
