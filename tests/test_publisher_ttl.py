"""A turn's event stream outlives the turn only while it waits on the user."""

from api import publisher as pub
from api.publisher import FINISHED_TURN_TTL_SECONDS, WAITING_TURN_TTL_SECONDS, SSEEventPublisher


class _Redis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []
        self.ttl: dict[str, int] = {}

    def xadd(self, key, fields):
        self.entries.append((key, fields))
        return f"{len(self.entries)}-0"

    def expire(self, key, seconds):
        self.ttl[key] = seconds


def _publisher(monkeypatch) -> tuple[SSEEventPublisher, _Redis]:
    redis = _Redis()
    monkeypatch.setattr(pub, "get_redis_client", lambda: redis)
    return SSEEventPublisher("s1"), redis


def test_finished_turn_expires_after_a_minute(monkeypatch):
    p, redis = _publisher(monkeypatch)
    p.publish_result(session_id="s1", subtype="success")
    p.close()
    assert redis.ttl["session:s1:events"] == FINISHED_TURN_TTL_SECONDS


def test_turn_waiting_on_a_question_stays_replayable(monkeypatch):
    p, redis = _publisher(monkeypatch)
    p.publish_result(session_id="s1", subtype="interrupted", interrupt={"value": {"questions": []}})
    p.close()
    assert redis.ttl["session:s1:events"] == WAITING_TURN_TTL_SECONDS


def test_a_later_result_replaces_the_waiting_state(monkeypatch):
    p, redis = _publisher(monkeypatch)
    p.publish_result(session_id="s1", subtype="interrupted", interrupt={"value": {}})
    p.publish_result(session_id="s1", subtype="success")
    p.close()
    assert redis.ttl["session:s1:events"] == FINISHED_TURN_TTL_SECONDS
