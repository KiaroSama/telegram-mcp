"""Regression tests for distribution packaging.

Importing ``telegram_mcp.visual`` from a repo checkout succeeds even when the
package is missing from the wheel, so a source-tree import proves nothing. These
tests check the declared packaging config and the built/installed artifact, which
is where a dropped subpackage actually shows up — as an ImportError at startup.
"""

from fnmatch import fnmatch
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile

import pytest

REPO = Path(__file__).resolve().parents[1]
PACKAGE = "telegram_mcp"

BUILD_TIMEOUT_SECONDS = 600
INSTALL_TIMEOUT_SECONDS = 300
IMPORT_TIMEOUT_SECONDS = 120

# Only modules whose imports are pure stdlib: telegram_mcp.tools.* needs the
# Telegram credentials and trips the install-provenance guard.
_IMPORT_CHECK = """
import pathlib
import sys

import telegram_mcp
import telegram_mcp.message_view
import telegram_mcp.visual.capture
import telegram_mcp.visual.frames
import telegram_mcp.visual.images

target = pathlib.Path(sys.argv[1]).resolve()
for module in (
    telegram_mcp,
    telegram_mcp.message_view,
    telegram_mcp.visual.capture,
    telegram_mcp.visual.frames,
    telegram_mcp.visual.images,
):
    origin = pathlib.Path(module.__file__).resolve()
    assert target in origin.parents, f"{module.__name__} came from {origin}, not the wheel"
"""


def _pyproject() -> dict:
    with (REPO / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _source_packages() -> set[str]:
    """Every importable subpackage in the source tree, as dotted names."""
    return {
        ".".join(init.parent.relative_to(REPO).parts)
        for init in (REPO / PACKAGE).rglob("__init__.py")
    }


def _source_modules() -> set[str]:
    """Every source module, spelled the way a wheel records its members."""
    return {
        module.relative_to(REPO).as_posix()
        for module in (REPO / PACKAGE).rglob("*.py")
        if "__pycache__" not in module.parts
    }


def _undeclared_packages(setuptools_config: dict, expected: set[str]) -> set[str]:
    """Subpackages the given ``[tool.setuptools]`` table would leave out."""
    packages = setuptools_config.get("packages")
    if isinstance(packages, list):
        return expected - set(packages)

    find = (packages or {}).get("find", {})
    include = find.get("include") or ["*"]
    exclude = find.get("exclude") or []
    return {
        name
        for name in expected
        if not any(fnmatch(name, pattern) for pattern in include)
        or any(fnmatch(name, pattern) for pattern in exclude)
    }


def _stage_sources(stage: Path) -> Path:
    """Copy the buildable sources, so setuptools writes build/ and .egg-info here."""
    stage.mkdir(parents=True)
    shutil.copytree(REPO / PACKAGE, stage / PACKAGE, ignore=shutil.ignore_patterns("__pycache__"))
    modules = _pyproject()["tool"]["setuptools"].get("py-modules", [])
    for name in ["pyproject.toml", "README.md", "LICENSE", *(f"{m}.py" for m in modules)]:
        if (REPO / name).exists():
            shutil.copy2(REPO / name, stage / name)
    return stage


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory) -> Path:
    if importlib.util.find_spec("build") is None:
        pytest.skip("the `build` module is not installed")

    workspace = tmp_path_factory.mktemp("packaging")
    outdir = workspace / "dist"
    # Prefer --no-isolation when the build backend is already importable: the
    # isolated default downloads setuptools+wheel, which turns this test red on an
    # offline or cold-cache runner instead of exercising the packaging config.
    if importlib.util.find_spec("setuptools") is not None:
        build_args = ["--no-isolation"]
    else:
        build_args = ["--installer", "uv"] if shutil.which("uv") else []
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", *build_args, "--outdir", str(outdir)]
        + [str(_stage_sources(workspace / "src"))],
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        output = result.stdout + result.stderr
        if any(
            marker in output.lower()
            for marker in ("network", "offline", "failed to fetch", "no such host", "resolve")
        ):
            pytest.skip(f"the build backend could not be provisioned: {output[-200:]}")
        raise AssertionError(output)

    wheels = sorted(outdir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    return wheels[0]


def test_every_source_subpackage_is_declared_for_packaging():
    expected = _source_packages()
    assert f"{PACKAGE}.visual" in expected, "source walk found no subpackages to check"

    assert _undeclared_packages(_pyproject()["tool"]["setuptools"], expected) == set()

    # Prove the check bites: a hand-maintained list that forgets a subpackage is
    # exactly how telegram_mcp.visual was dropped from the wheel.
    stale = {"packages": sorted(expected - {f"{PACKAGE}.visual"})}
    assert _undeclared_packages(stale, expected) == {f"{PACKAGE}.visual"}


def test_built_wheel_contains_every_subpackage(built_wheel):
    with zipfile.ZipFile(built_wheel) as wheel:
        shipped = set(wheel.namelist())

    missing = sorted(_source_modules() - shipped)
    assert not missing, f"wheel {built_wheel.name} is missing: {missing}"


def test_installed_package_imports_visual_modules(built_wheel, tmp_path):
    if shutil.which("uv") is None:
        pytest.skip("uv is required to install the wheel (pip is unavailable in this venv)")

    target = tmp_path / "site"
    install = subprocess.run(
        # --offline/--no-deps keep this local to the wheel file: no network, no resolution.
        ["uv", "pip", "install", "--target", str(target), "--no-deps", "--offline"]
        + [str(built_wheel)],
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT_SECONDS,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    # cwd outside the repo plus the origin assertions in _IMPORT_CHECK mean only
    # the installed copy can satisfy these imports.
    check = subprocess.run(
        [sys.executable, "-c", _IMPORT_CHECK, str(target)],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(target)},
        capture_output=True,
        text=True,
        timeout=IMPORT_TIMEOUT_SECONDS,
    )
    assert check.returncode == 0, check.stdout + check.stderr


def test_the_build_toolchain_is_a_declared_dev_dependency():
    """This suite silently skipped in CI because `build` was never installed.

    A skipped wheel test is indistinguishable from a passing one in the CI summary,
    which is how a package list that omitted telegram_mcp.visual shipped unnoticed.
    """
    dev = _pyproject().get("dependency-groups", {}).get("dev", [])
    names = {requirement.split(">")[0].split("=")[0].split("[")[0].strip() for requirement in dev}

    for required in ("build", "setuptools", "wheel"):
        assert (
            required in names
        ), f"{required} is missing from the dev group; the wheel test will skip"


def test_the_wheel_build_is_not_skipped_in_this_environment():
    """Guard the guard: if `build` is importable the fixture must actually run."""
    assert importlib.util.find_spec("build") is not None, (
        "build is not installed, so test_built_wheel_contains_every_subpackage skips. "
        "Install the dev group: uv sync --group dev"
    )


def test_requirements_txt_matches_the_declared_dependencies():
    """Dockerfile installs from requirements.txt, so a drift there ships a container
    missing a runtime dependency while every uv-based path stays fine.

    It is the one dependency list in this repository maintained by hand: pyproject.toml
    is the source of truth, uv.lock is generated from it, and this file is neither.
    """
    expected = set(_pyproject()["project"]["dependencies"])
    listed = {
        line.strip()
        for line in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert listed == expected, (
        "requirements.txt and pyproject.toml disagree; Dockerfile installs from the "
        f"former. Only in requirements.txt: {sorted(listed - expected)}. "
        f"Only in pyproject.toml: {sorted(expected - listed)}."
    )


def test_license_metadata_uses_the_form_setuptools_will_still_accept():
    """setuptools deprecated both the `license` table and the `License ::` classifiers,
    with builds failing after 2027-02-18. The replacement is a PEP 639 SPDX expression -
    and "Proprietary" is not an SPDX identifier, so it takes the `LicenseRef-` prefix
    reserved for licences that are not on the SPDX list.

    Declaring both an expression and a `License ::` classifier is an error, not a
    warning: they are two answers to one question.
    """
    project = _pyproject()["project"]

    assert isinstance(
        project["license"], str
    ), "`license` is a TOML table again; setuptools removes support 2027-02-18."
    declared = project["license"]
    assert declared and not declared.isspace(), "the licence expression is empty"
    # A real SPDX identifier, or the `LicenseRef-` prefix reserved for one that is
    # not on the SPDX list. Both are valid PEP 639; this used to require the second
    # because the project was proprietary, which then failed the moment it was not.
    assert declared.startswith("LicenseRef-") or declared[0].isalnum(), declared
    assert project["license-files"], "the LICENCE text is no longer shipped in the dist"

    license_classifiers = [c for c in project["classifiers"] if c.startswith("License ::")]
    assert (
        license_classifiers == []
    ), f"a License:: classifier alongside an SPDX expression is rejected: {license_classifiers}"


def test_the_no_public_pypi_guard_survives_the_license_migration():
    """`Private :: Do Not Upload` is not a registered trove prefix, so PyPI rejects any
    upload carrying it. It reads like a license classifier and sits in the same list,
    which is exactly how it would get deleted alongside one.
    """
    classifiers = _pyproject()["project"]["classifiers"]

    assert "Private :: Do Not Upload" in classifiers, (
        "the classifier that makes a PyPI upload fail has been removed; the "
        "telegram-mcp name there belongs to a different project."
    )


def test_the_build_backend_is_new_enough_to_understand_the_license_expression():
    """setuptools below 77 does not read the SPDX string form. It would not fail - it
    would build a wheel with no licence metadata at all, which is worse.
    """
    requires = _pyproject()["build-system"]["requires"]
    setuptools_pin = next(r for r in requires if r.startswith("setuptools"))

    floor = int(setuptools_pin.split(">=")[1].split(",")[0].strip())
    assert floor >= 77, f"{setuptools_pin} predates SPDX `license` support"


def _declared_for_coverage() -> set:
    return set(_pyproject()["tool"]["coverage"]["run"]["source"])


def _measurable_modules() -> set:
    """Every module the coverage floor is supposed to be a claim about.

    telegram_mcp.tools.* is excluded by policy, stated in pyproject: those 29
    adapters marshal arguments to Telethon and format what comes back, so
    measuring them rewards mocking the API rather than testing anything.
    """
    return {
        ".".join(module.relative_to(REPO).with_suffix("").parts)
        for module in (REPO / PACKAGE).rglob("*.py")
        if "__pycache__" not in module.parts and module.name != "__init__.py"
    } - {
        name
        for name in (
            ".".join(module.relative_to(REPO).with_suffix("").parts)
            for module in (REPO / PACKAGE / "tools").rglob("*.py")
        )
    }


def test_every_measurable_module_is_declared_for_coverage():
    """The third hand-maintained list in this repository, and the third guard.

    The wheel dropped telegram_mcp.visual from a hand-written package list, and CI
    ran a hand-written list of PowerShell suites while one of them outlived the
    script it tested. Both were replaced with discovery plus a check. This list
    had quietly accumulated seven modules outside it - handles.py among them, at
    822 lines and named in fourteen test files - so the coverage floor was a claim
    about a project that excluded this fork's own security core.

    A module left out is not caught by anything else: coverage reports happily on
    what it was told to watch, and the number goes UP when the untested parts are
    the ones nobody declared.
    """
    declared = _declared_for_coverage()
    measurable = _measurable_modules()
    assert measurable, "the module walk found nothing to check"

    unmeasured = {
        name
        for name in measurable
        if name not in declared and not any(name.startswith(parent + ".") for parent in declared)
    }
    assert not unmeasured, (
        "these modules are invisible to the coverage floor. Add them to "
        "[tool.coverage.run] source in pyproject.toml, or record why they are "
        f"exempt the way telegram_mcp.tools.* is: {sorted(unmeasured)}"
    )


def test_no_coverage_source_names_something_that_is_gone():
    """The other half. coverage warns about a source that matches nothing and
    carries on, so a renamed module leaves an entry that quietly measures air.
    """
    stale = {
        name
        for name in _declared_for_coverage()
        if not (REPO / (name.replace(".", "/") + ".py")).exists()
        and not (REPO / name.replace(".", "/")).is_dir()
    }
    assert not stale, f"coverage is told to measure modules that do not exist: {sorted(stale)}"
