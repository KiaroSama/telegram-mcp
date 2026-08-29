# Use an official Python runtime as a parent image (Alpine-based for minimal vulnerabilities)
FROM python:3.13-alpine

# Set the working directory in the container
WORKDIR /app

# Prevent Python from writing pyc files to disc
ENV PYTHONDONTWRITEBYTECODE=1
# Ensure Python output is sent straight to terminal (useful for logs)
ENV PYTHONUNBUFFERED=1

# uv.lock is the single dependency source of truth for this project. The image
# used to install from the pip requirements file, which carries floors and not
# pins, so two images built from the same commit a week apart held different
# dependency trees, and a compromised release of Pillow or Telethon landed in
# the container with no commit and no lockfile diff. --frozen fails loudly when
# uv.lock and pyproject.toml disagree, which is the behaviour you want: a
# manifest edit without a re-lock should break the image build rather than
# quietly resolve something new.
# --no-install-project installs the dependencies only. main.py is run as a
# script from /app, so the project is never imported from site-packages, and
# building it here would need README.md, LICENSE and NOTICE in the context for
# nothing.
# UV_PYTHON_DOWNLOADS=never keeps uv on the base image's own interpreter instead
# of fetching a second managed CPython into an image chosen for being minimal.
ENV UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev --no-install-project

# uv puts the environment in /app/.venv, so putting it first on PATH is what makes
# the bare `python` in CMD below the interpreter holding the locked dependencies.
ENV PATH="/app/.venv/bin:$PATH"

# Copy the rest of the application code
COPY main.py sanitize.py ./
COPY telegram_mcp ./telegram_mcp
# COPY session_string_generator.py . # Optional: if needed within the container, otherwise can be run outside

# Create a non-root user and switch to it
RUN adduser --disabled-password --gecos "" appuser && chown -R appuser:appuser /app
USER appuser

# Define environment variables needed by the application
# These should be provided at runtime, not hardcoded (especially secrets)
ENV TELEGRAM_API_ID=""
ENV TELEGRAM_API_HASH=""
# Specify one of the following at runtime:
# Default session filename
ENV TELEGRAM_SESSION_NAME="telegram_mcp_session"
# Or provide the session string directly
ENV TELEGRAM_SESSION_STRING=""

# Expose any ports if the application were a web server (not needed for stdio MCP)
# EXPOSE 8000

# Define the command to run the application
CMD ["python", "main.py"]
