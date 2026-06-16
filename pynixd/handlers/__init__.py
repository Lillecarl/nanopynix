"""Handler package — importing all modules registers them in HANDLER_REGISTRY."""

from . import add_build_log  # noqa: F401
from . import add_indirect_root  # noqa: F401
from . import add_multiple_to_store  # noqa: F401
from . import add_perm_root  # noqa: F401
from . import add_temp_root  # noqa: F401
from . import add_to_store  # noqa: F401
from . import add_to_store_nar  # noqa: F401
from . import build_derivation  # noqa: F401
from . import build_paths  # noqa: F401
from . import build_paths_with_results  # noqa: F401
from . import collect_garbage  # noqa: F401
from . import nar_from_path  # noqa: F401
from . import optimise_store  # noqa: F401
from . import pynixd_collect_garbage  # noqa: F401
from . import query_missing  # noqa: F401
from . import set_options  # noqa: F401
from . import sign_path_info  # noqa: F401
from . import verify_store  # noqa: F401
