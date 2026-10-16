"""`conduct_pipeline.py`, loaded by path, for the commands that need to pick a node.

`state-manage.py` builds cycles and this module answers "which node does this line start on" by
loading the pipeline module -- the same one the planner lives in -- rather than duplicating the DAG's
ranking rule. Everything else about a cycle is written here, in `cycles.py`.
"""


import importlib.util

from .store import HERE


def _load_pipeline(name: str = 'conduct_pipeline'):
    """`conduct_pipeline.py`, loaded by path -- one fresh module per caller.

    Fresh on every call, never cached: each caller wants a module of its own, and a cached one would
    carry a stale environment between calls in the same process. `conduct_pipeline.py` is loaded the
    same way this package is -- see the bootstrap in `state-manage.py`.
    """
    spec = importlib.util.spec_from_file_location(name, HERE / 'pipeline' / 'conduct_pipeline.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
