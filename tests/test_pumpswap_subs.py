from apex.ingestor.service import plan_pool_subs


def test_subscriptions_are_spread_over_rounds():
    wanted = {f"pool{i:03d}" for i in range(200)}
    subscribed: set[str] = set()
    rounds = 0
    while True:
        unsub, sub = plan_pool_subs(wanted, subscribed, set(), 5)
        if not unsub and not sub:
            break
        assert len(unsub) + len(sub) <= 5          # jamais de rafale de 200 abonnements
        subscribed |= set(sub)
        rounds += 1
    assert subscribed == wanted and rounds == 40


def test_unsubscribe_first_and_skip_pending():
    unsub, sub = plan_pool_subs({"a", "b", "c"}, {"x", "y"}, {"a"}, 3)
    assert unsub == ["x", "y"] and sub == ["b"]
    assert plan_pool_subs({"a"}, {"a"}, set(), 5) == ([], [])


def test_pools_spread_over_connections_below_rpc_limit():
    from apex.ingestor.service import pool_shard
    pools = [f"pool{i:03d}" for i in range(200)]
    shards = [pool_shard(p, 4) for p in pools]
    assert shards == [pool_shard(p, 4) for p in pools]          # stable
    counts = [shards.count(k) for k in range(4)]
    assert max(counts) < 90                                     # chaque connexion sous la limite (~100) du RPC public
    assert all(pool_shard(p, 1) == 0 for p in pools)


def test_connection_reopened_before_rpc_limit(monkeypatch):
    """Le RPC public coupe après 100 abonnements tentés : la connexion s'arrête d'elle-même à 90."""
    import asyncio

    import orjson

    import apex.ingestor.service as svc

    class FakeWS:
        def __init__(self):
            self.sent = []

        async def send(self, m):
            self.sent.append(orjson.loads(m))

        async def recv(self):
            await asyncio.sleep(3600)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeBus:
        async def get_json(self, key, default=None):
            return [{"pool": f"pool{i:03d}", "mint": f"mint{i}"} for i in range(150)]

    class FakeCfg:
        def get(self, key, default=None):
            return None

    ing = svc.Ingestor.__new__(svc.Ingestor)
    ing.cfg, ing.bus, ing.stats = FakeCfg(), FakeBus(), {"bytes": {}}
    ing.pumpswap_burst, ing.pumpswap_gap_s = 50, 0
    ing.pumpswap_conns, ing.pumpswap_max_attempts = 1, 90
    ing._pool_counts = {}
    ws = FakeWS()
    monkeypatch.setattr(svc.websockets, "connect", lambda *a, **k: ws)
    asyncio.run(asyncio.wait_for(ing.pumpswap(0), 10))         # se termine seule, sans coupure du RPC
    assert len(ws.sent) == 90 and all(m["method"] == "logsSubscribe" for m in ws.sent)
