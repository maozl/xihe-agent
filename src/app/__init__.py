"""App layer — the `xihe` process launcher.

The one place that knows all three run modes: it parses the command line and
dispatches into cli (chat/doctor), gateway (gateway + serve), and core
(SharedContext lives there, not here — the modes build their own). Import
``app.main`` for the entry point; there is nothing else to import."""
