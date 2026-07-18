"""
SkillRegistry — discovers, indexes, and serves SKILL.md files.

Three scopes (highest-precedence first, workspace wins on name conflict):
  1. workspace  — <cwd>/.skills/<name>/SKILL.md
  2. personal   — ~/.yuyutsava/skills/<name>/SKILL.md  (runtime-written)
  3. bundled    — <package>/skills/bundled/<agent>/<name>/SKILL.md

The index_block() method returns a compact XML snippet (~24 tokens/skill)
suitable for injection into any agent's system prompt.  Full skill bodies
are loaded on demand via get_body().
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from yuyutsava.core.config import LIMITS

logger = logging.getLogger("yuyutsava.skills")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_MAX_INDEX_CHARS = LIMITS.max_skill_index_chars
_MAX_DESC_CHARS = LIMITS.max_skill_desc_chars


@dataclass
class SkillMeta:
    name: str
    description: str
    path: Path   # absolute path to SKILL.md
    scope: str   # "bundled" | "personal" | "workspace"
    agent: str | None = None  # which agent this skill belongs to (bundled only)
    requires_tools: tuple[str, ...] = ()  # e.g. ("ws_*",) — picked up by BaseSubAgent
    platforms: tuple[str, ...] = ()  # OS families this skill applies to; empty = all


class SkillRegistry:
    """Discovers and serves SKILL.md files across all scopes."""

    def __init__(
        self,
        *,
        home_dir: Path | None = None,
        workspace_dir: Path | None = None,
        bundled_dir: Path | None = None,
    ) -> None:
        self._home_dir = home_dir or (Path.home() / ".yuyutsava" / "skills")
        self._workspace_dir = workspace_dir or (Path.cwd() / ".skills")
        self._bundled_dir = bundled_dir or (Path(__file__).parent / "bundled")
        self._cache: list[SkillMeta] | None = None

    def scan(self, agent: str | None = None) -> list[SkillMeta]:
        """Return all discovered skills, optionally filtered by agent scope."""
        skills = self._load_all()
        if agent is None:
            return skills
        return [s for s in skills if s.agent is None or s.agent == agent]

    def get_body(self, name: str) -> str:
        """Read and return the full SKILL.md content for a skill by name."""
        for skill in self._load_all():
            if skill.name == name:
                try:
                    return skill.path.read_text(encoding="utf-8")
                except OSError as exc:
                    return f"error reading skill {name!r}: {exc}"
        return f"skill {name!r} not found"

    def get_meta(self, name: str) -> SkillMeta | None:
        """Return the :class:`SkillMeta` for a skill by name, or None."""
        slug = _slugify(name)
        for skill in self._load_all():
            if skill.name == slug:
                return skill
        return None

    def write_skill(
        self, name: str, description: str, body: str, *, agent: str | None = None
    ) -> str:
        """Write a new skill to the personal scope (~/.yuyutsava/skills/).

        ``agent`` scopes the skill to one agent (None = global, visible to
        every agent's scan/search). Returns the slug the skill was written
        under, so callers can index it into the semantic store (see
        skills/tools.py dual-write).
        """
        slug = _slugify(name)
        skill_dir = self._home_dir / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        desc_clean = description.strip()[:_MAX_DESC_CHARS]
        agent_line = f"agent: {agent}\n" if agent else ""
        content = (
            f"---\nname: {slug}\ndescription: |\n  {desc_clean}\n{agent_line}---\n\n"
            f"{body.strip()}\n"
        )
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
        self._cache = None  # invalidate cache
        logger.info(
            "skills: wrote personal skill %r (agent=%s) → %s", slug, agent, skill_dir
        )
        return slug

    def index_block(self, agent: str | None = None) -> str:
        """Build an XML index block for injection into a system prompt."""
        skills = self.scan(agent=agent)
        if not skills:
            return ""
        lines: list[str] = ["<available_skills>"]
        total = len("<available_skills>\n</available_skills>\n")
        for s in skills:
            desc = s.description.replace("<", "&lt;").replace(">", "&gt;")[:_MAX_DESC_CHARS]
            entry = (
                f"  <skill>\n"
                f"    <name>{s.name}</name>\n"
                f"    <description>{desc}</description>\n"
                f"    <scope>{s.scope}</scope>\n"
                f"  </skill>\n"
            )
            if total + len(entry) > _MAX_INDEX_CHARS:
                break
            lines.append(entry)
            total += len(entry)
        lines.append("</available_skills>")
        return "".join(lines)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_all(self) -> list[SkillMeta]:
        if self._cache is not None:
            return self._cache

        seen: dict[str, SkillMeta] = {}  # name → first (highest precedence) wins

        # 1. workspace (./.skills/)
        for skill in _scan_dir(self._workspace_dir, scope="workspace"):
            seen.setdefault(skill.name, skill)

        # 2. personal (~/.yuyutsava/skills/)
        for skill in _scan_dir(self._home_dir, scope="personal"):
            seen.setdefault(skill.name, skill)

        # 3. bundled (per-agent subdirs inside package/skills/bundled/)
        if self._bundled_dir.exists():
            for agent_dir in sorted(self._bundled_dir.iterdir()):
                if agent_dir.is_dir():
                    for skill in _scan_dir(agent_dir, scope="bundled", agent=agent_dir.name):
                        seen.setdefault(skill.name, skill)

        # Drop skills tagged for other OS families — a Windows triage playbook
        # must never surface (index, recall, or get_body) on macOS/Linux.
        from yuyutsava.platform import host_profile

        fam = host_profile().os_family
        self._cache = [
            s for s in seen.values() if not s.platforms or fam in s.platforms
        ]
        return self._cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scan_dir(directory: Path, *, scope: str, agent: str | None = None) -> list[SkillMeta]:
    """Scan a skill directory for <name>/SKILL.md entries."""
    if not directory.exists():
        return []
    skills: list[SkillMeta] = []
    for entry in sorted(directory.iterdir()):
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.exists():
            continue
        meta = _parse_frontmatter(skill_file, scope=scope, agent=agent)
        if meta:
            skills.append(meta)
    return skills


def _parse_frontmatter(path: Path, *, scope: str, agent: str | None) -> SkillMeta | None:
    """Parse YAML frontmatter from a SKILL.md file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            fm = {}
    else:
        fm = {}

    name = str(fm.get("name") or path.parent.name)
    # Personal/workspace skills may carry an ``agent:`` frontmatter key (written
    # by write_skill(agent=…)); for bundled skills the per-agent dir name is the
    # default, but explicit frontmatter wins in every scope.
    fm_agent = fm.get("agent")
    if fm_agent:
        agent = str(fm_agent).strip() or agent
    raw_desc = fm.get("description") or ""
    # yaml may parse multi-line block scalar as a string with newlines
    description = " ".join(str(raw_desc).split())[:_MAX_DESC_CHARS]
    if not description:
        # Fall back to first non-empty line of body
        body_lines = text[m.end() if m else 0:].strip().splitlines()
        description = next((l.lstrip("# ").strip() for l in body_lines if l.strip()), "")

    # ``requires_tools`` — optional list of tool-name globs the skill needs
    # exposed at build time. BaseSubAgent reads this to decide whether to
    # attach ws_* search tools (and future categories) to an agent.
    raw_req = fm.get("requires_tools") or []
    if isinstance(raw_req, str):
        raw_req = [raw_req]
    requires_tools: tuple[str, ...] = tuple(
        str(r).strip() for r in raw_req if isinstance(r, (str, int, float)) and str(r).strip()
    )

    # ``platforms`` — optional list of OS families ("windows"/"macos"/"linux")
    # this skill applies to. Empty means all. Filtered against the host in
    # SkillRegistry._load_all so a Windows playbook never surfaces on macOS.
    raw_plat = fm.get("platforms") or []
    if isinstance(raw_plat, str):
        raw_plat = [raw_plat]
    platforms: tuple[str, ...] = tuple(
        str(p).strip().lower() for p in raw_plat if str(p).strip()
    )

    return SkillMeta(
        name=_slugify(name),
        description=description,
        path=path,
        scope=scope,
        agent=agent,
        requires_tools=requires_tools,
        platforms=platforms,
    )


def _slugify(name: str) -> str:
    """Normalize to lowercase-hyphenated identifier."""
    slug = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "skill"
