"""
Which filesystem backend a specialist gets.

A specialist normally gets the DB-backed ``SessionArtifactBackend``, which never
touches real disk and therefore cannot produce files a process like Metro could
serve. One whose node sets ``config.backend.persistent_workspace`` gets
``deepagents``' real-disk ``FilesystemBackend`` instead.

This is a BACKEND choice and nothing more. Whether those files outlive the pod —
a volume, an object store, neither — is the platform's concern and is not
observable from this process (ADR-036 §10). The flag's name says *persistent*
because that is what a specialist is asking for; it does not name, and must not
imply, any mechanism.
"""

from __future__ import annotations

from typing import Any, Dict


def specialist_wants_persistent_workspace(specialist_config: Dict[str, Any]) -> bool:
    """True if this one specialist opts into a real-disk backend."""
    return bool(specialist_config.get("backend", {}).get("persistent_workspace"))


def is_persistent_workspace_enabled(agent_definition: Dict[str, Any]) -> bool:
    """True if any node in the definition opts in.

    Session-level, because the git repo is scaffolded once per session
    regardless of which specialist ends up doing the writing.
    """
    return any(
        specialist_wants_persistent_workspace(n.get("config", {}))
        for n in agent_definition.get("nodes", [])
    )


def build_workspace_filesystem_backend(root_dir: str = "/") -> object:
    """deepagents' real-disk backend.

    ``root_dir`` is the filesystem root (``"/"``), not ``"/workspace"``. Every
    other path convention here treats ``/workspace/...`` as a real absolute
    path — the DB backend's path handling, ``shell_middleware``'s SKILLS_BASE,
    ``embedded_tool_loader``'s WORKSPACE_ROOT, and critically
    ``subagent_builder``'s FilesystemPermission ACLs (``paths=["/workspace/**"]``).
    With ``virtual_mode=True`` resolving as ``root_dir / path``, a root of
    ``/workspace`` would make the agent's own ``/workspace/...`` calls resolve to
    ``/workspace/workspace/...`` — confirmed against a real pod, where
    ``ls("/workspace")`` and skill loading both failed with ``path_not_found``.

    A root of ``/`` forgoes ``virtual_mode``'s traversal guard, which is moot
    once everything is trivially under the root; the real containment for this
    specialist is the ACL layer in ``subagent_builder``, not this guard.
    """
    from deepagents.backends.filesystem import FilesystemBackend

    return FilesystemBackend(root_dir=root_dir, virtual_mode=True)
