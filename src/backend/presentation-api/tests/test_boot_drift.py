import importlib

import pytest


def test_api_urls_import_cleanly():
    importlib.import_module("api.urls")


def test_platform_credential_mirror_exists_with_expected_fields():
    from api.models.mirrors import PlatformCredential

    assert PlatformCredential._meta.db_table == "platform_credentials"
    assert PlatformCredential._meta.get_field("user_email").max_length == 254
    assert (
        PlatformCredential._meta.get_field("encrypted_payload").get_internal_type() == "BinaryField"
    )
    assert PlatformCredential._meta.get_field("is_expired").default is False


def test_user_identity_model_matches_post_0002_shape():
    from api.models.identity import ItemReadState, User

    with pytest.raises(Exception):
        User._meta.get_field("ms_oid")

    email = User._meta.get_field("email")
    preferences = User._meta.get_field("preferences")
    deleted_at = User._meta.get_field("deleted_at")

    assert email.unique is True
    assert preferences.default is dict
    assert deleted_at.null is True
    assert ItemReadState._meta.db_table == "presentation_api_item_read_state"
