"""MCP server: delegate_to_claude / delegate_to_codex + policy-gated run_command."""

import logging
import os
import uuid
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import (AcceptedElicitation, Elicit, ElicitationResult, MCPServer,
                                  Resolve)
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from mcp_server.delegate import DelegationManager
from mcp_server.issue_service import IssueService
from mcp_server.issue_runner import IssueRunner
from mcp_server.supervisor import SupervisorRuntime
from mcp_server.terminal.executor import TerminalExecutor
from mcp_server.terminal.policy import Level, PolicyError, evaluate, load_policy

log = logging.getLogger("a2a_link.mcp")
BALANCER_URL = os.environ.get("A2A_LINK_BALANCER_URL", "http://127.0.0.1:8000")


def _strip_top_level_title(schema: dict) -> None:
    # The MCP spec's requestedSchema has only type/properties/required; strict clients
    # (Codex) reject pydantic's top-level "title" and cancel the elicitation.
    schema.pop("title", None)


class ConfirmCommand(BaseModel):
    model_config = ConfigDict(json_schema_extra=_strip_top_level_title)

    approve: bool = Field(title="Zatwierdź", description="Zatwierdź wykonanie tej komendy")


class NoConfirmationNeeded(BaseModel):
    """Resolver outcome for commands that are safe or denied; never counts as approval."""


DELEGATIONS = DelegationManager(BALANCER_URL)
ISSUES = IssueService(BALANCER_URL)
ISSUE_HOME = Path(os.environ.get("A2A_LINK_HOME", str(Path.home() / ".a2a_link")))
ISSUE_RUNNER = IssueRunner(ISSUES, SupervisorRuntime(ISSUE_HOME / "supervisor"))
DEFAULT_WAIT_S = 45.0


def _issue_error(exc: Exception) -> dict:
    from mcp_server.issue_service import IssueAPIError
    if isinstance(exc, IssueAPIError):
        return {"status": "error", "http_status": exc.status_code, **exc.payload}
    return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _delegate(agent: str, prompt: str, cwd: str | None, wait_s: float, isolation: str) -> dict:
    started = DELEGATIONS.start(agent, prompt, cwd, isolation=isolation)
    if "task_id" not in started or started["status"] != "in_progress":
        return started  # refused, unreachable balancer, or already finished
    return DELEGATIONS.wait(started["task_id"], wait_s)


def delegate_to_claude(prompt: str, cwd: str | None = None, wait_s: float = DEFAULT_WAIT_S,
                       isolation: str = "auto") -> dict:
    """Deleguj całe zadanie do Claude Code (`claude -p`, edycje plików dozwolone).

    Przy statusie needs_human przekaż użytkownikowi błąd i poproś o pomoc zgodnie z hint.
    prompt: pełny opis zadania; delegowany agent nie zna tej rozmowy.
    cwd: katalog projektu, w którym agent ma pracować.
    wait_s: ile sekund czekać (maks. 55). Jeśli zadanie trwa dłużej, dostaniesz
    status "in_progress" i task_id; odbierz wynik przez get_delegation_result.
    isolation: "auto" (domyślnie: osobny git worktree na gałęzi a2a/<task_id>, gdy cwd jest
    w repozytorium z commitami), "worktree" (wymuś) albo "none" (wspólny katalog).
    Wynik zawiera workspace.merge i workspace.cleanup z gotowymi komendami git.
    """
    return _delegate("claude", prompt, cwd, wait_s, isolation)


def delegate_to_codex(prompt: str, cwd: str | None = None, wait_s: float = DEFAULT_WAIT_S,
                      isolation: str = "auto") -> dict:
    """Deleguj całe zadanie do Codex CLI (`codex exec`, sandbox workspace-write).

    Przy statusie needs_human przekaż użytkownikowi błąd i poproś o pomoc zgodnie z hint.
    prompt: pełny opis zadania; delegowany agent nie zna tej rozmowy.
    cwd: katalog projektu, w którym agent ma pracować.
    wait_s: ile sekund czekać (maks. 55). Jeśli zadanie trwa dłużej, dostaniesz
    status "in_progress" i task_id; odbierz wynik przez get_delegation_result.
    isolation: "auto" (domyślnie: osobny git worktree na gałęzi a2a/<task_id>, gdy cwd jest
    w repozytorium z commitami), "worktree" (wymuś) albo "none" (wspólny katalog).
    Wynik zawiera workspace.merge i workspace.cleanup z gotowymi komendami git.
    """
    return _delegate("codex", prompt, cwd, wait_s, isolation)


def list_delegations(limit: int = 20) -> dict:
    """Lista oddelegowanych zadań wszystkich agentów (najnowsze pierwsze): kto, komu, status, czas.

    Zadania innych sesji widać tylko, gdy działa balancer.
    """
    return DELEGATIONS.recent(limit)


def get_delegation_log(task_id: str, lines: int = 80) -> dict:
    """Przebieg pracy oddelegowanego agenta na żywo: ostatnie `lines` linii dziennika.

    Każda linia ma znacznik czasu od startu zadania, np. "[01:05] -> PowerShell: python -m pytest".
    Gdy agent milczy, co kilkanaście sekund pojawia się linia "... still working".
    """
    return DELEGATIONS.log(task_id, lines)


def get_delegation_result(task_id: str, wait_s: float = DEFAULT_WAIT_S) -> dict:
    """Odbierz wynik oddelegowanego zadania; czeka do wait_s sekund (maks. 55).

    status: "done" (pole result), "failed" (pole error) albo "in_progress" (wywołaj ponownie).
    """
    return DELEGATIONS.wait(task_id, wait_s)


def _load_executor(policy_path: str | None) -> tuple[TerminalExecutor | None, str]:
    if not policy_path:
        return None, "terminal executor disabled: A2A_LINK_POLICY is not set"
    try:
        return TerminalExecutor(load_policy(policy_path)), "ok"
    except (OSError, ValueError, PolicyError) as e:
        return None, f"terminal executor disabled: invalid policy {policy_path}: {e}"


def build_server(policy_path: str | None = None) -> MCPServer:
    server = MCPServer("a2a-link")
    server.tool()(delegate_to_claude)
    server.tool()(delegate_to_codex)
    read_only = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    server.tool(annotations=read_only)(get_delegation_result)
    server.tool(annotations=read_only)(get_delegation_log)
    server.tool(annotations=read_only)(list_delegations)

    @server.tool()
    def open_issue(title: str, goal: str, acceptance: list[str], cwd: str,
                   opened_from: str = "codex", request_id: str | None = None,
                   participants: list[str] | None = None) -> dict:
        """Utwórz trwałe issue na czystej bazie Git i przygotuj osobne worktree uczestników."""
        try:
            if opened_from not in ("claude", "codex"):
                return {"status": "error", "error": "opened_from must be claude or codex"}
            result = ISSUES.open_managed_issue(title, goal, acceptance, cwd,
                                               request_id or str(uuid.uuid4()), opened_from, participants)
            return {"status": "open", **result}
        except Exception as exc:
            return _issue_error(exc)

    @server.tool(annotations=read_only)
    def get_issue_status(issue_id: str) -> dict:
        """Pokaż trwały stan issue, rund, uczestników, prób i integracji."""
        try:
            return ISSUES.snapshot(issue_id)
        except Exception as exc:
            return _issue_error(exc)

    @server.tool()
    def resolve_issue_profile(agent: str, task_type: str,
                              user_override: dict | None = None) -> dict:
        """Rozwiąż profil modelu; jawne ustawienia użytkownika mają pierwszeństwo."""
        try:
            return ISSUES.resolve_profile(agent, task_type, user_override)
        except Exception as exc:
            return _issue_error(exc)

    @server.tool()
    def send_issue_message(issue_id: str, participant: str, prompt: str, task_type: str,
                           user_override: dict | None = None, message_id: str | None = None,
                           wait_s: float = 0) -> dict:
        """Wyślij turę do stałego wykonawcy issue; kolejne wiadomości wznawiają zapisany wątek."""
        try:
            return ISSUE_RUNNER.send(issue_id, participant, prompt, task_type, user_override,
                                     message_id, wait_s)
        except Exception as exc:
            return _issue_error(exc)

    @server.tool()
    def replace_issue_generation(issue_id: str, participant: str, reason: str,
                                 expected_generation: int, expected_version: int,
                                 handoff: dict | None = None) -> dict:
        """Zastąp jedną generację uczestnika; zapisz strukturalny handoff przed nowym startem."""
        try:
            return ISSUES.bump_generation(issue_id, participant,
                                          expected_generation=expected_generation,
                                          reason=reason, handoff=handoff,
                                          expected_version=expected_version)
        except Exception as exc:
            return _issue_error(exc)

    @server.tool(annotations=read_only)
    def get_issue_attempt(attempt_id: str, wait_s: float = 0) -> dict:
        """Odbierz wynik zarządzanej tury albo jej bieżący status."""
        return ISSUE_RUNNER.wait(attempt_id, wait_s)

    @server.tool()
    def abandon_issue_message(issue_id: str, message_id: str, expected_attempt: str) -> dict:
        """Zamknij potwierdzoną jako nieaktywną próbę; zapisany uchwyt wykonawcy pozostaje do następnej wiadomości."""
        try:
            return ISSUE_RUNNER.abandon_message(issue_id, message_id, expected_attempt)
        except Exception as exc:
            return _issue_error(exc)

    @server.tool()
    def propose_issue_change(issue_id: str, round_id: str, by: str, text: str,
                             expected_version: int) -> dict:
        """Zapisz wersjonowaną propozycję do bieżącej rundy issue."""
        try:
            return ISSUES.proposal(issue_id, round_id=round_id, by=by, text=text,
                                   expected_version=expected_version)
        except Exception as exc:
            return _issue_error(exc)

    @server.tool()
    def decide_issue(issue_id: str, round_id: str, proposal_id: str,
                     proposal_version: int, kind: str, by: str = "coordinator",
                     note: str = "", expected_version: int = 0) -> dict:
        """Zapisz accept/reject/request_changes/new_round z kontrolą wersji."""
        try:
            return ISSUES.decide(issue_id, round_id=round_id, proposal_id=proposal_id,
                                 proposal_version=proposal_version, kind=kind, by=by,
                                 note=note, expected_version=expected_version)
        except Exception as exc:
            return _issue_error(exc)

    @server.tool()
    def integrate_issue(issue_id: str, integration_request_id: str, round_id: str,
                        proposal_id: str, proposal_version: int,
                        source_shas: dict[str, str], expected_base_sha: str,
                        verification_argv: list[str], expected_version: int) -> dict:
        """Uruchom merge na worktree integracyjnym, test dokładnym argv i CAS gałęzi bazowej."""
        try:
            return ISSUE_RUNNER.integrate(issue_id, integration_request_id, round_id,
                                          proposal_id, proposal_version, source_shas,
                                          expected_base_sha, verification_argv, expected_version)
        except Exception as exc:
            return _issue_error(exc)

    executor, status = _load_executor(policy_path)
    if executor is None:
        log.warning(status)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def describe_terminal_policy() -> dict:
        """Pokaż, które komendy i katalogi są dozwolone dla run_command."""
        if executor is None:
            return {"enabled": False, "reason": status}
        return {"enabled": True, **executor.describe()}

    def approval(argv: list[str], cwd: str) -> NoConfirmationNeeded | Elicit[ConfirmCommand]:
        """Ask the human (MCP elicitation) only for commands the policy marks `confirm`.

        Runs through the SDK's Resolve/Elicit mechanism, so it works on both the
        2026-07-28 protocol (InputRequiredResult round-trip) and older ones
        (`elicitation/create` mid-call). A client without elicitation support
        gets an error and the command does not run.
        """
        if executor is None:
            return NoConfirmationNeeded()
        decision = evaluate(executor.policy, argv, cwd)
        if decision.level is not Level.CONFIRM:
            return NoConfirmationNeeded()
        return Elicit(
            f"Agent chce uruchomić komendę:\n  {argv!r}\nw katalogu: {decision.cwd}\n"
            f"Powód potwierdzenia: {decision.reason}\nZatwierdzić?",
            ConfirmCommand,
        )

    @server.tool(annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    async def run_command(
        argv: list[str],
        cwd: str,
        confirmation: Annotated[ElicitationResult[ConfirmCommand], Resolve(approval)],
        timeout_s: float | None = None,
    ) -> dict:
        """Uruchom zatwierdzoną komendę lokalnie (bez powłoki).

        argv: program i argumenty jako osobne elementy, np. ["git", "status"].
        Nie ma interpretacji powłoki: potoki, przekierowania i `&&` nie działają.
        cwd: katalog roboczy wewnątrz dozwolonych katalogów (zob. describe_terminal_policy).
        Komendy oznaczone jako "confirm" wymagają zatwierdzenia przez człowieka.
        """
        if executor is None:
            return {"status": "denied", "reason": status}

        human_approved = (isinstance(confirmation, AcceptedElicitation)
                          and isinstance(confirmation.data, ConfirmCommand)
                          and confirmation.data.approve)

        async def confirm(_argv: list[str], _cwd: str, _reason: str) -> bool:
            return human_approved

        try:
            outcome = await executor.run(argv, cwd, timeout_s, confirm)
        except Exception as e:  # surface the cause to the agent instead of an opaque tool error
            log.exception("run_command failed")
            return {"status": "error", "reason": f"{type(e).__name__}: {e}", "argv": argv, "cwd": cwd}
        return outcome.to_dict()

    return server
