"""
Pub/sub read side. Tabs/modules subscribe to relevant channels and
re-render on push — never poll the shared state layer on their own
timer (PRD §7.3b consequence: "Pub/sub push, not per-tab polling").
"""
import json
import redis

from config.settings import settings
from engine.shared_state.publisher import CHANNELS

_r = redis.from_url(settings.redis_url)


def get_latest(key: str) -> dict | None:
    raw = _r.get(key)
    return json.loads(raw) if raw else None


def subscribe(channel_names: list[str]):
    """Returns a redis PubSub object already subscribed — caller iterates .listen()."""
    pubsub = _r.pubsub()
    channels = [CHANNELS[c] for c in channel_names]
    pubsub.subscribe(*channels)
    return pubsub
