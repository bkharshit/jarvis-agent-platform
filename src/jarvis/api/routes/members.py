"""Member management (S2, ADR 0009 §8): list/create/patch/delete users of
the acting principal's tenant. Admin or owner only (403 otherwise) —
minimal roles, no RBAC. A foreign tenant's member id is 404, never 403
(no existence leak); only owners can act on owners (no admin-driven
lockout of the owner role)."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy.exc import IntegrityError

from jarvis.api.auth import AuthContext, AuthDep
from jarvis.api.errors import ApiError
from jarvis.api.schemas import MemberCreate, MemberList, MemberOut, MemberPatch
from jarvis.domain.auth import TenantRole, UserAccount
from jarvis.security import hash_password

router = APIRouter(prefix="/members", tags=["members"])


def _require_member_manager(auth: AuthContext) -> None:
    if not auth.principal.can_manage_members:
        raise ApiError(403, "forbidden", "member management requires the admin or owner role")


def _require_role(role: str | None) -> TenantRole:
    if role not in ("owner", "admin", "member"):
        raise ApiError(422, "validation", f"role must be one of owner|admin|member, got {role!r}")
    return role  # type: ignore[return-value]


def _out(user: UserAccount) -> MemberOut:
    # built field-by-field so password_hash can never ride along
    return MemberOut(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        created_at=user.created_at,
    )


async def _require_tenant_member(auth: AuthContext, user_id: str) -> UserAccount:
    """The member must belong to the acting principal's tenant — a foreign
    id resolves to 404 (no existence leak)."""
    user = await auth.repo.get_user(user_id)
    if user is None or user.tenant_id != auth.principal.tenant_id:
        raise ApiError(404, "not_found", f"member {user_id!r} not found")
    return user


def _protect_owner(auth: AuthContext, user: UserAccount) -> None:
    if user.role == "owner" and auth.principal.role != "owner":
        raise ApiError(403, "forbidden", "only owners can modify owners")


@router.get("")
async def list_members(auth: AuthContext = AuthDep) -> MemberList:
    _require_member_manager(auth)
    users = await auth.repo.list_users(auth.principal.tenant_id)
    return MemberList(items=[_out(u) for u in users])


@router.post("", status_code=201)
async def create_member(req: MemberCreate, auth: AuthContext = AuthDep) -> MemberOut:
    _require_member_manager(auth)
    role = _require_role(req.role)
    password_hash = hash_password(req.password) if req.password is not None else None
    try:
        user = await auth.repo.create_user(
            tenant_id=auth.principal.tenant_id,
            email=req.email,
            display_name=req.display_name,
            password_hash=password_hash,
            role=role,
        )
    except IntegrityError:
        raise ApiError(409, "conflict", f"a user with email {req.email!r} already exists") from None
    return _out(user)


@router.patch("/{user_id}")
async def update_member(user_id: str, req: MemberPatch, auth: AuthContext = AuthDep) -> MemberOut:
    _require_member_manager(auth)
    user = await _require_tenant_member(auth, user_id)
    _protect_owner(auth, user)
    role = _require_role(req.role) if req.role is not None else None
    updated = await auth.repo.update_user(user_id, display_name=req.display_name, role=role)
    assert updated is not None  # existence verified above
    return _out(updated)


@router.delete("/{user_id}", status_code=204)
async def delete_member(user_id: str, auth: AuthContext = AuthDep) -> None:
    _require_member_manager(auth)
    user = await _require_tenant_member(auth, user_id)
    _protect_owner(auth, user)
    deleted = await auth.repo.delete_user(user_id)
    if not deleted:
        raise ApiError(
            409,
            "conflict",
            f"member {user_id!r} owns API keys or credentials; delete refused",
        )


__all__ = ["router"]
