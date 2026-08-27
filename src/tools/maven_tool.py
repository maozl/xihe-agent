"""Maven dependency analysis tool.

Runs Maven (mvn) commands to analyze a project's dependencies and parses the
text output into structured JSON the agent can reason over. Analysis only —
does NOT edit pom.xml (the agent uses `patch` for that, going through the
read-before-edit guard).

Actions:
  - tree          : dependency tree (mvn dependency:tree)
  - conflicts     : version conflicts (dependency:tree -Dverbose, filtered)
  - analyze       : unused-declared + used-undeclared (dependency:analyze)
  - updates       : deps with newer versions (versions:display-dependency-updates)
  - effective_pom : fully-resolved pom XML (help:effective-pom)

Gated by check_fn: the tool disappears entirely if `mvn` is not on PATH.
"""

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from tools import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_MVN = shutil.which("mvn")  # absolute path (mvn.cmd on Windows) or None
_DEFAULT_TIMEOUT = 180
_MAX_OUTPUT = 30000          # chars; head+tail truncation beyond this
_MVN_FAILURE_TAIL = 30       # lines of stderr/stdout tail on non-zero exit


def _check_maven() -> bool:
    return _MVN is not None


def _run_mvn(args: list, project: str, timeout: int) -> tuple[int, str, str]:
    """Run `mvn <args>` in `project`. Returns (returncode, stdout, stderr)."""
    env = dict(os.environ)
    # Keep Maven's own env hints if present.
    for k in ("JAVA_HOME", "M2_HOME", "MAVEN_OPTS", "MAVEN_HOME"):
        if k in os.environ:
            env[k] = os.environ[k]

    cmd = [_MVN] + args
    logger.info("maven_dep run: %s (cwd=%s)", " ".join(cmd), project)
    # run_interruptible registers the child so /stop can kill it mid-build.
    from tools.interrupt import run_interruptible
    proc = run_interruptible(
        cmd,
        cwd=project,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _tail(text: str, n: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:]) if len(lines) > n else text


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    head = text[:12000]
    tail = text[-15000:]
    omitted = len(text) - 27000
    return head + f"\n\n... [TRUNCATED - {omitted:,} chars omitted] ...\n\n" + tail


def _err_result(action: str, rc: int, stdout: str, stderr: str) -> str:
    """Build a tool_error for a failed mvn run, surfacing the useful tail."""
    blob = stderr.strip() or stdout.strip()
    tail = _tail(blob, _MVN_FAILURE_TAIL)
    return tool_error(
        f"mvn {action} failed (exit {rc}). Tail of output:\n{tail}"
    )


_CONFLICT_RE = re.compile(r"omitted for conflict with|omitted for duplicate")
_DEP_LINE_RE = re.compile(r"^[^\s].*-[0-9]")

# A tree line looks like:  `[INFO] +- org.group:artifact:packaging:version:scope`
# or with a conflict marker suffix.
_TREE_DEP_RE = re.compile(
    r"([a-zA-Z0-9_.-]+):([a-zA-Z0-9_.-]+):([a-zA-Z0-9_.-]+):([a-zA-Z0-9_.-]+)(?::([a-zA-Z0-9_.-]+))?"
)


def _parse_tree(text: str) -> dict:
    """Parse `mvn dependency:tree` output.

    Returns {deps:[{group,artifact,version,type,scope}], conflicts:[...lines]}.
    """
    deps = []
    seen = set()
    conflicts = []
    for line in text.splitlines():
        if "[INFO]" not in line and "[DEBUG]" not in line and not line.strip():
            continue
        # strip leading [INFO] / [DEBUG] / markers for matching
        body = re.sub(r"^\s*(\[INFO\]|\[DEBUG\]|\\|\+|\||-|\s)+", "", line)
        m = _TREE_DEP_RE.search(body)
        if m:
            g, a, pkg, ver = m.group(1), m.group(2), m.group(3), m.group(4)
            scope = m.group(5) or ""
            key = (g, a, ver, scope)
            if key not in seen:
                seen.add(key)
                deps.append({"group": g, "artifact": a, "version": ver,
                             "type": pkg, "scope": scope})
        if _CONFLICT_RE.search(line):
            conflicts.append(line.strip())
    return {"deps": deps, "conflicts": conflicts}


def _parse_analyze(text: str) -> dict:
    """Parse `mvn dependency:analyze`. Returns {unused_declared, used_undeclared}."""
    def _collect(section_marker: str) -> list[str]:
        out = []
        capturing = False
        for line in text.splitlines():
            if section_marker in line:
                capturing = True
                continue
            if capturing:
                stripped = line.strip()
                if not stripped:
                    if out:
                        break  # blank line ends the list (after we've collected)
                    continue
                # stop at next section heading
                if re.match(r"^\[INFO\]\s*(Used|Unused|No)", stripped):
                    break
                coord = re.sub(r"^\[INFO\]\s*", "", line).strip()
                if coord and not coord.startswith(("Used", "Unused", "No ")):
                    out.append(coord)
        return out

    return {
        "unused_declared": _collect("Unused declared"),
        "used_undeclared": _collect("Used undeclared"),
    }


def _parse_updates(text: str) -> list[dict]:
    """Parse `versions:display-dependency-updates`.

    Lines look like:  `   org.group:artifact .... --> 1.2.3`
    """
    out = []
    pat = re.compile(
        r"([a-zA-Z0-9_.-]+):([a-zA-Z0-9_.-]+)[\s.]*->\s*([0-9][^\s]*)"
    )
    for line in text.splitlines():
        m = pat.search(line)
        if m:
            out.append({"group": m.group(1), "artifact": m.group(2),
                        "new_version": m.group(3)})
    return out


def _maven_dep(args: dict, **kw) -> str:
    if not _MVN:
        return tool_error("mvn not found on PATH. Install Maven to use maven_dep.")

    action = (args.get("action") or "tree").strip().lower()
    project = args.get("project", "").strip()
    if not project:
        return tool_error("project is required (directory containing pom.xml)")
    proj_path = Path(project).expanduser()
    if not proj_path.exists():
        return tool_error(f"project path not found: {proj_path}")
    if not (proj_path / "pom.xml").exists() and not (proj_path.is_dir() and any(proj_path.glob("pom.xml"))):
        return tool_error(f"no pom.xml found in: {proj_path}")

    includes = (args.get("includes") or "").strip()
    verbose = bool(args.get("verbose", False))
    timeout = min(int(args.get("timeout", _DEFAULT_TIMEOUT)), 600)
    offline = bool(args.get("offline", False))

    common = ["-q", "--batch-mode", "-N"]  # -N: non-recursive for multi-module safety
    if offline:
        common.append("-o")  # mvn offline: use local cache only, skip remote repo queries

    try:
        if action == "tree":
            # Write tree to a temp file to dodge download-progress noise on stdout.
            with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as tf:
                out_file = tf.name
            mvn_args = common + [
                "dependency:tree",
                f"-DoutputFile={out_file}",
                "-DoutputType=text",
            ]
            if verbose:
                mvn_args.append("-Dverbose=true")
            if includes:
                mvn_args.append(f"-Dincludes={includes}")
            rc, so, se = _run_mvn(mvn_args, str(proj_path), timeout)
            if rc != 0:
                return _err_result(action, rc, so, se)
            try:
                with open(out_file, encoding="utf-8", errors="replace") as f:
                    tree_text = f.read()
            finally:
                try:
                    os.unlink(out_file)
                except OSError:
                    pass
            parsed = _parse_tree(tree_text)
            return tool_result(action="tree", project=str(proj_path),
                               **parsed, raw_tree=_truncate(tree_text))

        if action == "conflicts":
            mvn_args = common + ["dependency:tree", "-Dverbose=true"]
            if includes:
                mvn_args.append(f"-Dincludes={includes}")
            rc, so, se = _run_mvn(mvn_args, str(proj_path), timeout)
            if rc != 0:
                return _err_result(action, rc, so, se)
            parsed = _parse_tree(so)
            return tool_result(action="conflicts", project=str(proj_path),
                               conflicts=parsed["conflicts"])

        if action == "analyze":
            rc, so, se = _run_mvn(
                common + ["dependency:analyze", "-DignoreNonCompile=true"],
                str(proj_path), timeout,
            )
            if rc != 0:
                return _err_result(action, rc, so, se)
            return tool_result(action="analyze", project=str(proj_path),
                               **_parse_analyze(so))

        if action == "updates":
            rc, so, se = _run_mvn(
                common + ["versions:display-dependency-updates",
                          "-DprocessDependencyManagement=true"],
                str(proj_path), timeout,
            )
            if rc != 0:
                return _err_result(action, rc, so, se)
            return tool_result(action="updates", project=str(proj_path),
                               updates=_parse_updates(so))

        if action == "effective_pom":
            rc, so, se = _run_mvn(
                common + ["help:effective-pom", "-Doutput=effective-pom.xml"],
                str(proj_path), timeout,
            )
            if rc != 0:
                return _err_result(action, rc, so, se)
            ep = proj_path / "effective-pom.xml"
            if ep.exists():
                try:
                    content = ep.read_text(encoding="utf-8", errors="replace")
                finally:
                    try:
                        ep.unlink()
                    except OSError:
                        pass
            else:
                content = so
            return tool_result(action="effective_pom", project=str(proj_path),
                               pom=_truncate(content))

        return tool_error(
            f"Unknown action: '{action}'. Use: tree, conflicts, analyze, updates, effective_pom"
        )
    except subprocess.TimeoutExpired:
        return tool_error(f"mvn {action} timed out after {timeout}s")
    except Exception as e:
        return tool_error(f"maven_dep {action} failed: {e}")


registry.register(
    name="maven_dep",
    toolset="dev_tool",
    schema={
        "type": "function",
        "function": {
            "name": "maven_dep",
            "description": (
                "Analyze a Maven project's dependencies by running mvn. Specify the "
                "project directory (containing pom.xml) and an action. Analysis only — "
                "to modify pom.xml use the patch tool. Actions: "
                "tree (dependency tree + conflicts), conflicts (version conflicts only), "
                "analyze (unused-declared / used-undeclared dependencies), "
                "updates (dependencies that have newer versions available), "
                "effective_pom (fully-resolved pom XML)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Path to the directory containing pom.xml (required).",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["tree", "conflicts", "analyze", "updates", "effective_pom"],
                        "description": "Analysis action (default: tree).",
                    },
                    "includes": {
                        "type": "string",
                        "description": "Optional groupId[:artifactId] filter for tree/conflicts (e.g. 'org.slf4j').",
                    },
                    "verbose": {
                        "type": "boolean",
                        "description": "For tree: include conflicting/omitted paths (verbose tree).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 180, max 600).",
                    },
                    "offline": {
                        "type": "boolean",
                        "description": "Run mvn in offline mode (-o): use local repo cache only, skip all remote queries. Use this to avoid hanging on slow/unreachable remote repos, especially for tree/analyze (faster). Note: updates action needs remote to find newer versions, so offline updates will report none.",
                    },
                },
                "required": ["project"],
            },
        },
    },
    handler=lambda args, **kw: _maven_dep(args, **kw),
    check_fn=_check_maven,
    read_only=True,
)
