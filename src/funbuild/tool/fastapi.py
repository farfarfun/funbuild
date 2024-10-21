from functools import wraps

_api_routes_registry = []


class api_route(object):  # noqa: UP004
    def __init__(self, path, **kwargs):
        self._path = path
        self._kwargs = kwargs

    def __call__(self, fn):
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


def add_api_routes(router):
    for reg in _api_routes_registry:
        if router.__class__.__name__ == reg["cls"]:
            router.add_api_route(path=reg["path"], endpoint=getattr(router, reg["method"]), **reg["kwargs"])
