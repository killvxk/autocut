import argparse
import datetime
import time
import os 
import re

import srt
import opencc
from moviepy import editor

_EDIT_DONE = 'DONE EDITTING'

def _expand_segments(segments, expand_head, expand_tail, total_length):
    # Pad head and tail for each time segment
    results = []
    for i in range(len(segments)):
        t = segments[i]
        start = max(t['start'] - expand_head,
            segments[i-1]['end'] if i > 0 else 0)
        end = min(t['end'] + expand_tail,
            segments[i+1]['start'] if i < len(segments)-1 else total_length)
        results.append({'start':start, 'end':end})
    return results

def _remove_short_segments(segments, threshold): 
    # Remove segments whose length < threshold
    return [s for s in segments if s['end'] - s['start'] > threshold]

def _merge_adjacent_segments(segments, threshold):
    # Merge two adjacent segments if their distance < threshold
    results = []
    i = 0
    while i < len(segments):
        s = segments[i]
        for j in range(i+1, len(segments)):
            if segments[j]['start'] < s['end'] + threshold:
                s['end'] = segments[j]['end']
                i = j
            else:
                break
        i += 1
        results.append(s) 
    return results

def _check_exists(output, force):
    if os.path.exists(output):
        if force:
            print(f'{output} exists. Will ovewrite it')
        else:
            print(f'{output} exists, skipping... Use the --force flag to overwrite')
            return True
    return False 

class Transcribe:
    def __init__(self, args):
        self.args = args
        self.sampling_rate = 16000
        self.whisper_model = None
        self.vad_model = None
        self.detect_speech = None

    def run(self):
        import whisper

        for input in self.args.inputs:
            print(f'Transcribing {input}')
            name, _ = os.path.splitext(input)
            output = name +'.srt'
            if _check_exists(output, self.args.force):
                continue

            audio = whisper.load_audio(input, sr=self.sampling_rate)
            speech_timestamps = self._detect_voice_activity(audio)
            transcribe_results = self._transcibe(audio, speech_timestamps)
            
            self._save_srt(output, transcribe_results)
            print(f'Transcribed {input} to {output}')
            if self.args.md:
                self._save_md(name+'.md', output)
                print(f'Saved sentences to {name+".md"} to mark')

    def _detect_voice_activity(self, audio):
        """Detect segments that have voice activities"""
        tic = time.time()
        if self.vad_model is None or self.detect_speech is None:
            import torch

            self.vad_model, utils = torch.hub.load(
                repo_or_dir='snakers4/silero-vad',
                model='silero_vad',
                trust_repo=True)
            
            self.detect_speech = utils[0]
        
        speeches = self.detect_speech(audio, self.vad_model, 
            sampling_rate=self.sampling_rate)
        
        # Merge very closed segments
        # speeches = _merge_adjacent_segments(speeches, 0.5 * self.sampling_rate)

        # Remove too short segments 
        # speeches = _remove_short_segments(speeches, 1.0 * self.sampling_rate)

        # Expand to avoid to tight cut. You can tune the pad length
        speeches =  _expand_segments(speeches, 0.2*self.sampling_rate, 
            0.0*self.sampling_rate, audio.shape[0])
        
        print(f'Done voice activity detetion in {time.time()-tic:.1f} sec')
        return speeches

    def _transcibe(self, audio, speech_timestamps):
        tic = time.time()
        if self.whisper_model is None:
            import whisper             
            self.whisper_model = whisper.load_model(self.args.whisper_model)        

        res = []
        for seg in speech_timestamps:    
            r = self.whisper_model.transcribe(
                    audio[int(seg['start']):int(seg['end'])],
                    task='transcribe', language='zh')#, initial_prompt=self.args.prompt)
            r['origin_timestamp'] = seg
            res.append(r)
        print(f'Done transcription in {time.time()-tic:.1f} sec')
        return res

    def _save_srt(self, output, transcribe_results):
        subs = []
        # whisper sometimes generate traditional chinese, explicitly convert
        cc = opencc.OpenCC('t2s')

        def _add_sub(start, end, text):
            subs.append(srt.Subtitle(index=0, 
                start=datetime.timedelta(seconds=start),
                end=datetime.timedelta(seconds=end), 
                content=cc.convert(text.strip())))

        prev_end = 0
        for r in transcribe_results:
            origin = r['origin_timestamp']
            for s in r['segments']:                
                start = s['start'] + origin['start'] / self.sampling_rate
                end = min(s['end'] + origin['start'] / self.sampling_rate, origin['end'] / self.sampling_rate)
                # mark any empty segment that is not very short
                if start > prev_end + 1.0:
                    _add_sub(prev_end, start, '< No Speech >')
                _add_sub(start, end, s["text"])
                prev_end = end
        
        with open(output, 'w') as f:
            f.write(srt.compose(subs))

    def _save_md(self, md_fn, srt_fn):
        with open(srt_fn) as f:
            subs = srt.parse(f.read())
        basename = os.path.basename(srt_fn)
        MARK = '- [ ] '
        md = [
            f'Texts generated from [{basename}]({basename}). Mark the sentences to keep for autocut. Then mark the following item if done editing.\n',
            MARK + _EDIT_DONE,
            '\nThe format is [subtitle_index,duration_in_second] subtitle context\n']
        for s in subs:
            dur = (s.end - s.start).total_seconds()
            pre = f'{MARK}[{s.index},{dur:.1f}s]'
            md.append(
                f'{pre:15} {s.content.strip()}'
            )
        with open(md_fn, 'w') as f:
            f.write('\n'.join(md))

class Cutter:
    def __init__(self, args):
        self.args = args
        
    def run(self):
        fns = {'srt':None, 'video':None, 'md':None}
        for fn in self.args.inputs:            
            ext = os.path.splitext(fn)[1][1:]            
            fns[ext if ext in fns else 'video'] = fn

        assert fns['video'], 'must provide a video filename'
        assert fns['srt'], 'must provide a srt filename'
        if self.args.md:
            assert fns['md'], 'must provide a md filename'
            print(f'Cut {fns["video"]} based on {fns["srt"]} and {fns["md"]}')    
        else:
            print(f'Cut {fns["video"]} based on {fns["srt"]}')    

        output_fn = os.path.splitext(fns['video'])[0] + '_autocut.mp4'
        if _check_exists(output_fn, self.args.force):
            return

        segments = []
        with open(fns['srt']) as f:
            subs = list(srt.parse(f.read()))

        if self.args.md:
            done, index = self._marked_index(fns['md'])
            if not done:
                print(f'{fns["md"]} is not marked as done editting. skip...')
                return 
            subs = [s for s in subs if s.index in index]

        for x in subs:
            segments.append({'start':x.start.total_seconds(), 'end':x.end.total_seconds()})

        video = editor.VideoFileClip(fns['video'])
        
        # Add a fade between two clips. Not quite necesary. keep code here for reference
        # fade = 0
        # segments = _expand_segments(segments, fade, 0, video.duration)
        # clips = [video.subclip(
        #         s['start'], s['end']).crossfadein(fade) for s in segments]
        # final_clip = editor.concatenate_videoclips(clips, padding = -fade)

        clips = [video.subclip(s['start'], s['end']) for s in segments]
        final_clip = editor.concatenate_videoclips(clips)
        print(f'Reduced duration from {video.duration:.1f} to {final_clip.duration:.1f}')

        aud = final_clip.audio.set_fps(44100)
        final_clip = final_clip.without_audio().set_audio(aud)
        final_clip = final_clip.fx(editor.afx.audio_normalize)
        
        # an alterantive to birate is use crf, e.g. ffmpeg_params=['-crf', '18']
        final_clip.write_videofile(output_fn, audio_codec='aac', logger=None, bitrate=self.args.bitrate)
        print(f'Saved video to {output_fn}')

    def _marked_index(self, md_fn):
        with open(md_fn) as f:
            lines = f.readlines()
        done = False
        ret = []
        for l in lines:
            m = re.match(r'- +\[([ x])\] +'+_EDIT_DONE, l)
            if m:
                done = m.groups()[0] == 'x'
            m = re.match(r'- +\[([ x])\] +\[(\d+)', l)        
            if m and m.groups()[0] == 'x':
                ret.append(int(m.groups()[1]))
        return done, ret

class Daemon:
    def __init__(self, args):
        self.args = args

    def run(self):
        pass 
    
def main():
    parser = argparse.ArgumentParser(description='Edit videos based on transcribed subtitles',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:

# Transcribe a video into subtitles
autocut -t my_video.mp4
# Delete uncessary sentences in my_video.srt, then
# generate a new video with only these sentences kept
autocut -c my_video.mp4 my_video.srt

Note that you can transcribe multiple vidoes at the same time to 
slightly make it faster:

autocut -t my_video_*.mp4

''')

    parser.add_argument('inputs', type=str, nargs='+',
                        help='Inputs filenames/folders')
    parser.add_argument('-t', '--transcribe', help='Transcribe videos/audio into subtitles', 
        action=argparse.BooleanOptionalAction)
    parser.add_argument('-c', '--cut', help='Cut a video based on subtitles', 
        action=argparse.BooleanOptionalAction)
    parser.add_argument('-d', '--daemon', help='Monitor a folder to trascribe and cut', 
        action=argparse.BooleanOptionalAction)
    parser.add_argument('--prompt', type=str, default='', 
        help='initial prompt feed into whisper')
    parser.add_argument('--whisper-model', type=str, default='small',
        choices=['tiny', 'base', 'small', 'medium', 'large'],
        help='The whisper model used to transcribe.')
    parser.add_argument('--bitrate', type=str, default='1m',
        help='The bitrate to export the cutted video, such as 10m, 1m, or 500k')
    parser.add_argument('--force', help='Force write even if files exist', 
        action=argparse.BooleanOptionalAction)
    parser.add_argument('--md', help='Use markdown files to select sentences',
        action=argparse.BooleanOptionalAction)

    
    args = parser.parse_args()

    if args.transcribe:
        trans = Transcribe(args)
        trans.run()        
    elif args.cut:
        cutter = Cutter(args)
        cutter.run()
    elif args.daemon:
        daemon = Daemon(args)
        daemon.run()

    else:
        print('No action, use -c, -t or -d')
    
if __name__ == "__main__":
    main()
