import hashlib

from .services import create_audit_log

def _hash(value): return hashlib.sha256(str(value or "").encode("utf-8", "ignore")).hexdigest() if value else ""

def _client_ip(request):
    from risk.network import get_client_ip
    return get_client_ip(request)

def audit_request_action(*, request, action, resource_type, resource_id="", metadata=None):
    return create_audit_log(action=action, resource_type=resource_type, resource_id=resource_id, actor=getattr(request, "user", None), metadata=metadata, request_id=getattr(request, "request_id", ""), ip_hash=_hash(_client_ip(request)), user_agent_hash=_hash(request.META.get("HTTP_USER_AGENT", "")))
