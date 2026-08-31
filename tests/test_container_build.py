"""Guards on the container build, read from the files rather than from an image.

`docker-compose.yml` builds with context `.`, so everything at the repository root is
sent to the daemon: `.env`, the Telethon `*.session` auth-key databases and their
`-wal`/`-shm`/`-journal` sidecars, `logs/`, `.git`. Git-ignored is not build-ignored.
With a local daemon that is merely wasteful; with a remote or CI builder it is
credential transfer to a third party, with nothing on screen to say so. `.dockerignore`
is the only thing that stops it, and nothing else in this suite reads that file.

The image is also the one artifact this project builds without `uv.lock`, which is why
the Dockerfile assertions below exist.

These tests read text. They prove the configuration says the right thing, not that the
image builds or runs — only a real `docker build` shows that, and CI is where it
happens.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCKERIGNORE = REPO / ".dockerignore"
DOCKERFILE = REPO / "Dockerfile"
COMPOSE = REPO / "docker-compose.yml"

# Everything here is git-ignored and none of it belongs in a build context.
MUST_DENY = (".env*", "*.session*", "secrets.md", ".git/", "logs/")


def _patterns():
    """The `.dockerignore` lines that carry a rule, in file order.

    Order matters: docker applies last-match-wins, so a negation only re-includes a
    file when it comes after the pattern that excluded it.
    """
    return [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _published_ports():
    """The `ports:` entries of `docker-compose.yml`, read without a YAML parser.

    PyYAML reaches this environment only as a transitive dependency of pre-commit, and
    a guard that vanishes when someone installs a smaller dev set is not a guard.
    """
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    entries = []
    for index, line in enumerate(lines):
        if line.strip() != "ports:":
            continue
        indent = len(line) - len(line.lstrip())
        for follower in lines[index + 1 :]:
            stripped = follower.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if len(follower) - len(follower.lstrip()) <= indent or not stripped.startswith("- "):
                break
            entries.append(stripped[2:].strip().strip("\"'"))
    return entries


def test_the_build_context_denies_every_credential_bearing_path():
    assert DOCKERIGNORE.is_file(), (
        "No .dockerignore, so `docker compose build` sends .env and every *.session "
        "auth-key database to the daemon."
    )

    missing = [pattern for pattern in MUST_DENY if pattern not in _patterns()]
    assert not missing, (
        f".dockerignore no longer denies {missing}. Each of these matches a real file at "
        "the repository root that carries Telegram credentials or bulk noise."
    )


def test_the_example_env_file_survives_the_env_denial():
    """`.env.example` is tracked on purpose and holds placeholders only."""
    patterns = _patterns()

    assert "!.env.example" in patterns, ".dockerignore drops .env.example along with .env."
    assert patterns.index("!.env.example") > patterns.index(".env*"), (
        "The `!.env.example` negation sits before `.env*`, so the later pattern wins and "
        "the file is excluded anyway. Docker resolves these last-match-first."
    )


def test_the_image_installs_from_the_lockfile():
    """Floors resolve to whatever PyPI serves that day; the lock pins what CI tested."""
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "uv.lock" in dockerfile, (
        "Dockerfile no longer copies uv.lock, so the image is built from an unpinned "
        "dependency set while every other path in this repository uses the lock."
    )
    assert "requirements.txt" not in dockerfile, (
        "Dockerfile installs from requirements.txt again. That file carries floors only, "
        "so two images built from one commit a week apart hold different trees."
    )


def test_the_published_port_stays_on_loopback():
    """The compose file acknowledges the bind guard, so this prefix is what is left.

    `MCP_HOST` has to be 0.0.0.0 inside a container for a published port to work at all,
    and the server cannot tell the container's interface from the host's — so
    `MCP_ALLOW_UNAUTHENTICATED_REMOTE` is set there. Every tool in this server acts as
    the operator's Telegram account; without the `127.0.0.1:` prefix, reaching the port
    is the authorization.
    """
    published = _published_ports()

    assert published, "docker-compose.yml publishes no port, so this guard checks nothing."
    exposed = [entry for entry in published if not entry.startswith("127.0.0.1:")]
    assert not exposed, (
        f"docker-compose.yml publishes {exposed} on every interface. The bind guard is "
        "already acknowledged in that file, so this prefix is the only thing keeping "
        "full Telegram account control off the network."
    )


def test_the_persistence_example_does_not_mount_over_the_application():
    """A session mount on /app hides the code it was meant to keep running.

    The commented example is documentation people paste. It pointed at `/app`,
    which is `WORKDIR` — uncommenting it replaced the installed application with
    an empty host directory and the container started with nothing to run. The
    audit that found it stayed open through four follow-up passes because every
    other container check looked at the build context, the lockfile and the
    published port, and none of them read the volume target.

    Commented lines are checked on purpose: an example that only breaks once
    someone follows it is worse than one that breaks in CI.
    """
    compose = COMPOSE.read_text(encoding="utf-8")

    mounts = [
        line.strip().lstrip("#").strip()
        for line in compose.splitlines()
        if ":/app" in line and line.strip().lstrip("#").strip().startswith("-")
    ]

    assert not mounts, (
        f"docker-compose.yml offers {mounts}, which mounts over WORKDIR and hides the "
        "application. Persist sessions at /data instead."
    )


def test_the_image_puts_sessions_outside_the_application_directory():
    """The other half: /data has to be a real, writable, declared location.

    Pointing the example at /data is only correct if the image actually creates
    it, hands it to the unprivileged user that writes the session database, and
    declares it — otherwise the mount lands somewhere root-owned and the first
    write fails.
    """
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "/data" in dockerfile, "the image never creates the session directory"
    assert 'TELEGRAM_SESSION_NAME="/data/' in dockerfile, (
        "TELEGRAM_SESSION_NAME does not point into /data, so a bare filename lands in "
        "WORKDIR and the session is lost with every container replacement."
    )
    assert "appuser:appuser /app /data" in dockerfile, (
        "/data is not owned by the account that runs the server, so the session "
        "database cannot be written."
    )


def test_the_base_image_can_actually_install_the_dependencies():
    """A musl base cannot build this image, and says so only in CI.

    `tdjson` is a required dependency — secret chats and the admin rights newer
    than Telethon's TL layer have no other route — and it publishes manylinux
    wheels with no musllinux wheel and no source distribution. On Alpine
    `uv sync` fails with "doesn't have a source distribution or wheel for the
    current platform", several minutes into a build, for a reason nothing in the
    Dockerfile would have hinted at.

    So the constraint is asserted here, where it costs a second: the base image
    has to be a glibc one for as long as that dependency is required.
    """
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")

    base = [line for line in dockerfile.splitlines() if line.strip().upper().startswith("FROM ")]
    assert base, "the Dockerfile declares no base image"

    for line in base:
        assert "alpine" not in line.lower(), (
            f"{line.strip()!r} is a musl base, which cannot install tdjson "
            "(manylinux wheels only, no sdist). Use a glibc image such as -slim."
        )

    requirements = (REPO / "requirements.txt").read_text(encoding="utf-8")
    assert "tdjson" in requirements, (
        "tdjson is no longer required, so this constraint may be lifted - delete "
        "this test rather than leaving a rule whose reason has gone"
    )
