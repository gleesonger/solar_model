from common import try_get


def test_try_get_returns_operation_result() -> None:
    assert try_get(lambda: 5, 0, ValueError) == 5


def test_try_get_returns_default_for_requested_exception() -> None:
    assert try_get(lambda: int("invalid"), 0, ValueError) == 0


def test_try_get_does_not_hide_other_exceptions() -> None:
    try:
        try_get(lambda: {}["missing"], 0, ValueError)
    except KeyError:
        pass
    else:
        raise AssertionError("try_get should not hide unlisted exceptions")
