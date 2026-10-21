#!/usr/bin/env python3
"""Private-worker synthetic integration: real FFmpeg, ASS, mixing and both formats."""
import json
from pathlib import Path

import pysubs2

from clipforge.batch_models import BatchClip, RichStyle
from clipforge.rich_render import render
from clipforge.storage import process, write_json


def main():
    root = Path('workspace/output/verification').resolve()
    root.mkdir(parents=True,exist_ok=True)
    source = root/'source.mp4'
    if not source.exists():
        process(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=25',
                 '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','60',
                 '-c:v','libx264','-preset','ultrafast','-threads','2','-pix_fmt','yuv420p',
                 '-c:a','aac',source],timeout=180)
    rows=[]
    for kind in ('shorts','topics'):
        clip=BatchClip(clip_id='clip_01',type='topic',hook_title='字幕と通常動画の動作確認',
                       start_time=100,end_time=160,score=0,reason='synthetic integration fixture',subtitles=[
                           {'start':101,'end':104,'text':'ちょうど話題が変わりました。aiを紹介します。',
                            'words':[{'start':101,'end':101.3,'text':'ちょ'},
                                     {'start':101.3,'end':101.8,'text':'うど'},
                                     {'start':101.8,'end':104,'text':'話題が変わりました。aiを紹介します。'}]},
                           {'start':110,'end':113,'text':'料理の準備と材料について説明します。'}])
        style=RichStyle.model_validate_json(Path('assets/style.'+('topics.' if kind=='topics' else '')+'example.json').read_text())
        receipt=render(source,root/kind,clip,style,'assets',kind)
        assert receipt['media']['fps']=='25'
        assert receipt['quality']['status']=='passed'
        ass=pysubs2.load(str(root/kind/'captions.ass'))
        srt=pysubs2.load(str(root/kind/'captions.srt'))
        events=[e for e in ass if e.style=='Default']
        assert [(e.start,e.end,e.plaintext) for e in events]==[(e.start,e.end,e.plaintext) for e in srt]
        assert all(len(e.plaintext.splitlines())<=2 for e in events)
        assert 'ちょうど' in ''.join(e.plaintext.replace('\n','') for e in events)
        rows.append({'output_kind':kind,'status':'passed','media':receipt['media'],'quality':receipt['quality']})
    write_json(root/'verification.json',{'status':'passed','checks':rows,'human_review_performed':False})
    source.unlink()
    print(json.dumps({'status':'passed','formats':['shorts','topics']}))


if __name__=='__main__':
    main()
