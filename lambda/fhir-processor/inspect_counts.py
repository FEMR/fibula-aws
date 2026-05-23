import gzip,re
from collections import Counter
with gzip.open('latest_dump.sql.gz','rb') as f:
    txt=f.read().decode('latin-1')
tables=re.findall(r"INSERT INTO\s+`?(\w+)`?",txt,re.IGNORECASE)
c=Counter(tables)
for name in ['patient_encounters','patientencounters','patient_encounter','patientencounter','patient_encounter_vitals','patientencountervital','patients','users','patient_encounter_tab_fields','vitals']:
    print(name,c.get(name,0))
