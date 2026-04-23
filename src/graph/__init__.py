__all__ = [
    "build_graph",
]


def build_graph(*args, **kwargs):
    """Build the workflow graph lazily to keep lightweight imports side-effect free."""
    from .builder import build_graph as _build_graph

    return _build_graph(*args, **kwargs)
