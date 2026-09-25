"""
PRD §4.5.5 mode switching rules:
- Into Live/Both requires go-live gates (§2) cleared — refuse/warn otherwise.
- Out of Live -> Paper happens automatically on any circuit breaker trip
  (weekly drawdown cap breach), same mechanism as §4.8.4.
- Every mode switch (manual or automatic) logged with timestamp, reason,
  account state — full audit trail.
"""
from dataclasses import dataclass
from datetime import datetime
from loguru import logger


@dataclass
class ModeSwitchEvent:
    from_mode: str
    to_mode: str
    reason: str
    ts: datetime
    account_state: dict


def request_mode_switch(current_mode: str, target_mode: str, go_live_gates_cleared: bool,
                         account_state: dict, manual: bool = True) -> ModeSwitchEvent:
    if target_mode in ("live", "both") and not go_live_gates_cleared:
        raise PermissionError(
            "Go-live gates not cleared (PRD §2): expectancy>0 with significant bootstrap CI "
            "across >=500 trades or 12 months, max drawdown within cap. Refusing mode switch."
        )
    event = ModeSwitchEvent(
        from_mode=current_mode, to_mode=target_mode,
        reason="manual_switch" if manual else "auto_downgrade_circuit_breaker",
        ts=datetime.utcnow(), account_state=account_state,
    )
    logger.critical(f"MODE SWITCH: {event}")
    # persist to an audit table — never silently switch
    return event


def auto_downgrade_to_paper(current_mode: str, account_state: dict, breach_reason: str) -> ModeSwitchEvent:
    return request_mode_switch(current_mode, "paper", go_live_gates_cleared=True,
                                account_state=account_state, manual=False)
