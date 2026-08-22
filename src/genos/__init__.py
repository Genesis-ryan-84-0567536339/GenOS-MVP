"""GenOS MVP lifecycle foundation."""

__version__ = "0.1.0.dev0"

# Distribution application identity is local immutable release configuration,
# not end-user credential state. Loading is bounded/read-only and deliberately
# non-fatal: missing or invalid publisher config keeps optional integrations at
# truthful NOT_CONFIGURED without taking down the GenOS core.
from .distribution_runtime import apply_distribution_environment

apply_distribution_environment()
del apply_distribution_environment
