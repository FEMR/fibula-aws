import gzip,re
from collections import Counter
with gzip.open('latest_dump.sql.gz','rb') as f:
    data=f.read()
print('bytes',len(data))
text=None
for enc in ('utf-8','latin-1'):
    try:
        text=data.decode(enc)
        print('decoded',enc,'chars',len(text))
        break
    except Exception as e:
        print('decode_failed',enc,str(e)[:120])
if text is None:
    raise SystemExit(1)
tables=re.findall(r"INSERT INTO\s+`?(\w+)`?",text,re.IGNORECASE)
c=Counter(tables)
print('insert_tables_count',len(c))
for name in ['patient','patients','encounter','encounters','vitals','vital','medication','prescription','users','photo','play_evolutions']:
    print(f'{name}={c.get(name,0)}')
print('top15=',c.most_common(15))
