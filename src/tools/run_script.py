"""Whitelisted script execution tool.

Responsibility: let the agent run a bounded set of pre-approved operational
scripts (e.g. generate weekly orders, compute finances) by name. Only scripts on
an explicit whitelist may run; arbitrary command execution is never permitted.
Captures output and exit status and returns a typed result from `results.py`.

TODO: define the script whitelist and implement guarded execution.
"""

# TODO: implement whitelisted script execution.
