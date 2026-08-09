# jupyterhub_pilot package
# The AI extension (extension.py) requires IPython which is only available
# on Worker VMs inside notebook kernels. Import it lazily so the Hub-side
# infrastructure (spawner, session_store, vault_client, etc.) can be imported
# without IPython being installed.

__version__ = "0.2.0"


def load_ipython_extension(ipython):
    """Lazy entry point — delegates to extension.py when IPython is present."""
    from .extension import load_ipython_extension as _load  # noqa: PLC0415
    _load(ipython)
