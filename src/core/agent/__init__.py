"""Agent engine layer — the XiheAgent loop and the modules only it consumes
(prompts, prompt context files, compressor, auxiliary client, model catalog,
title generator). Sits above the root contracts (config/session/registry/
toolsets) and shares support/ machinery with everything else."""

from core.agent.agent import XiheAgent

__all__ = ["XiheAgent"]
