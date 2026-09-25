"""
Single write path into the shared state layer (Redis pub/sub) per PRD §7.3b:
"no tab fetches its own copy of anything... every data point is written to a
single in-memory shared state layer exactly once, by exactly one ingestion
process". This module is that ingestion boundary.
"""
import json
import redis

from config.settings import settings

_r = redis.from_url(settings.redis_url)

CHANNELS = {
    "tick": "state:tick",
    "option_chain": "state:option_chain",
    "order_update": "state:order_update",
    "model_score": "state:model_score",
    "signal": "state:signal",
    "position": "state:position",
    "risk_state": "state:risk_state",
}


def publish_tick(payload: dict):
    _r.set(f"latest:tick:{payload.get('symbol')}", json.dumps(payload))
    _r.publish(CHANNELS["tick"], json.dumps(payload))


def publish_option_chain(index_id: str, payload: dict):
    _r.set(f"latest:option_chain:{index_id}", json.dumps(payload))
    _r.publish(CHANNELS["option_chain"], json.dumps({"index_id": index_id, **payload}))


def publish_order_update(payload: dict):
    _r.publish(CHANNELS["order_update"], json.dumps(payload))


def publish_model_score(index_id: str, model_id: str, payload: dict):
    _r.set(f"latest:model_score:{index_id}:{model_id}", json.dumps(payload))
    _r.publish(CHANNELS["model_score"], json.dumps({"index_id": index_id, "model_id": model_id, **payload}))


def publish_signal(payload: dict):
    _r.publish(CHANNELS["signal"], json.dumps(payload))


def publish_risk_state(payload: dict):
    _r.set("latest:risk_state", json.dumps(payload))
    _r.publish(CHANNELS["risk_state"], json.dumps(payload))
