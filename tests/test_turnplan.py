"""Uji unit: TurnPlan meneruskan capability/config dari payload (tanpa server)."""
from dataclasses import asdict

from app.api.routes.ws_chat import TurnPlan


def test_turnplan_carries_capability_and_config() -> None:
    plan = TurnPlan(
        session=None,  # type: ignore[arg-type]
        content="tes",
        is_new_session=False,
        history=[],
        capability="deep_research",
        config={"depth": "mendalam"},
    )
    d = asdict(plan)
    assert d["capability"] == "deep_research"
    assert d["config"] == {"depth": "mendalam"}


def test_turnplan_defaults_none() -> None:
    plan = TurnPlan(
        session=None,  # type: ignore[arg-type]
        content="tes",
        is_new_session=False,
        history=[],
    )
    assert plan.capability is None
    assert plan.config is None
