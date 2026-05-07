import base64
import boto3
import gzip
import re
from Crypto.Cipher import AES

bucket='femr-kit-db-dumps-west'
key='kit-2bf989b2d01f/2026-04-07/20260407T212208446.sql.gz.encrypted'

s3=boto3.client('s3',region_name='us-west-2')
kms=boto3.client('kms',region_name='us-west-2')
obj=s3.get_object(Bucket=bucket,Key=key)
enc=obj['Body'].read()
meta=obj.get('Metadata',{})
enc_key_b64=meta.get('x-amz-encrypted-data-key')

plain_key=kms.decrypt(CiphertextBlob=base64.b64decode(enc_key_b64))['Plaintext']
plain=AES.new(plain_key,AES.MODE_ECB).decrypt(enc)

# remove PKCS7 padding if present
pad=plain[-1]
if 1 <= pad <= 16:
    plain=plain[:-pad]

try:
    sql=gzip.decompress(plain)
except Exception:
    sql=plain

text=sql.decode('latin-1','replace')
print('starts_with:',repr(text[:120]))
for table in ['patients','patient_encounters','patient_encounter_vitals']:
    m=re.search(rf"INSERT INTO `?{table}`?(?:\s*\((.*?)\))?\s*VALUES", text, flags=re.IGNORECASE|re.DOTALL)
    if not m:
        print(table,': NOT FOUND')
        continue
    has_cols=bool(m.group(1))
    print(table,':','WITH_COLUMNS' if has_cols else 'NO_COLUMNS')

m2=re.search(r"INSERT INTO `?patient_encounters`?.{0,300}VALUES", text, flags=re.IGNORECASE|re.DOTALL)
print('sample:', m2.group(0).replace('\n',' ') if m2 else 'none')
