from devagent.config import Config


def test_max_system_repairs_default_is_one():
    cfg = Config.load(env={})
    assert cfg.max_system_repairs == 1


def test_max_system_repairs_overridable_via_env():
    cfg = Config.load(env={"DEVAGENT_MAX_SYSTEM_REPAIRS": "3"})
    assert cfg.max_system_repairs == 3
