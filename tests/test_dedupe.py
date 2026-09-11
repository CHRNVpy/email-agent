import pytest

from app import dedupe


def test_claim_is_exclusive_until_released(data_dir):
    key = dedupe.make_key("reply", "msg-1")
    assert dedupe.claim(key)
    assert not dedupe.claim(key)
    dedupe.release(key)
    assert dedupe.claim(key)
    dedupe.mark_done(key)
    assert dedupe.is_done(key)
    assert not dedupe.claim(key)


def test_once_is_noop_without_scope(data_dir):
    for _ in range(2):
        with dedupe.once("email", "a@example.com") as first:
            assert first


def test_once_suppresses_repeats_within_scope(data_dir):
    sent = []
    for _ in range(3):
        with dedupe.scope("wf:run-1:2"), dedupe.once("email", "a@example.com", "hi") as first:
            if first:
                sent.append(1)
    assert sent == [1]

    with dedupe.scope("wf:run-1:3"), dedupe.once("email", "a@example.com", "hi") as first:
        assert first  # another stage may send the same email


def test_failed_action_can_be_retried(data_dir):
    with pytest.raises(RuntimeError), dedupe.scope("s"), dedupe.once("drive-file", "x"):
        raise RuntimeError("upload failed")
    with dedupe.scope("s"), dedupe.once("drive-file", "x") as first:
        assert first
