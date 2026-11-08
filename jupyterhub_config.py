# Kept for installs made with `git clone` (0.4.0 and earlier): their systemd unit
# runs `jupyterhub -f /opt/jupyterhub-pilot/jupyterhub_config.py`. Settings stay
# next to this file; the real config now ships in the package.
import os

import jupyterhub_pilot

os.environ.setdefault("JUPYTERHUB_PILOT_DIR", os.path.dirname(os.path.abspath(__file__)))
load_subconfig(os.path.join(os.path.dirname(jupyterhub_pilot.__file__), "jupyterhub_config.py"))  # type: ignore[name-defined]  # noqa: F821
