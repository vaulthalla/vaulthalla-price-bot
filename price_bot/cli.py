from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from price_bot.config import load_settings

app = typer.Typer(help="Vaulthalla object-storage pricing profile tooling.")
db_app = typer.Typer(help="Database commands.")
sources_app = typer.Typer(help="Provider source commands.")
console = Console()


@db_app.command("init")
def db_init() -> None:
    from price_bot.db.migrations import init_db

    init_db()
    console.print("[green]Database schema initialized.[/green]")


@sources_app.command("list")
def sources_list() -> None:
    from price_bot.providers import all_fetchers

    table = Table(title="Provider sources")
    table.add_column("Provider")
    table.add_column("Kind")
    table.add_column("Source")
    table.add_column("Fixture")
    for fetcher in all_fetchers():
        for source in fetcher.sources():
            table.add_row(
                fetcher.provider_key,
                source.kind,
                source.url or "curated",
                source.fixture_name or "-",
            )
    console.print(table)


@app.command()
def refresh(
    provider: Annotated[str | None, typer.Option(help="Provider key to refresh.")] = None,
    live: Annotated[bool, typer.Option(help="Allow live provider requests.")] = False,
    fixture_dir: Annotated[
        Path | None, typer.Option(help="Directory containing provider fixture payloads.")
    ] = None,
) -> None:
    from price_bot.db.store import Store
    from price_bot.providers import selected_fetchers
    from price_bot.providers.base import make_catalog_version

    settings = load_settings()
    store = Store.from_settings(settings)
    catalog_version = make_catalog_version()
    allow_live = live or settings.live_enabled

    with store.session() as session:
        store.seed_provider_sources(session)
        for fetcher in selected_fetchers(provider):
            result = fetcher.fetch(live=allow_live, fixture_dir=fixture_dir)
            snapshot = store.record_fetch_result(session, fetcher, result, settings.raw_dir)
            profiles = fetcher.build_profiles(result.payload, catalog_version, snapshot.sha256)
            store.upsert_profiles(session, profiles)
            console.print(
                f"[green]{fetcher.provider_key}[/green]: stored snapshot {snapshot.sha256[:12]} "
                f"and {len(profiles)} profile(s)"
            )
        session.commit()


@app.command()
def normalize(
    provider: Annotated[str | None, typer.Option(help="Provider key to normalize.")] = None,
) -> None:
    from price_bot.db.store import Store
    from price_bot.providers import selected_fetchers
    from price_bot.providers.base import make_catalog_version

    settings = load_settings()
    store = Store.from_settings(settings)
    catalog_version = make_catalog_version()

    with store.session() as session:
        for fetcher in selected_fetchers(provider):
            snapshot = store.latest_snapshot(session, fetcher.provider_key)
            if snapshot is None:
                raise typer.BadParameter(f"No raw snapshot found for {fetcher.provider_key}")
            payload = store.read_snapshot_payload(snapshot.blob_uri)
            profiles = fetcher.build_profiles(payload, catalog_version, snapshot.sha256)
            store.upsert_profiles(session, profiles)
            console.print(f"[green]{fetcher.provider_key}[/green]: normalized {len(profiles)} profile(s)")
        session.commit()


@app.command()
def publish(
    out: Annotated[Path, typer.Option(help="Output directory for public v1 artifacts.")] = Path(
        "public/v1"
    ),
) -> None:
    from price_bot.db.store import Store
    from price_bot.publish.artifacts import publish_catalog

    settings = load_settings()
    store = Store.from_settings(settings)
    with store.session() as session:
        manifest = publish_catalog(session, store, out, settings)
        session.commit()
    console.print(f"[green]Published[/green] {manifest.profile_count} profiles to {out}")


@app.command("mirror-artifacts")
def mirror_artifacts_command(
    out: Annotated[Path, typer.Option(help="Output directory for mirrored public v1 artifacts.")],
    source: Annotated[
        Path | None,
        typer.Option(help="Source public v1 artifact directory. Defaults to PRICE_BOT_PUBLIC_DIR."),
    ] = None,
    include_public_key: Annotated[
        bool,
        typer.Option(help="Copy the configured public key as public-key.pem."),
    ] = False,
) -> None:
    from price_bot.publish.artifacts import mirror_artifacts

    settings = load_settings()
    mirror_artifacts(
        source or settings.public_dir,
        out,
        settings,
        include_public_key=include_public_key,
    )
    console.print(f"[green]Mirrored[/green] signed artifacts to {out}")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port.")] = 8080,
) -> None:
    uvicorn.run("price_bot.api.main:create_app", factory=True, host=host, port=port)


@app.command()
def validate(manifest: Annotated[Path, typer.Argument(help="Path to manifest.json")]) -> None:
    from price_bot.publish.artifacts import validate_manifest

    validate_manifest(manifest, load_settings())
    console.print(f"[green]Validated[/green] {manifest}")


@app.command()
def estimate(
    profile: Annotated[Path, typer.Option(help="Rating profile JSON path.")],
    usage: Annotated[Path, typer.Option(help="Usage JSON path.")],
) -> None:
    from price_bot.normalizer.estimator import estimate_from_files

    result = estimate_from_files(profile, usage)
    console.print_json(data=result.model_dump(mode="json"))


app.add_typer(db_app, name="db")
app.add_typer(sources_app, name="sources")


if __name__ == "__main__":
    app()
