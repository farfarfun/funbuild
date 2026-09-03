"""FastAPI 路由注册辅助工具。

提供 `api_route` 装饰器: 先把被装饰的类方法登记到模块级注册表, 再由
`add_api_routes` 在拿到具体 router 实例时统一挂载, 用于类方法定义路由但
实际注册要等实例存在之后再进行的场景。
"""

from collections.abc import Callable
from functools import wraps
from typing import Any

_api_routes_registry: list[dict[str, Any]] = []


class api_route:
    """标记一个类方法为 API 路由, 实际挂载延迟到 `add_api_routes` 执行。"""

    def __init__(self, path: str, **kwargs: Any) -> None:
        """记录路由路径及透传给 `router.add_api_route` 的其余参数。

        参数:
            path: 路由路径, 如 "/users"。
            **kwargs: 透传给 `router.add_api_route` 的其余关键字参数
                (如 methods、response_model 等)。
        返回:
            无。
        """
        self._path = path
        self._kwargs = kwargs

    def __call__(self, fn: Callable) -> Callable:
        """把被装饰的方法登记进 `_api_routes_registry`, 并原样返回一个包装函数。

        参数:
            fn: 被装饰的类方法。
        返回:
            包装后的函数, 行为与原函数一致(仅透传调用), 不改变调用方式。
        """
        cls, method = fn.__repr__().split(" ")[1].split(".")
        _api_routes_registry.append(
            {
                "fn": fn,
                "path": self._path,
                "kwargs": self._kwargs,
                "cls": cls,
                "method": method,
            }
        )

        @wraps(fn)
        def decorated(*args, **kwargs):
            return fn(*args, **kwargs)

        return decorated


def add_api_routes(router: Any) -> None:
    """把注册表中属于该 router 所属类的路由逐一挂载到 router 实例上。

    参数:
        router: FastAPI 的 `APIRouter` (或兼容 `add_api_route` 接口的对象) 实例;
            通过类名匹配 `_api_routes_registry` 中登记的路由。
    返回:
        无。
    """
    for reg in _api_routes_registry:
        if router.__class__.__name__ == reg["cls"]:
            router.add_api_route(path=reg["path"], endpoint=getattr(router, reg["method"]), **reg["kwargs"])
