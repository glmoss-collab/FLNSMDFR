"""
Versioned system prompts for FLNSMDFR agents.

Prompts live as named string constants in submodules so they can be revised
independently of agent orchestration code. The shape (named prompt + semver
version + registry) is intentionally close to Vertex AI Prompt Management so
the constants in this package can be migrated to managed prompts later
without touching call sites.
"""

from typing import Dict, Tuple

from . import estimation_agent, hvac_skill

# Registry: name -> {version -> prompt text}
_PROMPTS: Dict[str, Dict[str, str]] = {
    "estimation_agent.system": {
        estimation_agent.VERSION: estimation_agent.SYSTEM_V1,
    },
    "hvac_skill.system": {
        hvac_skill.VERSION: hvac_skill.SYSTEM_V1,
    },
}

# Latest version per prompt name. Update when adding a new SYSTEM_VN.
_LATEST: Dict[str, str] = {
    "estimation_agent.system": estimation_agent.VERSION,
    "hvac_skill.system": hvac_skill.VERSION,
}


def get_prompt(name: str, version: str = None) -> str:
    """Return a registered prompt text. Defaults to the latest version."""
    if name not in _PROMPTS:
        raise KeyError(f"Unknown prompt: {name!r}. Available: {list(_PROMPTS)}")
    versions = _PROMPTS[name]
    resolved = version or _LATEST[name]
    if resolved not in versions:
        raise KeyError(
            f"Unknown version {resolved!r} for prompt {name!r}. "
            f"Available: {list(versions)}"
        )
    return versions[resolved]


def list_prompts() -> Dict[str, Tuple[str, ...]]:
    """Return {prompt_name: (version, ...)} for every registered prompt."""
    return {name: tuple(versions) for name, versions in _PROMPTS.items()}


__all__ = ["get_prompt", "list_prompts", "estimation_agent", "hvac_skill"]
