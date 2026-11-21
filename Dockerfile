# Pinned by digest: the tag python:3.13-slim moves whenever Docker publishes a
# rebuild, but a digest always names the very same image. Dependabot updates it.
FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

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

# The app, its database migrations (app/migrations) and their configuration.
COPY alembic.ini .
COPY app ./app
USER 10001:10001

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
# --no-proxy-headers: the app itself decides whose X-Forwarded-For to believe
# (TRUSTED_PROXIES, see app/main.py), so uvicorn must not rewrite it first.
# --timeout-keep-alive 65: keep idle connections open longer than Nginx does
# (60 s). Otherwise uvicorn could close one just as Nginx sends a request on it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-proxy-headers", "--timeout-keep-alive", "65"]
