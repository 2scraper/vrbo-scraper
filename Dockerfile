# Builds the Playwright engine (the one the README recommends) into a
# container with REAL CHROME — see below for why that word is load-bearing.
#
#   docker build -t vrbo-scraper .
#   docker run --rm -v "$PWD/out:/out" vrbo-scraper \
#     --url "https://www.vrbo.com/search?destination=Orlando,%20Florida,%20United%20States%20of%20America" \
#     --pages 3 --out /out/orlando
#
# Pass --proxy/--twocaptcha-key the same way as running locally, or mount a
# .env at /app/.env — nothing here bakes in a credential, and .dockerignore
# keeps one out of the build context.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-playwright.txt ./

# `playwright install chrome`, NOT `playwright install chromium`, and that is
# the difference between an image that works and one that is refused.
# Measured on this site from the same address seconds apart: Playwright's
# bundled Chromium was answered HTTP 429 and Google Chrome HTTP 200 with the
# full grid. An image built with the bundled browser would be a scraper that
# cannot fetch its own target — and it would look like a blocked IP rather
# than a build choice.
#
# `--with-deps` also pulls Chromium's shared-library dependencies through
# apt, which are not pip packages and so cannot ride in requirements.txt.
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt \
    && playwright install --with-deps chrome

# Every module playwright_scraper.py imports, transitively, plus diff_runs.py
# as a useful companion in the same image. smoke_test.py checks this list
# against the entrypoint's real import graph: three repos in this family
# shipped an image missing proxy_pool.py, which the engine imports at module
# level, so it died with ModuleNotFoundError on every invocation INCLUDING
# `--help` — a broken container that nothing in the repo would have noticed.
COPY captcha_solver.py env_config.py fingerprint_client.py output_writer.py \
     page_flow.py playwright_scraper.py product_parser.py proxy_pool.py \
     diff_runs.py ./

# Headful by default everywhere else in this repo; in a container there is no
# display, so the engine runs headless here. That is a real limitation rather
# than a preference — the measured discriminator on this site is the browser
# BUILD rather than the window, which is why installing chrome above is what
# matters, but a headless run is one more thing a bot manager can key on.
# Pass --proxy or --cdp-endpoint if this image starts getting refused.
ENV VRBO_DOCKER=1

ENTRYPOINT ["python3", "playwright_scraper.py", "--headless"]
CMD ["--help"]
