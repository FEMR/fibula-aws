import gzip,re
with gzip.open('latest_dump.sql.gz','rb') as f:
    txt=f.read().decode('latin-1')
for m in re.finditer(r"INSERT INTO\s+`?patient_encounters`?.*?;", txt, re.IGNORECASE|re.DOTALL):
    s=m.group(0)
    print('FOUND_LEN',len(s))
    print(s[:1200])
    print('---END_SNIPPET---')
    break
else:
    print('NOT_FOUND')
