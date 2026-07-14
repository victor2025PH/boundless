# -*- coding: utf-8 -*-
"""调试：找 FB 搜索结果页面的 People 筛选 tab 真实位置"""
import sys, io, time, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, '.')

import uiautomator2 as u2
d = u2.connect('8DWOF6CYY5R8YHX8')

# 打开搜索
d.app_start('com.facebook.katana')
time.sleep(2)
d.click(633, 96)
time.sleep(1.5)
d.send_keys('Keiko Suzuki')
time.sleep(4)

# dump XML
xml = d.dump_hierarchy()
from src.host.device_registry import data_dir
(data_dir() / 'debug_search_xml.txt').write_text(xml, encoding='utf-8')
print('XML saved to data/debug_search_xml.txt')

# 找所有 y < 300 的可点击元素
lines = xml.split('\n')
for line in lines:
    if 'clickable="true"' in line and ('bounds' in line):
        bounds = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', line)
        if bounds:
            x1,y1,x2,y2 = map(int, bounds.groups())
            cy = (y1+y2)//2
            if cy < 300:
                text = re.search(r'text="([^"]*)"', line)
                desc = re.search(r'content-desc="([^"]*)"', line)
                t = (text.group(1) if text else '')[:50]
                dsc = (desc.group(1) if desc else '')[:60]
                print(f'  [{(x1+x2)//2},{cy}] text={t!r}  desc={dsc!r}')
