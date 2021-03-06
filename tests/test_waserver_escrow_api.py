import random
import requests
from datetime import timedelta

import pytest
from Crypto.Random import get_random_bytes
from bson.json_util import dumps, loads
from django.conf import settings
from django.db import IntegrityError
from django.test import Client
from django.utils import timezone
from freezegun import freeze_time

from wacryptolib.container import (
    encrypt_data_into_container,
    decrypt_data_from_container, gather_escrow_dependencies, request_decryption_authorizations,
)
from wacryptolib.encryption import _encrypt_via_rsa_oaep
from wacryptolib.escrow import generate_free_keypair_for_least_provisioned_key_type
from wacryptolib.exceptions import KeyDoesNotExist, SignatureVerificationError, AuthorizationError, DecryptionError
from wacryptolib.jsonrpc_client import JsonRpcProxy, status_slugs_response_error_handler
from wacryptolib.key_generation import load_asymmetric_key_from_pem_bytestring
from wacryptolib.key_storage import DummyKeyStorage
from wacryptolib.scaffolding import (
    check_key_storage_basic_get_set_api,
    check_key_storage_free_keys_api,
    check_key_storage_free_keys_concurrency,
)
from wacryptolib.signature import verify_message_signature
from wacryptolib.utilities import generate_uuid0
from waescrow.escrow import SqlKeyStorage, _fetch_key_object_or_raise
from waescrow.models import EscrowKeypair


def test_sql_key_storage_basic_and_free_keys_api(db):

    sql_key_storage = SqlKeyStorage()

    test_locals = check_key_storage_basic_get_set_api(sql_key_storage)
    keychain_uid = test_locals["keychain_uid"]
    key_type = test_locals["key_type"]

    check_key_storage_free_keys_api(sql_key_storage)

    representation = repr(EscrowKeypair.objects.first())
    assert "ui" in representation

    with pytest.raises(
        IntegrityError
    ):  # Final tests, since it breaks current DB transaction
        EscrowKeypair.objects.create(
            keychain_uid=keychain_uid,
            key_type=key_type,
            public_key=b"hhhh",
            private_key=b"jjj",
        )


@pytest.mark.django_db(transaction=True)
def test_sql_key_storage_free_keys_concurrent_transactions():
    """This test runs outside SQL transactions, and checks the handling of concurrency via threading locks."""
    sql_key_storage = SqlKeyStorage()
    check_key_storage_free_keys_concurrency(sql_key_storage)


def test_jsonrpc_escrow_signature(live_server):

    jsonrpc_url = live_server.url + "/json/"  # FIXME change url!!

    escrow_proxy = JsonRpcProxy(
        url=jsonrpc_url, response_error_handler=status_slugs_response_error_handler
    )

    keychain_uid = generate_uuid0()
    signature_algo = "DSA_DSS"
    secret = get_random_bytes(101)
    secret_too_big = get_random_bytes(150)

    public_key_signature_pem = escrow_proxy.fetch_public_key(
        keychain_uid=keychain_uid, key_type=signature_algo
    )
    public_key_signature = load_asymmetric_key_from_pem_bytestring(
        key_pem=public_key_signature_pem, key_type=signature_algo
    )

    signature = escrow_proxy.get_message_signature(
        keychain_uid=keychain_uid, message=secret, signature_algo=signature_algo
    )

    with pytest.raises(ValueError, match="too big"):
        escrow_proxy.get_message_signature(
            keychain_uid=keychain_uid,
            message=secret_too_big,
            signature_algo=signature_algo,
        )

    verify_message_signature(
        message=secret,
        signature=signature,
        key=public_key_signature,
        signature_algo=signature_algo,
    )

    signature["digest"] += b"xyz"
    with pytest.raises(SignatureVerificationError, match="not authentic|Incorrect signature"):
        verify_message_signature(
            message=secret,
            signature=signature,
            key=public_key_signature,
            signature_algo=signature_algo,
        )


def test_jsonrpc_get_request(live_server):

    jsonrpc_url = live_server.url + "/json/"

    response = requests.get(jsonrpc_url)
    assert response.headers["Content-Type"] == "application/json"
    assert response.json() == \
           {'error': {'code': {'$numberInt': '-32600'},
                      'data': None, 'message': 'InvalidRequestError: The method you are trying to access is not available by GET requests',
                      'name': 'InvalidRequestError'}, 'id': None}


def test_jsonrpc_escrow_decryption_authorization_flags(live_server):

    jsonrpc_url = live_server.url + "/json/"  # FIXME change url!!

    escrow_proxy = JsonRpcProxy(
        url=jsonrpc_url, response_error_handler=status_slugs_response_error_handler
    )

    keychain_uid = generate_uuid0()
    keychain_uid_bad = generate_uuid0()
    key_encryption_algo = "RSA_OAEP"
    secret = get_random_bytes(101)

    public_key_encryption_pem = escrow_proxy.fetch_public_key(
        keychain_uid=keychain_uid, key_type=key_encryption_algo
    )
    public_key_encryption = load_asymmetric_key_from_pem_bytestring(
        key_pem=public_key_encryption_pem, key_type=key_encryption_algo
    )

    cipherdict = _encrypt_via_rsa_oaep(plaintext=secret, key=public_key_encryption)

    def _attempt_decryption():
        return escrow_proxy.decrypt_with_private_key(
            keychain_uid=keychain_uid,
            encryption_algo=key_encryption_algo,
            cipherdict=cipherdict,
        )

    with freeze_time() as frozen_datetime:

        with pytest.raises(AuthorizationError, match="Decryption not authorized"):
            _attempt_decryption()

        keypair_obj = EscrowKeypair.objects.get(
            keychain_uid=keychain_uid, key_type=key_encryption_algo
        )
        keypair_obj.decryption_authorized_at = timezone.now() + timedelta(hours=2)
        keypair_obj.save()

        with pytest.raises(
                AuthorizationError, match="Decryption authorization is only valid from"
        ):
            _attempt_decryption()  # Too early

        frozen_datetime.tick(delta=timedelta(hours=3))

        decrypted = _attempt_decryption()
        assert decrypted == secret  # It works!

        with pytest.raises(KeyDoesNotExist, match="not found"):
            escrow_proxy.decrypt_with_private_key(
                keychain_uid=keychain_uid_bad,
                encryption_algo=key_encryption_algo,
                cipherdict=cipherdict,
            )

        cipherdict["digest_list"].append(b"aaabbbccc")
        with pytest.raises(ValueError, match="Ciphertext with incorrect length"):
            escrow_proxy.decrypt_with_private_key(
                keychain_uid=keychain_uid,
                encryption_algo=key_encryption_algo,
                cipherdict=cipherdict,
            )

        frozen_datetime.tick(
            delta=timedelta(hours=24)
        )  # We hardcode DECRYPTION_AUTHORIZATION_LIFESPAN_H here

        with pytest.raises(
            AuthorizationError, match="Decryption authorization is only valid from"
        ):
            _attempt_decryption()  # Too late, cipherdict is not even used so no ValueError

        keypair_obj.decryption_authorized_at = None
        keypair_obj.save()

        with pytest.raises(AuthorizationError, match="Decryption not authorized"):
            _attempt_decryption()  # No more authorization at all


def test_jsonrpc_escrow_request_decryption_authorization_for_normal_keys(live_server):

    jsonrpc_url = live_server.url + "/json/"  # FIXME change url!!

    escrow_proxy = JsonRpcProxy(
        url=jsonrpc_url, response_error_handler=status_slugs_response_error_handler
    )

    key_encryption_algo = "RSA_OAEP"

    with freeze_time() as frozen_datetime:  # TEST AUTHORIZATION REQUEST HANDLING

        keychain_uid1 = generate_uuid0()
        keychain_uid2 = generate_uuid0()
        keychain_uid3 = generate_uuid0()
        keychain_uid4 = generate_uuid0()
        keychain_uid_unexisting = generate_uuid0()

        all_keypair_identifiers = [
            dict(keychain_uid=keychain_uid1, key_type=key_encryption_algo),
            dict(keychain_uid=keychain_uid2, key_type=key_encryption_algo),
            dict(keychain_uid=keychain_uid3, key_type=key_encryption_algo),
            dict(keychain_uid=keychain_uid4, key_type=key_encryption_algo),
            dict(keychain_uid=keychain_uid_unexisting, key_type=key_encryption_algo),
        ]

        public_key_pem = escrow_proxy.fetch_public_key(
            keychain_uid=keychain_uid1, key_type=key_encryption_algo
        )
        assert public_key_pem
        assert not _fetch_key_object_or_raise(
            keychain_uid=keychain_uid1, key_type=key_encryption_algo
        ).decryption_authorized_at

        # Non-pregenerated keys don't have that field set!
        assert not _fetch_key_object_or_raise(
            keychain_uid=keychain_uid1, key_type=key_encryption_algo
        ).attached_at

        result = escrow_proxy.request_decryption_authorization(
            keypair_identifiers=[], request_message="I want decryption!"
        )
        assert result["success_count"] == 0
        assert result["too_old_count"] == 0
        assert result["not_found_count"] == 0

        frozen_datetime.tick(delta=timedelta(minutes=2))

        result = escrow_proxy.request_decryption_authorization(
            keypair_identifiers=all_keypair_identifiers,
            request_message="I want decryption!",
        )
        assert result["success_count"] == 1
        assert result["too_old_count"] == 0
        assert (
            result["not_found_count"] == 4
        )  # keychain_uid2 and keychain_uid3 not created yet

        old_decryption_authorized_at = _fetch_key_object_or_raise(
            keychain_uid=keychain_uid1, key_type=key_encryption_algo
        ).decryption_authorized_at
        assert old_decryption_authorized_at

        public_key_pem = escrow_proxy.fetch_public_key(
            keychain_uid=keychain_uid2, key_type=key_encryption_algo
        )
        assert public_key_pem
        public_key_pem = escrow_proxy.fetch_public_key(
            keychain_uid=keychain_uid3, key_type=key_encryption_algo
        )
        assert public_key_pem

        frozen_datetime.tick(delta=timedelta(minutes=4))

        result = escrow_proxy.request_decryption_authorization(
            keypair_identifiers=all_keypair_identifiers,
            request_message="I want decryption!",
        )
        assert result["success_count"] == 2
        assert result["too_old_count"] == 1
        assert result["not_found_count"] == 2

        assert (
                _fetch_key_object_or_raise(
                keychain_uid=keychain_uid1, key_type=key_encryption_algo
            ).decryption_authorized_at
            == old_decryption_authorized_at
        )  # Unchanged
        assert _fetch_key_object_or_raise(
            keychain_uid=keychain_uid2, key_type=key_encryption_algo
        ).decryption_authorized_at
        assert _fetch_key_object_or_raise(
            keychain_uid=keychain_uid3, key_type=key_encryption_algo
        ).decryption_authorized_at

        with pytest.raises(KeyDoesNotExist, match="not found"):
            _fetch_key_object_or_raise(
                keychain_uid=keychain_uid_unexisting, key_type=key_encryption_algo
            )

        public_key_pem = escrow_proxy.fetch_public_key(
            keychain_uid=keychain_uid4, key_type=key_encryption_algo
        )
        assert public_key_pem

        frozen_datetime.tick(delta=timedelta(minutes=6))

        result = escrow_proxy.request_decryption_authorization(
            keypair_identifiers=all_keypair_identifiers,
            request_message="I want decryption!",
        )
        assert result["success_count"] == 0
        assert result["too_old_count"] == 4
        assert result["not_found_count"] == 1

        assert (
            _fetch_key_object_or_raise(
                keychain_uid=keychain_uid1, key_type=key_encryption_algo
            ).decryption_authorized_at
            == old_decryption_authorized_at
        )  # Unchanged

    del all_keypair_identifiers


def test_jsonrpc_escrow_request_decryption_authorization_for_free_keys(live_server):

    jsonrpc_url = live_server.url + "/json/"  # FIXME change url!!

    escrow_proxy = JsonRpcProxy(
        url=jsonrpc_url, response_error_handler=status_slugs_response_error_handler
    )

    keychain_uid_free = generate_uuid0()
    free_key_type1 = "RSA_OAEP"
    free_key_type2 = "ECC_DSS"
    free_key_type3 = "DSA_DSS"

    all_requested_keypair_identifiers = [
        dict(keychain_uid=keychain_uid_free, key_type=free_key_type1),
        dict(keychain_uid=keychain_uid_free, key_type=free_key_type2),
    ]

    sql_key_storage = SqlKeyStorage()

    with freeze_time() as frozen_datetime:  # TEST RELATION WITH FREE KEYS ATTACHMENT

        for i in range(3):  # Generate 1 free keypair per type
            has_generated = generate_free_keypair_for_least_provisioned_key_type(
                key_storage=sql_key_storage,
                max_free_keys_per_type=1,
                key_types=[free_key_type1, free_key_type2, free_key_type3]
            )
            assert has_generated

        keys_generated_before_datetime = timezone.now()

        public_key_pem1 = escrow_proxy.fetch_public_key(
                    keychain_uid=keychain_uid_free, key_type=free_key_type1
                )
        assert public_key_pem1

        # This key will not have early-enough request for authorization
        public_key_pem3 = escrow_proxy.fetch_public_key(
                    keychain_uid=keychain_uid_free, key_type=free_key_type3
                )
        assert public_key_pem3

        result = escrow_proxy.request_decryption_authorization(
            keypair_identifiers=all_requested_keypair_identifiers,
            request_message="I want early decryption!",
        )
        assert result["success_count"] == 1
        assert result["too_old_count"] == 0
        assert result["not_found_count"] == 1  # free_key_type2 is not attached yet

        frozen_datetime.tick(delta=timedelta(minutes=6))

        public_key_pem2 = escrow_proxy.fetch_public_key(
                    keychain_uid=keychain_uid_free, key_type=free_key_type2
                )
        assert public_key_pem2

        result = escrow_proxy.request_decryption_authorization(
            keypair_identifiers=all_requested_keypair_identifiers,
            request_message="I want later decryption!",
        )
        assert result["success_count"] == 1  # It's attachment time which counts!
        assert result["too_old_count"] == 1  # First key is too old now
        assert result["not_found_count"] == 0

        keypair_obj = EscrowKeypair.objects.get(
            keychain_uid=keychain_uid_free, key_type=free_key_type1
        )
        assert keypair_obj.created_at <= keys_generated_before_datetime
        assert keypair_obj.attached_at
        assert keypair_obj.decryption_authorized_at
        first_authorized_at = keypair_obj.decryption_authorized_at

        keypair_obj = EscrowKeypair.objects.get(
            keychain_uid=keychain_uid_free, key_type=free_key_type2
        )
        assert keypair_obj.created_at <= keys_generated_before_datetime
        assert keypair_obj.attached_at
        assert keypair_obj.decryption_authorized_at
        assert keypair_obj.decryption_authorized_at >= first_authorized_at + timedelta(minutes=5)

        keypair_obj = EscrowKeypair.objects.get(
            keychain_uid=keychain_uid_free, key_type=free_key_type3
        )
        assert keypair_obj.created_at <= keys_generated_before_datetime
        assert keypair_obj.attached_at
        assert not keypair_obj.decryption_authorized_at  # Never requested


def test_jsonrpc_escrow_encrypt_decrypt_container(live_server):

    jsonrpc_url = live_server.url + "/json/"  # FIXME change url!!

    encryption_conf = dict(
        data_encryption_strata=[
            # First we encrypt with local key and sign via main remote escrow
            dict(
                data_encryption_algo="AES_EAX",
                key_encryption_strata=[
                    dict(
                        key_encryption_algo="RSA_OAEP", key_escrow=dict(escrow_type="jsonrpc", url=jsonrpc_url)
                    )
                ],
                data_signatures=[
                    dict(
                        message_prehash_algo="SHA512",
                        signature_algo="DSA_DSS",
                        signature_escrow=dict(escrow_type="jsonrpc", url=jsonrpc_url),
                    )
                ],
            )
        ]
    )

    # CASE 1: authorization request well sent a short time after creation of "keychain_uid" keypair, so decryption is accepted

    with freeze_time() as frozen_datetime:

        keychain_uid = generate_uuid0()
        data = get_random_bytes(101)

        container = encrypt_data_into_container(
            data=data,
            conf=encryption_conf,
            metadata=None,
            keychain_uid=keychain_uid,
            key_storage_pool=None,  # Unused by this config actually
        )

        frozen_datetime.tick(delta=timedelta(minutes=3))

        with pytest.raises(AuthorizationError):
            decrypt_data_from_container(
                container=container, key_storage_pool=None
            )

        # Access automatically granted for now, with this escrow, when keys are young
        escrow_dependencies = gather_escrow_dependencies(containers=[container])
        decryption_authorization_requests_result = request_decryption_authorizations(
                escrow_dependencies,
                key_storage_pool=None,
                request_message="I need access to this")
        print(">>>>> request_decryption_authorizations is", decryption_authorization_requests_result)

        decrypted_data = decrypt_data_from_container(
            container=container, key_storage_pool=None
        )
        assert decrypted_data == data

        frozen_datetime.tick(
            delta=timedelta(hours=23)
        )  # Once authorization is granted, it stays so for a long time
        decrypted_data = decrypt_data_from_container(
            container=container, key_storage_pool=None
        )
        assert decrypted_data == data

        frozen_datetime.tick(
            delta=timedelta(hours=2)
        )  # Authorization has expired, and grace period to get one has long expired
        with pytest.raises(
            AuthorizationError, match="Decryption authorization is only valid from"
        ):
            decrypt_data_from_container(
                container=container, key_storage_pool=None
            )

    # CASE 2: authorization request sent too late after creation of "keychain_uid" keypair, so decryption is rejected

    with freeze_time() as frozen_datetime:

        keychain_uid = generate_uuid0()
        data = get_random_bytes(101)
        local_key_storage = DummyKeyStorage()

        container = encrypt_data_into_container(
            data=data,
            conf=encryption_conf,
            metadata=None,
            keychain_uid=keychain_uid,
                key_storage_pool=None,  # Unused by this config actually
        )

        frozen_datetime.tick(
            delta=timedelta(minutes=6)
        )  # More than the 5 minutes grace period
        with pytest.raises(AuthorizationError, match="Decryption not authorized"):
            decrypt_data_from_container(
                container=container, key_storage_pool=None
            )


def test_crashdump_reports(db):
    client = Client(enforce_csrf_checks=True)

    crashdump = "sòme dâta %s" % random.randint(1, 10000)

    res = client.get("/crashdumps/")
    assert res.status_code == 200
    assert res.content == b"CRASHDUMP ENDPOINT OF WAESCROW"

    res = client.post("/crashdumps/")
    assert res.status_code == 400
    assert res.content == b"Missing crashdump field"

    res = client.post("/crashdumps/", data=dict(crashdump=crashdump))
    assert res.status_code == 200
    assert res.content == b"OK"

    dump_files = sorted(settings.CRASHDUMPS_DIR.iterdir())
    assert dump_files
    dump_file_content = dump_files[-1].read_text(encoding="utf8")
    assert dump_file_content == crashdump


def test_waescrow_wsgi_application(db):
    from waescrow.wsgi import application

    with pytest.raises(KeyError, match="REQUEST_METHOD"):
        application(environ={}, start_response=lambda *args, **kwargs: None)
