#!/usr/bin/env bash
# Run once in the intended owner's Cloud Shell; credentials never enter the chat.
set -euo pipefail
drive_owner='n.yu1791@gmail.com'
drive_project='project-fdadb4dd-cb77-4270-9cd'
drive_active=$(gcloud auth list --filter=status:ACTIVE --format='value(account)')
if [[ ${drive_active,,} != "$drive_owner" ]]; then
  echo "Open Cloud Shell as $drive_owner." >&2
  exit 1
fi
command -v gh >/dev/null || { echo 'GitHub CLI (gh) is required in this Cloud Shell.' >&2; exit 1; }
gcloud services enable drive.googleapis.com --project="$drive_project" --quiet
gcloud auth application-default login --no-launch-browser --scopes='openid,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/drive.file'
gcloud auth application-default set-quota-project "$drive_project" --quiet
if ! gh auth status --hostname github.com >/dev/null 2>&1; then
  gh auth login --hostname github.com --git-protocol https --web --scopes repo
fi
python3 - <<'PYCODE'
import json,os,pathlib,subprocess,urllib.request
p=subprocess.run(['gcloud','auth','application-default','print-access-token'],capture_output=True,text=True,check=True)
req=urllib.request.Request('https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)',headers={'Authorization':'Bearer '+p.stdout.strip(),'x-goog-user-project':'project-fdadb4dd-cb77-4270-9cd'})
with urllib.request.urlopen(req,timeout=60) as r: account=json.load(r).get('user',{}).get('emailAddress','').lower()
if account!='n.yu1791@gmail.com':raise SystemExit('Drive account differs; no secret was registered.')
if subprocess.check_output(['gh','api','user','--jq','.login'],text=True).strip()!='nyu1791-collab':raise SystemExit('Use the repository owner GitHub login.')
config=pathlib.Path(os.environ.get('CLOUDSDK_CONFIG',str(pathlib.Path.home()/'.config/gcloud')))
credential=(config/'application_default_credentials.json').read_text()
for repo in ['nyu1791-collab/ai-model-workers','nyu1791-collab/ClipForge']:
 r=subprocess.run(['gh','secret','set','GOOGLE_DRIVE_ADC_JSON','--repo',repo],input=credential,text=True,capture_output=True)
 if r.returncode:raise SystemExit('Could not register the Drive secret in '+repo+'; no credential values were printed.')
 print('Drive credential registered in '+repo)
print('Target Drive ready: n.yu1791@gmail.com. Colab credentials unchanged.')
PYCODE
