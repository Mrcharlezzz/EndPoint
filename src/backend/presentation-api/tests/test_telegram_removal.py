import importlib


def test_identity_models_no_longer_export_user_telegram_group():
    identity = importlib.import_module("api.models.identity")

    assert not hasattr(identity, "UserTelegramGroup")


def test_api_urls_do_not_register_telegram_group_routes():
    urls = importlib.import_module("api.urls")

    route_strings = {str(pattern.pattern) for pattern in urls.urlpatterns}

    assert "telegram/groups/" not in route_strings
    assert "telegram/groups/refresh/" not in route_strings
