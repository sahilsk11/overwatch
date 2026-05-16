from __future__ import annotations

import asyncio
import os
import shlex
from dataclasses import dataclass
from typing import Protocol

MAX_PROVIDER_OUTPUT_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class AgentConfig:
    provider: str
    model: str | None = None
    harness: str | None = None
    cwd: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderRunResult:
    command: str | None
    output: str


class ProviderRunError(RuntimeError):
    def __init__(self, message: str, result: ProviderRunResult) -> None:
        super().__init__(message)
        self.result = result


class CodingAgentProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    async def run(self, prompt: str, config: AgentConfig) -> ProviderRunResult | None: ...


class CliProvider:
    def __init__(self, provider_id: str, command: list[str]) -> None:
        self._provider_id = provider_id
        self._command = command

    @property
    def provider_id(self) -> str:
        return self._provider_id

    def command(self, config: AgentConfig) -> list[str]:
        cmd = list(self._command)
        if config.model:
            cmd.extend(_model_args(self.provider_id, config.model))
        if config.harness:
            cmd.extend(["--harness", config.harness])
        return cmd

    def command_display(self, config: AgentConfig) -> str:
        return shlex.join(self.command(config))

    async def run(self, prompt: str, config: AgentConfig) -> ProviderRunResult:
        cmd = self.command(config)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=config.cwd,
        )
        stdout, stderr = await process.communicate(prompt.encode("utf-8"))
        command = shlex.join(cmd)
        output = _combined_output(stdout, stderr)
        result = ProviderRunResult(command=command, output=output)
        if process.returncode != 0:
            raise ProviderRunError(
                f"{self.provider_id} exited with {process.returncode}: {output}",
                result,
            )
        return result


class ProviderRegistry:
    def __init__(self, providers: list[CodingAgentProvider]) -> None:
        self._providers = {provider.provider_id: provider for provider in providers}

    def get(self, provider_id: str) -> CodingAgentProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            known = ", ".join(sorted(self._providers))
            raise ValueError(f"unknown provider {provider_id!r}; expected one of: {known}")
        return provider


def default_registry() -> ProviderRegistry:
    return ProviderRegistry(
        [
            CliProvider("opencode", _env_command("OVERWATCH_OPENCODE_CMD", ["opencode", "run"])),
            CliProvider(
                "codex",
                _env_command(
                    "OVERWATCH_CODEX_CMD",
                    [
                        "codex",
                        "exec",
                        "--dangerously-bypass-approvals-and-sandbox",
                        "--sandbox",
                        "danger-full-access",
                        "--color",
                        "never",
                    ],
                ),
            ),
            CliProvider(
                "claude-code",
                _env_command("OVERWATCH_CLAUDE_CODE_CMD", ["claude", "--print"]),
            ),
        ]
    )


def _env_command(name: str, default: list[str]) -> list[str]:
    value = os.environ.get(name)
    return shlex.split(value) if value else default


def _model_args(provider_id: str, model: str) -> list[str]:
    if provider_id in {"codex", "claude-code", "opencode"}:
        return ["--model", model]
    return []


def _combined_output(stdout: bytes, stderr: bytes) -> str:
    parts = []
    stdout_text = stdout.decode("utf-8", errors="replace").strip()
    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    if stdout_text:
        parts.append(f"stdout:\n{stdout_text}")
    if stderr_text:
        parts.append(f"stderr:\n{stderr_text}")
    return _truncate_text("\n\n".join(parts), MAX_PROVIDER_OUTPUT_CHARS)


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = f"\n[truncated to last {limit} chars]\n"
    return marker + value[-limit:]
