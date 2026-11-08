"""Where a deployment keeps its own files (settings, team mapping, SSH keys).

The code ships in the pip package; each machine's config lives here. Override
with JUPYTERHUB_PILOT_DIR (the repo-root jupyterhub_config.py sets it to the
clone, so clone-based installs keep their files where they were).
"""

import os

CONFIG_DIR: str = os.environ.get("JUPYTERHUB_PILOT_DIR", "/opt/jupyterhub-pilot")
PACKAGE_DIR: str = os.path.dirname(os.path.abspath(__file__))

# Free TCP port on a worker, printed to stdout. Run there over SSH as the user;
# inline so workers need no helper file.
GET_PORT_CMD: str = (
    "python3 -c \"import socket; s=socket.socket(); s.bind(('', 0)); "
    "print(s.getsockname()[1]); s.close()\""
)
