"""Local single-user configuration contracts."""

from app.config import AppMode, PersistenceBackend, Settings


def test_local_defaults_select_mysql_and_single_user_mode() -> None:
    settings = Settings(_env_file=None)

    assert settings.persistence_backend is PersistenceBackend.MYSQL
    assert settings.app_mode is AppMode.SINGLE_USER
    assert settings.app_host == "127.0.0.1"
    assert settings.single_user_email == "demo@fitweek.local"
    assert settings.single_user_display_name == "FitWeek 用户"
    assert settings.single_user_timezone == "Asia/Shanghai"
    assert settings.cors_allowed_origins == (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )


def test_wildcard_cors_is_rejected() -> None:
    try:
        Settings(cors_allowed_origins=("*",), _env_file=None)
    except ValueError as exc:
        assert "CORS" in str(exc)
    else:
        raise AssertionError("wildcard CORS origin must be rejected")
