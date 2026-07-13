import os
import glob

def fix_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    new_lines = []
    for line in lines:
        # Fix timeout
        if 'requests.get(url, params=params, timeout=10)' in line:
            line = line.replace('requests.get(url, params=params, timeout=10)', 'requests.get(url, params=params, timeout=10)')
        elif 'requests.post(url, json=payload, timeout=10)' in line:
            line = line.replace('requests.post(url, json=payload, timeout=10)', 'requests.post(url, json=payload, timeout=10)')
            
        # Fix print
        # Only simple prints that don't already have flush=True
        # We find 'print(' and the last ')' and insert ', flush=True'
        if 'print(' in line and 'flush=True' not in line:
            # this works for single-line statements
            idx = line.rfind(')')
            if idx != -1:
                if line[idx-1] == '(':
                    line = line[:idx] + 'flush=True' + line[idx:]
                else:
                    line = line[:idx] + ', flush=True' + line[idx:]
                    
        new_lines.append(line)
        
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)

for p in glob.glob('/home/elwady/linux-2026/Projects/me/quant-system/backend-python/*.py'):
    fix_file(p)

print("Done", flush=True)
