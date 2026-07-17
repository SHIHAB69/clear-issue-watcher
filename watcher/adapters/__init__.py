"""Source adapters. Each platform implements the Adapter interface in base.py."""
from .base import Adapter, Event


def build_adapter(meta: dict) -> Adapter:
    """Construct the right adapter from a registry entry."""
    platform = meta.get("platform")
    if platform == "github":
        from .github import GitHubAdapter
        return GitHubAdapter(meta)
    if platform == "jetrix":
        from .jetrix import JetrixAdapter
        return JetrixAdapter(meta)
    raise ValueError(f"unknown platform: {platform}")


__all__ = ["Adapter", "Event", "build_adapter"]
