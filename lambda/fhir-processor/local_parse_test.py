from process_sql_to_fhir import SQLParser
p=r"c:\Users\udupa\Downloads\2026-04-07T20_37_52.469Z (1).sql\2026-04-07T20_37_52.469Z (1).sql"
text=open(p,'r',encoding='latin-1').read()
parsed=SQLParser(text).parse()
print('patients',len(parsed['patients']))
print('encounters',len(parsed['encounters']))
print('vitals',len(parsed['vitals']))
if parsed['encounters']:
    print('encounter_keys_sample',sorted(parsed['encounters'][0].keys())[:12])
