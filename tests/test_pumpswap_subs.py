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
