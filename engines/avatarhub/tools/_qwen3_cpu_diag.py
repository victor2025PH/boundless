# -*- coding: utf-8 -*-
"""Sample .117 python CPU% while qwen3 clone runs (is it CPU-bound at 1 thread?)."""
import base64
import subprocess
import threading
import time

import requests

TOK = open('secrets/service_token.txt', encoding='utf-8').read().strip()
H = {'X-AH-Svc': TOK}
REF = base64.b64encode(open('voice_clones/_shared/andy_ref.wav', 'rb').read()).decode()
REF_TEXT = '我记得我已经红了很久，很久很久，香港已经是已经差不多最红的时候。'

samples = []
stop = threading.Event()


def sampler():
    ps = ('$p = Get-Process -Id 31772 -ErrorAction SilentlyContinue; '
          'if ($p) { $t1=$p.TotalProcessorTime; Start-Sleep -Milliseconds 800; '
          '$p2 = Get-Process -Id 31772; $t2=$p2.TotalProcessorTime; '
          '$cores=[Environment]::ProcessorCount; '
          '[math]::Round((($t2-$t1).TotalMilliseconds/800)*100/$cores,1) }')
    b64 = base64.b64encode(ps.encode('utf-16-le')).decode()
    while not stop.is_set():
        r = subprocess.run(['ssh', '-o', 'ConnectTimeout=8', 'Administrator@192.168.0.117',
                            f'powershell -NoProfile -EncodedCommand {b64}'],
                           capture_output=True, text=True, timeout=20)
        line = r.stdout.strip()
        if line:
            samples.append(line + '% of all cores')
        time.sleep(0.3)


th = threading.Thread(target=sampler, daemon=True)
th.start()
t0 = time.time()
r = requests.post('http://192.168.0.117:7858/v1/tts/clone', json={
    'text': '看看到底是显卡忙不过来，还是处理器只有一个线程在干活。',
    'language': 'zh', 'reference_audio_b64': REF, 'reference_text': REF_TEXT,
    'temperature': 0.7, 'top_p': 0.7, 'repetition_penalty': 1.2, 'seed': 123},
    headers=H, timeout=180)
el = time.time() - t0
stop.set()
th.join(timeout=5)
print(f'clone took {el:.1f}s, http {r.status_code}')
print('python.exe CPU%% (normalized to all cores):')
for s in samples:
    print(' ', s)
