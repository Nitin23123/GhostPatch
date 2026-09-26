from net.retry import retry


def test_success_first_time():
    assert retry(lambda: 42) == 42
