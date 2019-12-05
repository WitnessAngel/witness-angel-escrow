import builtins

import jsonrpc
from decorator import decorator
from django.core.serializers.json import DjangoJSONEncoder
from jsonrpc import jsonrpc_method
from jsonrpc.site import JsonRpcSite

from wacryptolib.error_handling import StatusSlugsMapper
from wacryptolib.utilities import load_from_json_str, dump_to_json_str
from waescrow.escrow import SQL_ESCROW_API

# MONKEY-PATCH django-jsonrpc package so that it uses Extended Json in CANONICAL form on responses
from jsonrpc import site

assert site.loads
site.loads = load_from_json_str
assert site.dumps
site.dumps = dump_to_json_str

class ExtendedDjangoJSONEncoder(DjangoJSONEncoder):
    def default(self, o):
        try:
            return super().default(o)
        except TypeError:
            return "<BROKEN JSON OBJECT FOR %s>" % o  # Just to please jsonrpc _response_dict() method...


extended_jsonrpc_site = JsonRpcSite(json_encoder=ExtendedDjangoJSONEncoder)


# TODO refine translated exceptions later
exception_classes = StatusSlugsMapper.gather_exception_subclasses(builtins, parent_classes=[Exception])
exception_mapper = StatusSlugsMapper(exception_classes, fallback_exception_class=Exception)

@decorator
def convert_exceptions_to_jsonrpc_status_slugs(f, *args, **kwargs):
    try:
        return f(*args, **kwargs)
    except Exception as exc:  # FIXME - do not convert ALL exceptions, some classes are to be unhandled!
        status_slugs = exception_mapper.slugify_exception_class(exc.__class__)
        jsonrpc_error = jsonrpc.Error("Server-side exception occurred, see error data for details")
        jsonrpc_error.code = 400  # Unique for now
        jsonrpc_error.status = 200  # Do not trigger nasty errors in rpc client
        jsonrpc_error.data = dict(
            status_slugs=status_slugs,
            data=None,
            message_translated=None,
            message_untranslated=str(exc)
        )
        raise jsonrpc_error from exc


@jsonrpc_method("get_public_key", site=extended_jsonrpc_site)
@convert_exceptions_to_jsonrpc_status_slugs
def get_public_key(request, keychain_uid, key_type):
    del request
    return SQL_ESCROW_API.get_public_key(keychain_uid=keychain_uid, key_type=key_type)

@jsonrpc_method("get_message_signature", site=extended_jsonrpc_site)
@convert_exceptions_to_jsonrpc_status_slugs
def get_message_signature(request, keychain_uid, message, key_type, signature_algo):
    del request
    return SQL_ESCROW_API.get_message_signature(
            keychain_uid=keychain_uid, message=message, key_type=key_type, signature_algo=signature_algo
    )

@jsonrpc_method("decrypt_with_private_key", site=extended_jsonrpc_site)
@convert_exceptions_to_jsonrpc_status_slugs
def decrypt_with_private_key(request, keychain_uid, key_type, encryption_algo, cipherdict):
    del request
    return SQL_ESCROW_API.decrypt_with_private_key(
            keychain_uid=keychain_uid,
        key_type=key_type,
        encryption_algo=encryption_algo,
        cipherdict=cipherdict,
    )

@jsonrpc_method("request_decryption_authorization", site=extended_jsonrpc_site)
@convert_exceptions_to_jsonrpc_status_slugs
def request_decryption_authorization(request, keypair_identifiers, request_message):
    del request
    return SQL_ESCROW_API.request_decryption_authorization(
            keypair_identifiers=keypair_identifiers, request_message=request_message
    )
