from ranking.leaderboard import podium, top

SCORES = [{'name': n, 'points': p} for n, p in [('a', 5), ('b', 9), ('c', 7), ('d', 1)]]


def test_podium():
    assert podium(SCORES) == ['b', 'c', 'a']


def test_top_one():
    assert [s['name'] for s in top(SCORES, 1)] == ['b']
