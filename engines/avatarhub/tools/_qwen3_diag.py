# -*- coding: utf-8 -*-
"""Qwen3-TTS on .117 slow-synthesis diagnosis: sample GPU util/clock while a clone request runs."""
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
    ps = ('nvidia-smi --query-gpu=utilization.gpu,memory.used,clocks.sm,power.draw '
          '--format=csv,noheader,nounits')
    b64 = base64.b64encode(ps.encode('utf-16-le')).decode()
    while not stop.is_set():
        r = subprocess.run(['ssh', '-o', 'ConnectTimeout=8', 'Administrator@192.168.0.117',
                            f'powershell -NoProfile -EncodedCommand {b64}'],
                           capture_output=True, text=True, timeout=20)
        line = r.stdout.strip()
        if line:
            samples.append(line)
        time.sleep(1.0)


th = threading.Thread(target=sampler, daemon=True)
th.start()
t0 = time.time()
r = requests.post('http://192.168.0.117:7858/v1/tts/clone', json={
    'text': '正在诊断合成速度瓶颈，看看显卡的利用率到底是什么水平。',
    'language': 'zh', 'reference_audio_b64': REF, 'reference_text': REF_TEXT,
    'temperature': 0.7, 'top_p': 0.7, 'repetition_penalty': 1.2, 'seed': 123},
    headers=H, timeout=180)
el = time.time() - t0
stop.set()
th.join(timeout=5)
print(f'clone took {el:.1f}s, http {r.status_code}')
print('gpu samples (util%, memMiB, smMHz, powerW):')
for s in samples:
    print(' ', s)
