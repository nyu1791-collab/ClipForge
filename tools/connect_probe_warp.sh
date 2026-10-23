#!/usr/bin/env bash
# Disposable GitHub-hosted diagnostic runner only; never change a developer host.
set -euo pipefail
if [ "${GITHUB_ACTIONS:-}" != true ] || [ "${RUNNER_ENVIRONMENT:-}" != github-hosted ]; then
  echo 'WARP probe requires a disposable GitHub-hosted runner' >&2
  exit 1
fi
mkdir -p probe-result
curl --fail --silent --show-error --max-time 20 https://pkg.cloudflareclient.com/pubkey.gpg |
  sudo gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ noble main' |
  sudo tee /etc/apt/sources.list.d/cloudflare-client.list >/dev/null
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends cloudflare-warp
sudo systemctl start warp-svc
timeout 20 warp-cli --accept-tos registration new
timeout 20 warp-cli --accept-tos mode warp+doh
# Roll back even if the new network interrupts the runner control connection.
sudo systemd-run --unit=clipforge-warp-rollback --on-active=240s /usr/bin/warp-cli --accept-tos disconnect
timeout 20 warp-cli --accept-tos connect
python3 - <<'PY'
from datetime import datetime, timezone
import json, pathlib, subprocess, time
started = time.monotonic()
report = {'started_at': datetime.now(timezone.utc).isoformat(), 'warp_verified': False}
for attempt in range(12):
    result = subprocess.run(['curl', '--fail', '--silent', '--show-error', '--max-time', '5', 'https://www.cloudflare.com/cdn-cgi/trace'], capture_output=True, text=True, timeout=7)
    if result.returncode == 0 and 'warp=on' in result.stdout.splitlines():
        report['warp_verified'] = True
        break
    time.sleep(2)
report.update(ended_at=datetime.now(timezone.utc).isoformat(), elapsed_seconds=round(time.monotonic()-started, 3))
report['client_version'] = subprocess.run(['warp-cli', '--version'], capture_output=True, text=True, timeout=5).stdout.strip()
pathlib.Path('probe-result/warp-status.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report))
raise SystemExit(0 if report['warp_verified'] else 1)
PY
