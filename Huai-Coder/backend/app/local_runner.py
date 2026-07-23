"""Local project runner used by the browser bridge.

The browser cannot spawn a native process.  This module is intentionally
stand-alone so it can be started on the user's machine and execute commands
in one explicitly selected project directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any


MAX_OUTPUT = 20_000
DEFAULT_COMMAND_TIMEOUT = 120
DEFAULT_INSTALL_TIMEOUT = 300
MAX_RETRIES = 3

_BLOCKED_COMMANDS = (
    re.compile(r"\b(?:rm|del|erase)\s+(?:-rf|-r|/s|/q)", re.I),
    re.compile(r"\bremove-item\b.*-recurse", re.I),
    re.compile(r"\bformat(?:\.com)?\b", re.I),
    re.compile(r"\b(?:shutdown|restart-computer|stop-computer)\b", re.I),
    re.compile(r"\bgit\s+(?:reset|clean)\b", re.I),
    re.compile(r"\b(?:docker|systemctl)\s+(?:run|start|stop)\b", re.I),
)


@dataclass(frozen=True)
class DependencyPlan:
    ecosystem: str
    manifest: str
    command: str
    fallback_commands: tuple[str, ...] = ()
    runtime_prefix: tuple[str, ...] = ()


@dataclass
class CommandResult:
    ok: bool
    exit_code: int | None
    output: str
    command: str
    duration_ms: int
    attempts: list[dict[str, Any]] = field(default_factory=list)
    dependency_steps: list[dict[str, Any]] = field(default_factory=list)
    changed_paths: list[str] = field(default_factory=list)
    error_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "result": self.output[:MAX_OUTPUT],
            "command": self.command,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
            "dependency_steps": self.dependency_steps,
            "changed_paths": self.changed_paths,
            "error_type": self.error_type,
        }


class LocalRunner:
    """Execute project commands with dependency preparation and bounded retry."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        max_retries: int = MAX_RETRIES,
        command_timeout: int = DEFAULT_COMMAND_TIMEOUT,
        install_timeout: int = DEFAULT_INSTALL_TIMEOUT,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"Workspace does not exist: {self.workspace}")
        self.max_retries = max(1, min(max_retries, MAX_RETRIES))
        self.command_timeout = max(5, command_timeout)
        self.install_timeout = max(30, install_timeout)
        self._lock = threading.Lock()
        self._prepared_signature: str | None = None
        self._runtime_prefix: tuple[str, ...] = ()

    def _relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.workspace)).replace("\\", "/")
        except ValueError:
            return str(path)

    def _signature(self, manifests: list[Path]) -> str:
        values = []
        for manifest in manifests:
            # Size/mtime alone can miss same-size edits on filesystems with
            # coarse timestamp resolution. Hash the manifest contents so a
            # dependency version change always invalidates preparation.
            values.append(
                (
                    self._relative(manifest),
                    hashlib.sha256(manifest.read_bytes()).hexdigest(),
                )
            )
        return hashlib.sha256(
            json.dumps(values, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _manifest_paths(self) -> list[Path]:
        names = {
            "package.json",
            "requirements.txt",
            "pyproject.toml",
            "Pipfile",
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "go.mod",
            "Cargo.toml",
            "Gemfile",
            "composer.json",
        }
        return sorted(
            path
            for path in self.workspace.iterdir()
            if path.is_file() and path.name in names
        )

    def detect_dependencies(self, *, force: bool = False) -> list[DependencyPlan]:
        """Return only deterministic, manifest-derived install plans."""
        plans: list[DependencyPlan] = []
        package = self.workspace / "package.json"
        if package.exists():
            manager = "npm"
            command = "npm install"
            fallbacks: tuple[str, ...] = ()
            if (self.workspace / "pnpm-lock.yaml").exists():
                manager, command = "pnpm", "pnpm install --frozen-lockfile"
                fallbacks = ("pnpm install",)
            elif (self.workspace / "yarn.lock").exists():
                manager, command = "yarn", "yarn install --frozen-lockfile"
                fallbacks = ("yarn install",)
            elif (self.workspace / "package-lock.json").exists():
                command = "npm ci"
                fallbacks = ("npm install", "npm install --legacy-peer-deps")
            if shutil.which(manager):
                if force or not (self.workspace / "node_modules").exists():
                    plans.append(DependencyPlan("node", "package.json", command, fallbacks))

        requirements = self.workspace / "requirements.txt"
        pyproject = self.workspace / "pyproject.toml"
        if requirements.exists() or pyproject.exists() or (self.workspace / "Pipfile").exists():
            venv = self.workspace / ".huai-coder-venv"
            venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if not venv_python.exists():
                plans.append(
                    DependencyPlan(
                        "python",
                        self._relative(requirements if requirements.exists() else pyproject),
                        f'"{sys.executable}" -m venv .huai-coder-venv',
                        (),
                    )
                )
            if requirements.exists():
                plans.append(
                    DependencyPlan(
                        "python",
                        "requirements.txt",
                        "python -m pip install -r requirements.txt",
                        (
                            "python -m pip install --no-cache-dir -r requirements.txt",
                            "python -m pip install --upgrade pip -r requirements.txt",
                        ),
                        (".huai-coder-venv/Scripts" if os.name == "nt" else ".huai-coder-venv/bin",),
                    )
                )
            elif pyproject.exists():
                plans.append(
                    DependencyPlan(
                        "python",
                        "pyproject.toml",
                        "python -m pip install -e .",
                        ("python -m pip install --no-cache-dir -e .",),
                        (".huai-coder-venv/Scripts" if os.name == "nt" else ".huai-coder-venv/bin",),
                    )
                )

        if (self.workspace / "pom.xml").exists() and shutil.which("mvn"):
            plans.append(DependencyPlan("java", "pom.xml", "mvn -q -DskipTests dependency:go-offline"))
        elif (self.workspace / "build.gradle").exists() or (self.workspace / "build.gradle.kts").exists():
            if shutil.which("gradle"):
                plans.append(DependencyPlan("java", "build.gradle", "gradle dependencies"))
        if (self.workspace / "go.mod").exists() and shutil.which("go"):
            plans.append(DependencyPlan("go", "go.mod", "go mod download"))
        if (self.workspace / "Cargo.toml").exists() and shutil.which("cargo"):
            plans.append(DependencyPlan("rust", "Cargo.toml", "cargo fetch"))
        if (self.workspace / "Gemfile").exists() and shutil.which("bundle"):
            plans.append(DependencyPlan("ruby", "Gemfile", "bundle install"))
        if (self.workspace / "composer.json").exists() and shutil.which("composer"):
            plans.append(DependencyPlan("php", "composer.json", "composer install"))
        return plans

    def _environment(self, prefix: tuple[str, ...] = ()) -> dict[str, str]:
        environment = os.environ.copy()
        # The Runner may itself be started with an absolute Python path while
        # that interpreter directory is absent from PATH. Keep `python` and
        # `pip` commands usable even before a project virtualenv is prepared.
        runtime_dir = str(Path(sys.executable).resolve().parent)
        paths = [str((self.workspace / item).resolve()) for item in prefix]
        if runtime_dir not in paths:
            paths.append(runtime_dir)
        if paths:
            environment["PATH"] = os.pathsep.join(paths + [environment.get("PATH", "")])
        return environment

    def _run_process(
        self,
        command: str,
        *,
        timeout: int,
        prefix: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        started = time.monotonic()
        process: subprocess.Popen[str] | None = None
        effective_command = command
        process_command: str | list[str] = effective_command
        use_shell = True
        if os.name == "nt":
            # cmd.exe does not treat single quotes as argument delimiters. The
            # common `python -c '...'` form can therefore silently run the
            # Windows launcher without executing the snippet. Resolve the
            # interpreter explicitly while preserving project venv support.
            python_executable = Path(sys.executable).resolve()
            if prefix:
                candidate = self.workspace / prefix[0] / "python.exe"
                if candidate.exists():
                    python_executable = candidate.resolve()
            effective_command = re.sub(
                r"^python(?:3(?:\.\d+)?)?(?=\s|$)",
                lambda _match: f'"{python_executable}"',
                command,
                count=1,
                flags=re.IGNORECASE,
            )
            inline = re.match(r"^python(?:3(?:\.\d+)?)?\s+-c\s+'(?P<code>.*)'\s*$", command, re.IGNORECASE | re.DOTALL)
            if inline:
                # Avoid cmd.exe's incompatible single-quote parsing for the
                # common Python inline-script form.
                process_command = [str(python_executable), "-c", inline.group("code")]
                use_shell = False
            else:
                process_command = effective_command
        try:
            process = subprocess.Popen(
                process_command,
                cwd=self.workspace,
                shell=use_shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self._environment(prefix),
            )
            output, _ = process.communicate(timeout=timeout)
            return {
                "ok": process.returncode == 0,
                "exit_code": process.returncode,
                "output": (output or "")[-MAX_OUTPUT:],
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        except subprocess.TimeoutExpired:
            if process is not None:
                process.kill()
                process.communicate()
            return {
                "ok": False,
                "exit_code": None,
                "output": f"Command timed out after {timeout} seconds",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }

    def _install_plan(self, plan: DependencyPlan) -> dict[str, Any]:
        commands = (plan.command,) + plan.fallback_commands
        attempts: list[dict[str, Any]] = []
        for command in commands[: self.max_retries]:
            result = self._run_process(
                command,
                timeout=self.install_timeout,
                prefix=plan.runtime_prefix,
            )
            attempts.append({"command": command, **result})
            if result["ok"]:
                return {
                    "ecosystem": plan.ecosystem,
                    "manifest": plan.manifest,
                    "ok": True,
                    "attempts": attempts,
                }
        return {
            "ecosystem": plan.ecosystem,
            "manifest": plan.manifest,
            "ok": False,
            "attempts": attempts,
        }

    def prepare_dependencies(self, *, force: bool = False) -> dict[str, Any]:
        manifests = self._manifest_paths()
        signature = self._signature(manifests) if manifests else "empty"
        if not force and self._prepared_signature == signature:
            return {"ok": True, "steps": [], "skipped": True}
        # A changed manifest must trigger preparation even when an old
        # node_modules directory or virtualenv is still present. Otherwise a
        # newly added dependency would be missed until the command happened to
        # fail with an import/module-not-found error.
        plans = self.detect_dependencies(
            force=force or self._prepared_signature != signature
        )
        steps: list[dict[str, Any]] = []
        for plan in plans:
            step = self._install_plan(plan)
            steps.append(step)
            if not step["ok"]:
                return {"ok": False, "steps": steps, "error_type": "dependency_install_failed"}
            if plan.runtime_prefix:
                self._runtime_prefix = plan.runtime_prefix
        self._prepared_signature = signature
        return {"ok": True, "steps": steps, "skipped": False}

    @staticmethod
    def _is_missing_dependency(output: str) -> bool:
        lowered = output.lower()
        return any(
            marker in lowered
            for marker in (
                "modulenotfounderror",
                "cannot find module",
                "no module named",
                "could not find",
                "command not found",
            )
        ) or re.search(r"package .+ not found", lowered) is not None

    def run(
        self,
        command: str,
        *,
        auto_prepare: bool = True,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        command = command.strip()
        if not command:
            return CommandResult(False, None, "Command is empty", command, 0, error_type="invalid_command").as_dict()
        if any(pattern.search(command) for pattern in _BLOCKED_COMMANDS):
            return CommandResult(False, None, "Command blocked by local runner safety policy", command, 0, error_type="unsafe_command").as_dict()

        with self._lock:
            dependency_steps: list[dict[str, Any]] = []
            if auto_prepare:
                prepared = self.prepare_dependencies()
                dependency_steps = prepared["steps"]
                if not prepared["ok"]:
                    return CommandResult(
                        False,
                        None,
                        "Dependency installation failed; see attempts for details.",
                        command,
                        0,
                        dependency_steps=dependency_steps,
                        error_type="dependency_install_failed",
                    ).as_dict()

            attempts: list[dict[str, Any]] = []
            started = time.monotonic()
            max_attempts = self.max_retries if auto_prepare else 1
            for attempt in range(max_attempts):
                result = self._run_process(
                    command,
                    timeout=timeout_seconds or self.command_timeout,
                    prefix=self._runtime_prefix,
                )
                attempts.append({"attempt": attempt + 1, **result})
                if result["ok"]:
                    return CommandResult(
                        True,
                        result["exit_code"],
                        result["output"],
                        command,
                        int((time.monotonic() - started) * 1000),
                        attempts=attempts,
                        dependency_steps=dependency_steps,
                    ).as_dict()
                if not auto_prepare or not self._is_missing_dependency(result["output"]):
                    break
                prepared = self.prepare_dependencies(force=True)
                dependency_steps.extend(prepared["steps"])
                if not prepared["ok"]:
                    break
            last = attempts[-1] if attempts else {"exit_code": None, "output": "No attempt made"}
            return CommandResult(
                False,
                last.get("exit_code"),
                last.get("output", "Command failed"),
                command,
                int((time.monotonic() - started) * 1000),
                attempts=attempts,
                dependency_steps=dependency_steps,
                error_type="command_failed",
            ).as_dict()


def runner_metadata(runner: LocalRunner) -> dict[str, Any]:
    return {
        "workspace": str(runner.workspace),
        "dependencies": [plan.__dict__ for plan in runner.detect_dependencies()],
        "runtime": {"python": sys.executable, "platform": sys.platform},
    }
