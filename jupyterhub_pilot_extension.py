# jupyterhub_pilot_extension.py — compatibility shim
# This file exists so users can still do:
#   %load_ext jupyterhub_pilot_extension
# All logic lives in the jupyterhub_pilot package.
from jupyterhub_pilot.extension import load_ipython_extension  # noqa: F401
