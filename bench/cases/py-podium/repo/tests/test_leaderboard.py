from ranking.leaderboard import top

SCORES = [{'name': 'a', 'points': 1}, {'name': 'b', 'points': 3}]


def test_order():
    assert [s['name'] for s in top(SCORES, 5)] == ['b', 'a']
