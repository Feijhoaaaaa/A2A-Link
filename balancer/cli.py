import os

import click
import httpx

from balancer.updater import UpdateError, update_installation


def _base_url() -> str:
    return os.environ.get("A2A_LINK_BALANCER_URL", "http://127.0.0.1:8000")


def _client() -> httpx.Client:
    return httpx.Client(base_url=_base_url(), timeout=10)


@click.group()
def main():
    """agentctl — sterowanie balancerem A2A_Link."""


@main.command()
def status():
    """Tabela zadań."""
    with _client() as c:
        tasks = c.get("/tasks").raise_for_status().json()
    click.echo(f"{'id':<14}{'status':<14}{'agent':<10}description")
    for t in tasks:
        click.echo(f"{t['id']:<14}{t['status']:<14}{(t['agent'] or '-'):<10}{t['description']}")


@main.command("add-task")
@click.option("--description", required=True)
@click.option("--model-hint", default="")
@click.option("--capability", default=None)
def add_task(description, model_hint, capability):
    """Dodaj zadanie; wypisuje id."""
    with _client() as c:
        r = c.post("/tasks", json={"description": description, "model_hint": model_hint,
                                   "required_capability": capability}).raise_for_status()
    click.echo(r.json()["id"])


@main.command()
def issues():
    """Zadania needs_human + incydenty."""
    with _client() as c:
        tasks = c.get("/tasks", params={"status": "needs_human"}).raise_for_status().json()
        incidents = c.get("/incidents").raise_for_status().json()
    for t in tasks:
        click.echo(f"needs_human {t['id']}: {t['description']} — {t['error']}")
    for i in incidents:
        click.echo(f"incident #{i['id']} [{i['agent']}] {i['kind']}: {i['detail']}")


@main.command("update")
def update():
    """Pobierz aktualizację z oficjalnego GitHuba i zainstaluj ją w tym środowisku."""
    try:
        result = update_installation()
    except UpdateError as exc:
        raise click.ClickException(str(exc)) from exc

    if result["status"] == "up_to_date":
        click.echo(f"A2A-Link {result['to_version']} is already up to date.")
        return
    click.echo(f"Updated A2A-Link {result['from_version']} → {result['to_version']}.")
    click.echo("Restart the balancer, runner, and MCP clients to load the update.")


if __name__ == "__main__":
    main()
