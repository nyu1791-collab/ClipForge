"""Small live Vertex requests; output only model names and success/failure."""
import base64
import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import os


def request_target(model):
    project = 'project-fdadb4dd-cb77-4270-9cd'
    return (f'https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/publishers/google/models/{model}:generateContent',
            {'Authorization': 'Bearer ' + os.environ['CLIPFORGE_VERTEX_ACCESS_TOKEN']})


def check(model, payload):
    url, headers = request_target(model)
    request = Request(url, data=json.dumps(payload).encode(),
                      headers={**headers, 'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=90) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            message = json.load(error).get('error', {}).get('message', '')
        except (ValueError, OSError):
            message = ''
        print(f'{model}: HTTP {error.code} {message}', flush=True)
        return False
    candidates = result.get('candidates') or []
    success = bool(candidates) and candidates[0].get('finishReason') == 'STOP'
    print(f'{model}: {"success" if success else "incomplete"}', flush=True)
    return success


def main():
    text = {'contents': [{'role': 'user', 'parts': [{'text': 'Reply with OK.'}]}],
            'generationConfig': {'maxOutputTokens': 1024}}
    audio = {'contents': [{'role': 'user', 'parts': [{'inlineData': {'mimeType': 'audio/mpeg',
              'data': base64.b64encode(Path(sys.argv[1]).read_bytes()).decode()}}]}],
             'generationConfig': {'audioTranscriptionConfig': {'languageCodes': ['en-US'], 'wordTimestamp': True}}}
    results = [check('gemini-3.5-flash', text), check('gemini-3.8-flash', text),
               check('gemini-3.5-transcribe', audio)]
    if not all(results):
        raise SystemExit('One or more models could not be confirmed.')


if __name__ == '__main__':
    main()
