"""Tool implementations for hermes-hub (W3, M1).

``peer_tools`` holds the six ``peer_*`` model tools. They are deliberately
transport-independent from the Hermes plugin runtime: the module imports
nothing from Hermes core, so it is fully testable against a real hub without
a gateway. The plugin package in ``plugin/`` is the only thing that knows
about ``PluginContext``.
"""
