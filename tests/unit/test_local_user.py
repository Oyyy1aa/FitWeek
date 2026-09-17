"""Single-user identity generation contracts."""

from app.application.local_user import build_local_user
from app.config import Settings


def test_local_user_is_stable_by_email_and_uses_display_configuration() -> None:
    first = build_local_user(
        Settings(
            single_user_email="member@fitweek.local",
            single_user_display_name="训练者",
            single_user_timezone="Asia/Shanghai",
            _env_file=None,
        )
    )
    renamed = build_local_user(
        Settings(
            single_user_email="member@fitweek.local",
            single_user_display_name="新名称",
            single_user_timezone="Asia/Shanghai",
            _env_file=None,
        )
    )

    assert first.id == renamed.id
    assert first.display_name == "训练者"
    assert renamed.display_name == "新名称"
    assert first.email == "member@fitweek.local"


def test_changed_email_creates_a_distinct_local_user_identity() -> None:
    first = build_local_user(
        Settings(single_user_email="first@fitweek.local", _env_file=None)
    )
    second = build_local_user(
        Settings(single_user_email="second@fitweek.local", _env_file=None)
    )

    assert first.id != second.id
