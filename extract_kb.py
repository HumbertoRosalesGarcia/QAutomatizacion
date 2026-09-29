import subprocess, re, json, os

aapt = r'C:\Users\Administrador\AppData\Local\Android\Sdk\build-tools\35.0.0\aapt.exe'
apk = os.path.abspath('review_temp/ontheflypos.apk')
out_json = os.path.abspath('review_temp/onthefly_knowledge_base.json')

res = subprocess.run([aapt, 'dump', 'resources', apk], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace')
hex_map = {}
resources_by_type = {}

pattern = re.compile(r'spec resource (0x[0-9a-fA-F]+) com\.kubilabs\.ontheflypos:([^/]+)/([^:]+):')

for line in res.stdout.splitlines():
    m = pattern.search(line)
    if m:
        h = m.group(1).lower()
        res_type = m.group(2)
        res_id = m.group(3)
        full_name = f'com.kubilabs.ontheflypos:{res_type}/{res_id}'
        hex_map[h] = full_name
        if res_type not in resources_by_type:
            resources_by_type[res_type] = []
        resources_by_type[res_type].append(res_id)

print(f'Extracted {len(hex_map)} resources!')
with open(out_json, 'w', encoding='utf-8') as f_out:
    json.dump({'hex_map': hex_map, 'resources': resources_by_type}, f_out, ensure_ascii=False)
print('Saved successfully!')
