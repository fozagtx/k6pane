"""Hermes directory-plugin shim.

Hermes loads this file when the repo is cloned or symlinked into
~/.hermes/plugins/k6pane. The implementation lives in ./k6pane/; pointing this
package's search path there means `<this package>.tools` etc. resolve to the
same files whether k6pane is loaded by Hermes, pip, or pytest.
"""

import os

__path__ = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "k6pane")]
__version__ = "0.3.0"

from .plugin import register  # noqa: E402, F401
