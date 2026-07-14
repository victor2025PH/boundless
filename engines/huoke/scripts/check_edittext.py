# -*- coding: utf-8 -*-
import uiautomator2 as u2, sys
sys.path.insert(0, '.')
d = u2.connect('LVHIOZSWDAYLCELN')
xml = d.dump_hierarchy()
out = open('data/edittext_raw.txt', 'w', encoding='utf-8')
for line in xml.split('\n'):
    if 'EditText' in line:
        out.write(line[:500] + '\n')
        break
# Also check EditText element attributes
el = d(className='android.widget.EditText')
if el.exists:
    info = el.info
    out.write(f'focused: {info.get("focused")}\n')
    out.write(f'text: {repr(info.get("text",""))}\n')
    out.write(f'content_desc: {repr(info.get("contentDescription",""))}\n')
out.close()
print('done')
