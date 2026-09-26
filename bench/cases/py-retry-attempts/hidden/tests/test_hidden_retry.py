import pytest

from net.retry import retry


def flaky(failures):
    state = {'n': 0}

    def action():
        state['n'] += 1
        if state['n'] <= failures:
            raise ValueError('boom')
        return 'ok'

    return action, state


def test_third_time_lucky():
    action, state = flaky(2)
    assert retry(action, attempts=3) == 'ok' and state['n'] == 3


def test_single_attempt_raises_the_error():
    action, state = flaky(5)
    with pytest.raises(ValueError):
        retry(action, attempts=1)
    assert state['n'] == 1
