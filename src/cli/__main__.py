"""Support ``python -m cli``.

Entry mirror of the ``xihe`` console script (``cli.app:main``), so the
agent can be launched as a module once installed (``pip install -e .``).
"""
import sys

from cli.app import main

sys.exit(main() or 0)
