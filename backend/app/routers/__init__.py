"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import delivery, resource

# 交付路由含 /api/field-delivery 等固定路径，需先于资源动态路由注册。
ROUTERS = [delivery, resource]
