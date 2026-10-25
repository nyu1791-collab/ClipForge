"""Use existing private Actions Google credentials without exposing them to the VM."""
import hashlib,json,os,re,subprocess,sys,time,secrets,base64,zipfile,urllib.request,urllib.parse,urllib.error
from pathlib import Path
ACCOUNT='n.yu1791@gmail.com'
REPO='nyu1791-collab/ai-model-workers'

def request(url,method='GET',payload=None,headers=None,raw=None):
 h=dict(headers or {})
 if payload is not None:raw=json.dumps(payload).encode();h['Content-Type']='application/json'
 req=urllib.request.Request(url,data=raw,headers=h,method=method)
 return urllib.request.urlopen(req,timeout=180)

def secret_set(name,value):
 p=subprocess.run(['gh','secret','set',name,'--repo',REPO],input=value,text=True,capture_output=True,timeout=60)
 if p.returncode:raise RuntimeError('Private GitHub secret update failed.')

def oauth_mode(mode,adc):
 redirect='https://sdk.cloud.google.com/applicationdefaultauthcode.html'
 scopes='openid https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/drive.file'
 if mode=='begin':
  verifier=secrets.token_urlsafe(64);state=secrets.token_urlsafe(24)
  pending={'client_id':adc['client_id'],'client_secret':adc['client_secret'],'code_verifier':verifier,'redirect_uri':redirect,'expires_at':time.time()+3600}
  secret_set('CLIPFORGE_DRIVE_OAUTH_PENDING',json.dumps(pending))
  query={'client_id':pending['client_id'],'redirect_uri':redirect,'scope':scopes,'response_type':'code','access_type':'offline','prompt':'consent','token_usage':'remote','login_hint':ACCOUNT,'state':state,'code_challenge':base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='),'code_challenge_method':'S256'}
  print('DRIVE_AUTH_URL=https://accounts.google.com/o/oauth2/auth?'+urllib.parse.urlencode(query),flush=True)
  return
 pending=json.loads(os.environ.pop('DRIVE_OAUTH_PENDING','{}'));code=os.environ.pop('DRIVE_AUTH_CODE','').strip()
 if not code or pending.get('expires_at',0)<time.time():raise RuntimeError('Pending Google consent or one-time code is missing or expired.')
 body=urllib.parse.urlencode({'grant_type':'authorization_code','client_id':pending['client_id'],'client_secret':pending['client_secret'],'code_verifier':pending['code_verifier'],'redirect_uri':pending['redirect_uri'],'code':code}).encode()
 with request('https://oauth2.googleapis.com/token','POST',raw=body,headers={'Content-Type':'application/x-www-form-urlencoded'}) as r:token=json.load(r)
 with request('https://www.googleapis.com/oauth2/v2/userinfo',headers={'Authorization':'Bearer '+token['access_token']}) as r:user=json.load(r)
 if user.get('email','').lower()!=ACCOUNT or not user.get('verified_email'):raise RuntimeError('Google account mismatch; refusing registration.')
 if not token.get('refresh_token'):raise RuntimeError('Google did not issue a refresh token.')
 credential={'type':'authorized_user','client_id':pending['client_id'],'client_secret':pending['client_secret'],'refresh_token':token['refresh_token'],'token_uri':'https://oauth2.googleapis.com/token'}
 secret_set('GOOGLE_DRIVE_ADC_JSON',json.dumps(credential))
 for name in ('CLIPFORGE_DRIVE_AUTH_CODE','CLIPFORGE_DRIVE_OAUTH_PENDING'):
  subprocess.run(['gh','secret','delete',name,'--repo',REPO],capture_output=True,timeout=60,check=True)
 print('DRIVE_AUTH_STATUS=registered; account verified; one-time secrets removed',flush=True)

def diagnose_projects():
 adc=json.loads(os.environ['COLAB_ADC_JSON']);drive=json.loads(os.environ.get('GOOGLE_DRIVE_ADC_JSON','{}'))
 print('ADC_QUOTA_PROJECT='+str(adc.get('quota_project_id','unset')),flush=True)
 print('DRIVE_QUOTA_PROJECT='+str(drive.get('quota_project_id','unset')),flush=True)
 raw=urllib.parse.urlencode({k:adc[k] for k in ('refresh_token','client_id','client_secret')}|{'grant_type':'refresh_token'}).encode()
 with request('https://oauth2.googleapis.com/token','POST',raw=raw,headers={'Content-Type':'application/x-www-form-urlencoded'}) as r:token=json.load(r)['access_token']
 with request('https://cloudresourcemanager.googleapis.com/v1/projects?pageSize=100',headers={'Authorization':'Bearer '+token}) as r:data=json.load(r)
 print('GOOGLE_PROJECTS='+json.dumps([{'projectId':p['projectId'],'projectNumber':p.get('projectNumber'),'state':p.get('lifecycleState')} for p in data.get('projects',[])],ensure_ascii=False),flush=True)

def configure_quota():
 def refresh(adc):
  body=urllib.parse.urlencode({k:adc[k] for k in ('refresh_token','client_id','client_secret')}|{'grant_type':'refresh_token'}).encode()
  with request('https://oauth2.googleapis.com/token','POST',raw=body,headers={'Content-Type':'application/x-www-form-urlencoded'}) as r:return json.load(r)['access_token']
 cloud=json.loads(os.environ['COLAB_ADC_JSON']);drive=json.loads(os.environ['GOOGLE_DRIVE_ADC_JSON'])
 ca={'Authorization':'Bearer '+refresh(cloud)};da={'Authorization':'Bearer '+refresh(drive)}
 for headers in (ca,da):
  with request('https://www.googleapis.com/oauth2/v2/userinfo',headers=headers) as r:identity=json.load(r)
  if identity.get('email','').lower()!=ACCOUNT:raise RuntimeError('Quota setup account mismatch.')
 with request('https://cloudresourcemanager.googleapis.com/v1/projects?pageSize=100',headers=ca) as r:projects=json.load(r).get('projects',[])
 chosen=None
 for project in projects:
  if project.get('lifecycleState')!='ACTIVE':continue
  pid=project['projectId'];number=project['projectNumber']
  try:
   with request(f'https://serviceusage.googleapis.com/v1/projects/{number}/services/drive.googleapis.com',headers=ca) as r:service=json.load(r)
   print('DRIVE_API_PROJECT='+pid+'; state='+str(service.get('state')),flush=True)
   if service.get('state')!='ENABLED':continue
   with request('https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)',headers=da|{'x-goog-user-project':pid}) as r:owner=json.load(r)
   if owner.get('user',{}).get('emailAddress','').lower()==ACCOUNT:chosen=pid;break
  except urllib.error.HTTPError as e:
   print('QUOTA_CANDIDATE_UNAVAILABLE='+pid+'; http='+str(e.code),flush=True)
 if chosen is None:
  chosen='clipforge-drive-'+secrets.token_hex(4)
  print('CREATING_DRIVE_ONLY_PROJECT='+chosen+'; billing account is not attached',flush=True)
  with request('https://cloudresourcemanager.googleapis.com/v1/projects','POST',payload={'projectId':chosen,'name':'ClipForge Drive delivery'},headers=ca) as r:operation=json.load(r)
  for _ in range(90):
   with request('https://cloudresourcemanager.googleapis.com/v1/'+operation['name'],headers=ca) as r:status=json.load(r)
   if status.get('error'):raise RuntimeError('Drive project creation operation failed.')
   if status.get('done'):break
   time.sleep(2)
  else:raise RuntimeError('Drive project creation is still pending.')
  with request('https://cloudresourcemanager.googleapis.com/v1/projects/'+chosen,headers=ca) as r:project=json.load(r)
  with request('https://serviceusage.googleapis.com/v1/projects/'+project['projectNumber']+'/services/drive.googleapis.com:enable','POST',payload={},headers=ca) as r:operation=json.load(r)
  for _ in range(90):
   with request('https://serviceusage.googleapis.com/v1/'+operation['name'],headers=ca) as r:status=json.load(r)
   if status.get('error'):raise RuntimeError('Drive API enable operation failed.')
   if status.get('done'):break
   time.sleep(2)
  else:raise RuntimeError('Drive API enable operation is still pending.')
  print('DRIVE_API_ENABLED_PROJECT='+chosen,flush=True)
  for attempt in range(12):
   try:
    with request('https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)',headers=da|{'x-goog-user-project':chosen}) as r:owner=json.load(r)
    if owner.get('user',{}).get('emailAddress','').lower()!=ACCOUNT:raise RuntimeError('Drive quota project owner check failed.')
    break
   except urllib.error.HTTPError:
    if attempt==11:raise
    time.sleep(5)
 drive['quota_project_id']=chosen
 secret_set('GOOGLE_DRIVE_ADC_JSON',json.dumps(drive))
 print('DRIVE_QUOTA_STATUS=ready; project='+chosen,flush=True)

def execute():
 mode=os.environ.get('DELIVERY_MODE','probe')
 if mode=='diagnose':diagnose_projects();return
 if mode=='configure':configure_quota();return
 adc=json.loads(os.environ.pop('GOOGLE_DRIVE_ADC_JSON','') or os.environ.pop('COLAB_ADC_JSON','{}'))
 if mode in ('begin','authorize'):
  oauth_mode(mode,adc);return
 if not all(adc.get(k) for k in ('refresh_token','client_id','client_secret')):raise RuntimeError('Existing Google user credentials are unavailable.')
 body=urllib.parse.urlencode({k:adc[k] for k in ('refresh_token','client_id','client_secret')}|{'grant_type':'refresh_token'}).encode()
 with request('https://oauth2.googleapis.com/token','POST',raw=body,headers={'Content-Type':'application/x-www-form-urlencoded'}) as r:token=json.load(r)['access_token']
 auth={'Authorization':'Bearer '+token}
 with request('https://www.googleapis.com/oauth2/v2/userinfo',headers=auth) as r:identity=json.load(r)
 if identity.get('email','').lower()!=ACCOUNT:raise RuntimeError('Google account mismatch; refusing Drive access.')
 if adc.get('quota_project_id'):auth['x-goog-user-project']=adc['quota_project_id']
 with request('https://www.googleapis.com/drive/v3/about?fields=user(emailAddress),storageQuota(limit,usage)',headers=auth) as r:about=json.load(r)
 if about.get('user',{}).get('emailAddress','').lower()!=ACCOUNT:raise RuntimeError('Drive owner mismatch.')
 print('DRIVE_PROBE_STATUS=ready; expected account verified; quota='+json.dumps(about.get('storageQuota',{})),flush=True)
 if os.environ.get('DELIVERY_MODE','probe')=='probe':return
 tag=os.environ.get('DELIVERY_TAG','')
 if not re.fullmatch(r'refs/heads/clipforge-drive-data/[a-f0-9]{32}',tag):raise RuntimeError('Invalid private staging branch.')
 directory=Path(os.environ['RUNNER_TEMP'])/'clipforge-drive-delivery';directory.mkdir(mode=0o700,exist_ok=False)
 staged=Path(os.environ['GITHUB_WORKSPACE'])/'drive-transfer-data'
 transfer=json.loads((staged/'transfer-manifest.json').read_text());expected={}
 for name,meta in transfer.items():
  if name not in ('highlights.mp4','ClipForge-clips.zip'):raise RuntimeError('Unexpected input filename.')
  with (directory/name).open('wb') as out:
   for part in meta['chunks']:
    if not re.fullmatch(r'part-[0-9]{3}\.bin',part['name']):raise RuntimeError('Invalid chunk name.')
    src=staged/part['name']
    if src.is_symlink():raise RuntimeError('Symlink not allowed in transfer.')
    data=src.read_bytes()
    if hashlib.sha256(data).hexdigest()!=part['sha256']:raise RuntimeError('Chunk SHA256 mismatch.')
    out.write(data)
  path=directory/name
  if path.stat().st_size!=meta['size'] or hashlib.sha256(path.read_bytes()).hexdigest()!=meta['sha256']:raise RuntimeError('Private transfer integrity check failed.')
  expected[name]=meta['sha256']
 if os.environ.get('INDIVIDUAL_CLIPS','false').lower()=='true':
  with zipfile.ZipFile(directory/'ClipForge-clips.zip') as archive:
   names=[n for n in archive.namelist() if re.fullmatch(r'clip-[0-9]{2}\.mp4',n)]
   if not 1<=len(names)<=10:raise RuntimeError('Expected top-level individual clip files in ZIP.')
   if sum(archive.getinfo(n).file_size for n in names)>2*1024**3:raise RuntimeError('Expanded clips exceed transfer budget.')
   checksums={line.split('  ',1)[1]:line.split('  ',1)[0] for line in archive.read('checksums.sha256').decode().splitlines() if '  ' in line}
   for name in sorted(names):
    h=hashlib.sha256()
    with archive.open(name) as src,(directory/name).open('wb') as out:
     while True:
      data=src.read(8*1024**2)
      if not data:break
      h.update(data);out.write(data)
    if checksums.get(name)!=h.hexdigest():raise RuntimeError('Individual clip SHA256 mismatch.')
    expected[name]=h.hexdigest()
 folder_name=os.environ.get('DELIVERY_FOLDER_NAME','').strip() or 'ClipForge 切り抜き '+time.strftime('%Y-%m-%d',time.gmtime(time.time()+9*3600))
 if len(folder_name)>120 or any(ord(c)<32 for c in folder_name):raise RuntimeError('Invalid Drive folder name.')
 with request('https://www.googleapis.com/drive/v3/files?fields=id,webViewLink','POST',payload={'name':folder_name,'mimeType':'application/vnd.google-apps.folder'},headers=auth) as r:folder=json.load(r)
 print('DRIVE_FOLDER='+folder['webViewLink'],flush=True)
 for name in expected:
  p=directory/name;size=p.stat().st_size;mime='video/mp4' if p.suffix=='.mp4' else 'application/zip'
  with request('https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=id,name,size,md5Checksum,webViewLink','POST',payload={'name':name,'parents':[folder['id']]},headers=auth|{'X-Upload-Content-Type':mime,'X-Upload-Content-Length':str(size)}) as r:location=r.headers['Location']
  with p.open('rb') as f:
   start=0
   while start<size:
    data=f.read(8*1024**2);end=start+len(data)-1
    try:
     with request(location,'PUT',raw=data,headers=auth|{'Content-Type':mime,'Content-Range':f'bytes {start}-{end}/{size}'}) as r:result=json.load(r)
    except urllib.error.HTTPError as e:
     if e.code!=308:raise
    start=end+1
  if int(result['size'])!=size or result['md5Checksum']!=hashlib.md5(p.read_bytes()).hexdigest():raise RuntimeError('Drive integrity check failed.')
  print('DRIVE_FILE='+json.dumps({'name':name,'link':result['webViewLink'],'size':size,'checksum_verified':True}),flush=True)
 print('DRIVE_DELIVERY_STATUS=complete; sharing unchanged/private',flush=True)

if __name__=='__main__':
 try:execute()
 except urllib.error.HTTPError as e:
  try:
   d=json.load(e).get('error',{}); reasons=[x.get('reason') for x in d.get('errors',[])]; message=re.sub(r'https?://\S+','[URL]',d.get('message',''))[:500]
  except Exception:reasons=[];message='Google API request failed.'
  print('DRIVE_ERROR='+json.dumps({'http_status':e.code,'reasons':reasons,'message':message}),flush=True);sys.exit(1)
 except Exception as e:
  print('DRIVE_ERROR_TYPE='+type(e).__name__,flush=True);sys.exit(1)
