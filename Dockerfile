# Pinned by digest: the tag python:3.13-slim moves whenever Docker publishes a
# rebuild, but a digest always names the very same image. Dependabot updates it.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# Python doesn't try to write .pyc files at run time (the app user can't write
# the code directory anyway), and log lines appear immediately.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Used only when DATABASE_URL is not given (docker-compose.yml sets it).
    DATABASE_URL=sqlite:////tmp/shortener.db

# An unprivileged user to run the app. If the app is ever compromised, the
# attacker is not root inside the container and cannot modify the code
# (which stays owned by root).
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid 10001 --create-home appuser

WORKDIR /app

# Copy requirements first so Docker can cache the dependency layer:
# code changes then rebuild in seconds instead of reinstalling everything.
# requirements.txt is a lock file: every package, even indirect ones, at an
# exact version with its hash, so pip refuses anything that was tampered with.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY app ./app
USER 10001:10001

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
