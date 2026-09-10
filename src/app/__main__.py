"""Support ``python -m app``.

Entry mirror of the ``xihe`` console script (``app.main:main``), so the
agent can be launched as a module once installed (``pip install -e .``).
"""
import sys

from app.main import main

sys.exit(main() or 0)
