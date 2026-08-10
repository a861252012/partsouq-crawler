from partsouq_crawler.admin.config import AdminConfig


def test_admin_defaults_to_separate_least_privilege_mysql_identity() -> None:
    config = AdminConfig()

    assert config.mysql_user == "partsouq_admin"
    assert config.mysql_password == "partsouq-admin-local"
    assert config.bind_host == "127.0.0.1"
