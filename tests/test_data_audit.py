"""Audit des données : une coupure réseau passagère vers le RPC public ne doit pas faire planter tout l'audit."""
import asyncio

import httpx

from apex.reporting import data_audit as DA


def test_rpc_retries_after_connect_timeout(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("délai de connexion dépassé", request=request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}})

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(DA.asyncio, "sleep", no_sleep)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await DA.rpc(c, "getSlot", [])

    assert asyncio.run(run()) == {"ok": 1}
    assert calls["n"] == 2


def test_rpc_gives_up_with_none_when_network_stays_down(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("réseau coupé", request=request)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(DA.asyncio, "sleep", no_sleep)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await DA.rpc(c, "getTransaction", ["sig"])

    assert asyncio.run(run()) is None
