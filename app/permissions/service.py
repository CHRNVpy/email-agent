"""User / role management and resolution of a sender's effective permissions."""

import logging
import time
from contextvars import ContextVar, Token

from sqlalchemy import delete, select

from app.config import settings
from app.db import app_session, utcnow
from app.permissions.models import Permission, Role, RolePermission, User, UserPermission, UserRole
from app.permissions.policy import DEFAULT_PERMISSIONS, DEFAULT_ROLES, Principal

logger = logging.getLogger(__name__)

_current: ContextVar[Principal | None] = ContextVar("principal", default=None)
_cache: dict[str, tuple[float, Principal]] = {}


class PermissionDenied(Exception):
    pass


# --- Acting principal (per request, via contextvars) ---------------------------------


def set_principal(principal: Principal | None) -> Token:
    return _current.set(principal)


def reset_principal(token: Token) -> None:
    _current.reset(token)


def current_principal() -> Principal:
    principal = _current.get()
    if principal is None:
        # Code paths outside an email request (CLI, scripts) act as the system.
        return Principal(email="system", unrestricted=True)
    return principal


def require(permission: str) -> None:
    principal = current_principal()
    if not principal.can(permission):
        logger.warning("Denied %s for %s", permission, principal.email)
        raise PermissionDenied(f"{principal.email} is not allowed to use '{permission}'.")


# --- Resolution -----------------------------------------------------------------------


async def resolve_principal(email: str) -> Principal:
    """Effective permissions of a sender. Unknown senders get the default role."""
    email = email.lower()
    if not settings.permissions_enabled:
        return Principal(email=email, unrestricted=True)

    cached = _cache.get(email)
    if cached and time.monotonic() - cached[0] < settings.permissions_cache_ttl:
        return cached[1]

    async with app_session() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user and user.is_active:
            granted = {p.name for role in user.roles for p in role.permissions}
            denied: set[str] = set()
            now = utcnow()
            for override in user.overrides:
                if override.expires_at and override.expires_at < now:
                    continue
                (granted if override.granted else denied).add(override.permission.name)
            principal = Principal(email, user.id, frozenset(granted), frozenset(denied))
        elif user:  # deactivated
            principal = Principal(email, user.id)
        else:
            role = await session.scalar(select(Role).where(Role.name == settings.default_user_role))
            principal = Principal(email, None, frozenset(p.name for p in role.permissions) if role else frozenset())

    _cache[email] = (time.monotonic(), principal)
    return principal


def invalidate_cache() -> None:
    _cache.clear()


async def is_registered(email: str) -> bool:
    async with app_session() as session:
        return await session.scalar(select(User.id).where(User.email == email.lower())) is not None


# --- Administration (used by the CLI) ------------------------------------------------


async def seed_defaults() -> None:
    """Create the default permission vocabulary and roles (idempotent)."""
    async with app_session() as session:
        existing = {p.name: p for p in await session.scalars(select(Permission))}
        for name, description in DEFAULT_PERMISSIONS.items():
            if name not in existing:
                existing[name] = Permission(name=name, description=description)
                session.add(existing[name])
        await session.flush()

        roles = {r.name for r in await session.scalars(select(Role))}
        for name, (description, permission_names) in DEFAULT_ROLES.items():
            if name not in roles:
                session.add(
                    Role(name=name, description=description, permissions=[existing[p] for p in permission_names])
                )
        await session.commit()
    invalidate_cache()


async def _get_or_create_permission(session, name: str) -> Permission:
    permission = await session.scalar(select(Permission).where(Permission.name == name))
    if permission is None:
        permission = Permission(name=name, description=DEFAULT_PERMISSIONS.get(name, ""))
        session.add(permission)
        await session.flush()
    return permission


async def create_user(email: str, name: str | None = None, roles: list[str] | None = None) -> User:
    async with app_session() as session:
        user = User(email=email.lower(), name=name)
        role_names = roles or [settings.default_user_role]
        user.roles = list(await session.scalars(select(Role).where(Role.name.in_(role_names))))
        missing = set(role_names) - {r.name for r in user.roles}
        if missing:
            raise ValueError(f"Unknown role(s): {', '.join(sorted(missing))}")
        session.add(user)
        await session.commit()
    invalidate_cache()
    return user


async def _user(session, email: str) -> User:
    user = await session.scalar(select(User).where(User.email == email.lower()))
    if user is None:
        raise ValueError(f"No user {email}")
    return user


async def set_role(email: str, role_name: str, *, assign: bool = True) -> None:
    async with app_session() as session:
        user = await _user(session, email)
        role = await session.scalar(select(Role).where(Role.name == role_name))
        if role is None:
            raise ValueError(f"No role {role_name}")
        if assign:
            if role not in user.roles:
                session.add(UserRole(user_id=user.id, role_id=role.id))
        else:
            await session.execute(delete(UserRole).where(UserRole.user_id == user.id, UserRole.role_id == role.id))
        await session.commit()
    invalidate_cache()


async def set_override(email: str, permission: str, *, granted: bool, expires_at=None) -> None:
    """Grant (or deny) a permission pattern directly to a user."""
    async with app_session() as session:
        user = await _user(session, email)
        perm = await _get_or_create_permission(session, permission)
        override = next((o for o in user.overrides if o.permission_id == perm.id), None)
        if override is None:
            user.overrides.append(
                UserPermission(permission_id=perm.id, permission=perm, granted=granted, expires_at=expires_at)
            )
        else:
            override.granted, override.expires_at = granted, expires_at
        await session.commit()
    invalidate_cache()


async def clear_override(email: str, permission: str) -> None:
    async with app_session() as session:
        user = await _user(session, email)
        user.overrides = [o for o in user.overrides if o.permission.name != permission]
        await session.commit()
    invalidate_cache()


async def add_role_permission(role_name: str, permission: str) -> None:
    async with app_session() as session:
        role = await session.scalar(select(Role).where(Role.name == role_name))
        if role is None:
            role = Role(name=role_name)
            session.add(role)
            await session.flush()
        perm = await _get_or_create_permission(session, permission)
        if perm not in role.permissions:
            session.add(RolePermission(role_id=role.id, permission_id=perm.id))
        await session.commit()
    invalidate_cache()


async def list_users() -> list[User]:
    async with app_session() as session:
        return list(await session.scalars(select(User).order_by(User.email)))


async def list_roles() -> list[Role]:
    async with app_session() as session:
        return list(await session.scalars(select(Role).order_by(Role.name)))
