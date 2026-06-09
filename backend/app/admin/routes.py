from fastapi import APIRouter

from app.admin.groups import router as groups_router
from app.admin.rbac import group_roles_router, permissions_router, roles_router
from app.admin.users import router as users_router

router = APIRouter()
router.include_router(users_router)
router.include_router(groups_router)
router.include_router(permissions_router)
router.include_router(roles_router)
router.include_router(group_roles_router)
