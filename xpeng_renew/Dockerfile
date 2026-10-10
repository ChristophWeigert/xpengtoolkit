# The Playwright image is used because it already carries Node - guardian_token.mjs
# needs it - and a python3. cryptography is the login code's only third-party
# import, so the image stays close to the solver add-on's.
FROM mcr.microsoft.com/playwright:v1.49.1-jammy

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-pip \
 && pip3 install --no-cache-dir cryptography \
 && rm -rf /var/lib/apt/lists/*

COPY run.sh renew_server.py ./
COPY vendor/ ./vendor/
RUN chmod +x /app/run.sh

ENV PORT=8090
EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python3 -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8090')+'/healthz',timeout=5).status==200 else 1)"

CMD ["/app/run.sh"]
