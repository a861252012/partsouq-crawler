# PartSouq browser worker

This process is intentionally separated from the proprietary crawler core. It speaks one
line-delimited JSON request/response protocol over stdin/stdout and is the only component that
imports NoDriver.

Runtime requirements:

- Python 3.12
- Google Chrome or current Chromium
- a persistent, worker-only browser profile directory
- a real display, or Xvfb on a Linux server; true headless mode is not supported

Create a dedicated environment instead of installing the worker into the crawler environment:

```bash
python3.12 -m venv .venv-browser
.venv-browser/bin/pip install --requirement browser_worker/requirements.txt
```

The worker keeps Chrome's sandbox enabled by default. The tested Docker variants either could not
start Chrome with its sandbox or received a persistent Cloudflare challenge with `--no-sandbox`.
They are not a supported production deployment. Run the worker as an unprivileged Linux host user
under Xvfb, keep the sandbox enabled, and require a successful live preflight on that exact host.

NoDriver 0.50.3 declares the GNU AGPL-3.0 license. Deployment and redistribution must be reviewed
for license compliance. The worker does not copy cookies to an HTTP client, use a proxy, rotate
identities, or call a paid CAPTCHA service.
