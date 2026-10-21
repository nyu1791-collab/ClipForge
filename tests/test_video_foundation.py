"""Local regression checks. Real CPU rendering is confined to private workers."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pysubs2

from clipforge import captions
from clipforge.batch import plan, speech_end
from clipforge.batch_models import ClipsManifest
from clipforge.batch_workflow import cache_lock
from clipforge.caption_layout import line_break, timed_units, units, NO_START
from clipforge.fonts import Measure
from clipforge.media import frame_rate
from clipforge.models import SelectedClip, Settings, Transcript, Word
from clipforge.storage import PipelineError
from clipforge.topic_selection import plan_topics

URL='https://kick.com/example/videos/01a1574c-2350-77ce-90e2-4a7fefd4235c'


class VideoFoundationTests(unittest.TestCase):
    def transcript(self):
        topics=['料理の材料を切り鍋に入れて煮込む夕食の準備',
                '宇宙の惑星と恒星の観測には望遠鏡が必要',
                '旅行のホテルと空港から電車に乗り観光地を歩く',
                '野球の投手が打者に向けて速球を投げホームラン',
                '自作パソコンのメモリと半導体を基板に接続',
                '音楽の楽譜を読みギターとピアノで歌を演奏',
                '小説の登場人物が推理して事件の犯人を探す',
                '川で魚を釣るため餌と針と竿を用意する',
                '農業の畑で野菜と果物を収穫し市場へ運ぶ',
                '病院の医師と看護師が患者の健康を診察する']
        return Transcript.model_validate({'language':'ja','segments':[
            {'start':t,'end':t+5,'text':topics[t//180]+'。',
             'words':[{'start':t,'end':t+5,'text':topics[t//180]+'。'}]}
            for t in range(0,1800,6)]})

    def test_topics_are_longer_nonoverlapping_and_end_at_speech_boundary(self):
        t=self.transcript()
        m=plan_topics(URL,1800,t,count=3,seconds=180)
        self.assertEqual(m.output_kind,'topics')
        self.assertEqual(len(m.clips),3)
        for c in m.clips:
            self.assertIn(c.end_time,{s.end for s in t.segments})
            self.assertTrue(150 <= c.end_time-c.start_time <= 210)
            self.assertTrue(all(c.start_time<=s.start<s.end<=c.end_time for s in c.subtitles))
        for a,b in zip(m.clips,m.clips[1:]):
            self.assertLessEqual(a.end_time,b.start_time)

    def test_topic_plan_refuses_silence_or_insufficient_distinct_ranges(self):
        with self.assertRaises(PipelineError):
            plan_topics(URL,1000,Transcript(language='ja',segments=[]))
        with self.assertRaises(PipelineError):
            plan_topics(URL,40,self.transcript(),seconds=300)

    def test_profile_limits_and_schema_backwards_compatibility(self):
        value=json.loads(Path('jobs/examples/kick-test-3.json').read_text())
        self.assertEqual(ClipsManifest.model_validate(value).output_kind,'shorts')
        value['output_kind']='topics'
        with self.assertRaises(ValueError):
            ClipsManifest.model_validate(value)
        for i,c in enumerate(value['clips']):
            c.update(type='topic',start_time=i*400,end_time=i*400+300)
        ClipsManifest.model_validate(value)
        value['output_kind']='shorts'
        with self.assertRaises(ValueError):
            ClipsManifest.model_validate(value)

    def test_source_lock_refuses_duplicate_analysis(self):
        with tempfile.TemporaryDirectory() as d, cache_lock(d,'example'):
            with self.assertRaises(PipelineError):
                with cache_lock(d,'example'):
                    pass

    def test_fps_preserves_25_and_fractional_rates_and_caps_high_rates(self):
        self.assertEqual(frame_rate('25/1'),'25')
        self.assertEqual(frame_rate('30000/1001'),'30000/1001')
        self.assertEqual(frame_rate('60000/1001'),'30')
        self.assertEqual(frame_rate('0/0'),'30')

    def test_morphology_rejoins_split_kana_and_preserves_whitespace_and_punctuation(self):
        for text in ['ちょうど話題が変わりました。','AI と AGI を比較します。','「料理の時間」です。',' hello  world ', '123万円です。']:
            self.assertEqual(''.join(units(text)),text)
        words=[Word(start=0,end=0.3,text='ちょ'),Word(start=0.3,end=0.8,text='うど'),
               Word(start=0.8,end=1.5,text='話題が変わりました。')]
        parts,_=timed_units(words)
        self.assertIn('ちょうど',[w.text for w in parts])
        self.assertTrue(all(w.text[0] not in NO_START for w in parts))

    def test_real_font_two_lines_srt_consistency_and_sanitization(self):
        t=Transcript.model_validate({'language':'ja','segments':[
            {'start':100,'end':103.5,'text':'ちょうど話題が変わりました。aiを紹介します。',
             'words':[{'start':100,'end':100.3,'text':'ちょ'},
                      {'start':100.3,'end':100.8,'text':'うど'},
                      {'start':100.8,'end':103.5,'text':'話題が変わりました。aiを紹介します。'}]},
            {'start':105,'end':106,'text':r'{\pos(0,0)}'}]})
        clip=SelectedClip(start=100,end=130,candidate_id='clip_01',title='料理の話題',reason='test',confidence=0)
        for aspect in ('9:16','16:9'):
            with tempfile.TemporaryDirectory() as d:
                root=Path(d)
                r=captions.create(t,clip,root,Settings(gemini_model='local',headline=True,aspect=aspect))
                ass=pysubs2.load(str(root/'captions.ass'))
                srt=pysubs2.load(str(root/'captions.srt'))
                events=[e for e in ass if e.style=='Default']
                self.assertEqual([(e.start,e.end,e.plaintext) for e in events],[(e.start,e.end,e.plaintext) for e in srt])
                self.assertTrue(all(len(e.plaintext.splitlines())<=2 for e in events))
                self.assertTrue(all(0<=e.start<e.end<=30000 for e in events))
                self.assertTrue(all(a.end<=b.start for a,b in zip(events,events[1:])))
                spoken=''.join(e.plaintext.replace('\n','') for e in events)
                self.assertIn('ちょうど',spoken)
                self.assertIn('AI',spoken)
                self.assertNotIn(r'{\pos',spoken)
                self.assertEqual(r['missing_glyphs'],[])

    def test_no_speech_end_never_forces_a_mid_utterance_cut(self):
        t=Transcript.model_validate({'language':'ja','segments':[{'start':0,'end':200,'text':'long'}]})
        self.assertEqual(speech_end(t,0,45,300,30,180),0)


if __name__=='__main__':
    unittest.main()
