# The Playwright image is used because it already carries Node and Chromium -
# guardian_token.mjs needs both - and a python3. cryptography is the login
# code's only third-party import, so the image stays close to the solver
# add-on's.
FROM mcr.microsoft.com/playwright:v1.49.1-jammy

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pip \
 && pip3 install --no-cache-dir cryptography \
 && rm -rf /var/lib/apt/lists/*

# guardian_token.mjs runs NetEase's Guardian SDK in a real browser, so it needs
# the playwright-core client as well as Chromium. It resolves that module from
# <XPENG_PLAYWRIGHT_DIR>/node_modules, and the onboarding tool points
# XPENG_PLAYWRIGHT_DIR at the add-on's own directory, so declare the dependency
# and install it beside this add-on's code - the same thing the slider-solver
# add-on does. Without it the renewal clears the slider and then dies at
# "guardian failed: playwright-core not found".
#
# Keep this version equal to the base image tag above: the client library and
# the browser build have to match.
COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

COPY run.sh renew_server.py ./
COPY vendor/ ./vendor/
RUN chmod +x /app/run.sh

# The base image keeps its browser builds here. Being explicit means the add-on
# does not depend on that ENV surviving a base image change.
ENV PORT=8090 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python3 -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8090')+'/healthz',timeout=5).status==200 else 1)"

CMD ["/app/run.sh"]
