# A plain Node base plus only Chromium, instead of the full Playwright image: that one
# also carries Firefox and WebKit, which guardian_token.mjs never launches. Node is
# needed for the script, a python3 for the worker, and cryptography is the login
# code's only third-party import.
FROM node:20-bookworm-slim

WORKDIR /app

# guardian_token.mjs runs NetEase's Guardian SDK in a real browser, so it needs
# the playwright-core client as well as Chromium. It resolves that module from
# <XPENG_PLAYWRIGHT_DIR>/node_modules, and the onboarding tool points
# XPENG_PLAYWRIGHT_DIR at the add-on's own directory, so declare the dependency
# and install it beside this add-on's code - the same thing the slider-solver
# add-on does. Without it the renewal clears the slider and then dies at
# "guardian failed: playwright-core not found".
#
# The client library and the browser build have to match: Chromium is installed
# by the pinned client below, never by a separately versioned one.
COPY package.json ./

# The base image keeps no browsers, so say where they go. guardian_token.mjs looks
# for a full `chromium-*` build under this path.
ENV PORT=8090 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# The headless shell build is deleted: the launch names the full browser
# explicitly (see CHROME_RELATIVES in guardian_token.mjs) and never uses it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-cryptography \
 && npm install --omit=dev --no-audit --no-fund \
 && npx playwright-core install --with-deps chromium \
 && rm -rf /ms-playwright/chromium_headless_shell-* /root/.npm /var/lib/apt/lists/*

COPY run.sh renew_server.py ./
COPY vendor/ ./vendor/
RUN chmod +x /app/run.sh

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python3 -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8090')+'/healthz',timeout=5).status==200 else 1)"

CMD ["/app/run.sh"]
