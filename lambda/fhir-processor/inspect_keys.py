import re
text=open('process_sql_to_fhir.py','r',encoding='utf-8').read()
keys=sorted(set(re.findall(r"encounter\.get\('([^']+)'",text)))
print('encounter_keys',keys)
keys2=sorted(set(re.findall(r"patient\.get\('([^']+)'",text)))
print('patient_keys',keys2)
keys3=sorted(set(re.findall(r"vital\.get\('([^']+)'",text)))
print('vital_keys',keys3)
