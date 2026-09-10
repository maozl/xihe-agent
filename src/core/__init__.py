"""Core agent engine — model interaction, sessions, compression, prompts.

Deliberately imports nothing: every tool module pulls ``core.config`` at
import time, and eager re-exports here used to drag ``core.agent`` (→
openai + httpx, ~2.5s) into ``load_all_tools``. Import submodules directly.
"""
