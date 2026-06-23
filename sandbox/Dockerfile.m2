# M2 sandbox image — the CONTAINED build environment for SdkExecutor.
# The Claude Agent SDK runs INSIDE this container (with setting_sources=[] so it never
# inherits the host's ~/.claude hooks/settings) and writes the built app to /out.
#
# At runtime this container is launched with an egress allowlist (api.anthropic.com +
# the npm registry) and ANTHROPIC_API_KEY injected as env — never baked into the image.
#
# Build (SdkExecutor) + verify (BuildVerifier rebuild + acceptance_runner) both run here.
FROM node:20-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The Claude Code CLI (claude-agent-sdk subprocesses it) + pnpm for app builds.
RUN npm install -g @anthropic-ai/claude-code pnpm

# The Agent SDK in the image's Python.
RUN pip3 install --no-cache-dir --break-system-packages claude-agent-sdk

# Playwright for the selector_present acceptance check (renders the built SPA in chromium).
# route_status checks need no browser. Browsers go to a SHARED, world-readable path so the
# runtime non-root user (uid 1000) can read them — the default per-$HOME cache would be
# written as root at build time and be unreadable at runtime.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip3 install --no-cache-dir --break-system-packages playwright \
    && python3 -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

# node:20-slim already ships a non-root UID-1000 user ("node"). Reuse it; just make the
# agent's cwd writable by it. /out is the bind-mounted artifact dir (mounted at runtime).
RUN mkdir -p /work && chown 1000:1000 /work
WORKDIR /work
