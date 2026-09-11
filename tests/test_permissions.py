from datetime import timedelta

import pytest

from app.db import utcnow
from app.permissions import service
from app.permissions.policy import Principal, matches


def test_wildcard_matching():
    assert matches("sql:crm:read", ["sql:*:read"])
    assert matches("anything:at:all", ["*"])
    assert not matches("sql:crm:write", ["sql:*:read"])


def test_denial_overrides_grant():
    principal = Principal("a@example.com", granted=frozenset({"*"}), denied=frozenset({"sql:*:write"}))
    assert principal.can("sheets:write")
    assert not principal.can("sql:crm:write")


def test_unrestricted_principal_can_do_anything():
    assert Principal("system", unrestricted=True).can("sql:crm:write")


async def test_role_permissions_are_resolved(app_db):
    await service.create_user("analyst@example.com", roles=["analyst"])
    principal = await service.resolve_principal("Analyst@Example.com")
    assert principal.user_id is not None
    assert principal.can("finance:read")
    assert principal.can("sql:warehouse:read")
    assert not principal.can("sql:warehouse:write")


async def test_unknown_sender_gets_default_role(app_db):
    principal = await service.resolve_principal("stranger@example.com")
    assert principal.user_id is None
    assert principal.can("web:search")
    assert not principal.can("finance:read")


async def test_overrides_grant_deny_and_expire(app_db):
    await service.create_user("ops@example.com", roles=["operator"])
    await service.set_override("ops@example.com", "sql:billing:write", granted=False)
    await service.set_override("ops@example.com", "finance:read", granted=True)
    await service.set_override("ops@example.com", "web:search", granted=True, expires_at=utcnow() - timedelta(days=1))

    principal = await service.resolve_principal("ops@example.com")
    assert principal.can("sql:crm:write")
    assert not principal.can("sql:billing:write")
    assert principal.can("finance:read")
    assert not principal.can("web:search")  # the grant has expired

    await service.clear_override("ops@example.com", "sql:billing:write")
    assert (await service.resolve_principal("ops@example.com")).can("sql:billing:write")


async def test_role_assignment_and_removal(app_db):
    await service.create_user("guest@example.com")
    await service.set_role("guest@example.com", "admin")
    assert (await service.resolve_principal("guest@example.com")).can("sql:crm:write")
    await service.set_role("guest@example.com", "admin", assign=False)
    assert not (await service.resolve_principal("guest@example.com")).can("sql:crm:write")


async def test_unknown_role_is_rejected(app_db):
    with pytest.raises(ValueError, match="Unknown role"):
        await service.create_user("x@example.com", roles=["superuser"])


def test_require_uses_current_principal():
    token = service.set_principal(Principal("g@example.com", granted=frozenset({"web:search"})))
    try:
        service.require("web:search")
        with pytest.raises(service.PermissionDenied):
            service.require("sheets:write")
    finally:
        service.reset_principal(token)
