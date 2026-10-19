FROM ghcr.io/astral-sh/uv:python3.12-bookworm

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies without the project for better layer caching
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code
COPY . .

# Install the project itself
RUN uv sync --frozen --no-dev

ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Make entrypoint executable
RUN chmod +x entrypoint.sh

EXPOSE 8000

CMD ["./entrypoint.sh"]