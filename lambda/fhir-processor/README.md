# FHIR SQL Processor Lambda

## Overview

This Lambda function processes SQL dump files from fEMR Kits, transforms them into HL7 FHIR R5 format, and stores them in the central RDS database using **intelligent differential updates**.

### Key Features

✅ **Differential Updates** - Only new/changed records are inserted or updated  
✅ **Duplicate Prevention** - Uses `legacy_id` to identify existing records  
✅ **FHIR R5 Compliance** - Full document-based exchange implementation  
✅ **Transaction Safety** - Atomic operations with rollback on errors  
✅ **Idempotent Processing** - Safe to reprocess same SQL dump  
✅ **Performance Optimized** - In-memory deduplication and batch processing  

## Architecture

```
fEMR Kit → Full DB Dump → S3 → Lambda (Differential Processor) → RDS (UPSERT)
                                        ↓
                                   FHIR R5 Transform
                                        ↓
                            Smart Deduplication via legacy_id
```

## Critical Concept: Differential Updates

**Problem**: Field kits upload **complete database dumps**, not incremental changes.

**Solution**: Lambda identifies what's NEW vs what's CHANGED:
- **New records** (by `legacy_id`) → INSERT
- **Existing records** → UPDATE if changed
- **Duplicate prevention** → `legacy_id` as unique identifier
- **In-memory tracking** → Avoid redundant operations within same dump

See [DIFFERENTIAL_UPDATES.md](./DIFFERENTIAL_UPDATES.md) for detailed strategy.

## FHIR Implementation

### Standard: HL7 FHIR R5
### Exchange Method: Document-Based Exchange

### Bundle Structure

Each SQL dump is processed into **one or more FHIR Bundles**, with **one Bundle per encounter**.

Each Bundle follows this structure:

1. **Composition** (MUST BE FIRST) - Describes the encounter document
2. **Patient** - Patient demographics and identifiers
3. **Practitioner(s)** - Healthcare providers (nurse, doctor, pharmacist)
4. **Encounter** - The medical encounter/visit
5. **Observation(s)** - Vital signs, clinical findings, flags
   - Blood Pressure (systolic/diastolic)
   - Heart Rate
   - Respiratory Rate
   - Body Temperature
   - Oxygen Saturation
   - Glucose Level
   - Height
   - Weight
   - Social history flags (smoking, diabetes, hypertension, alcohol, cholesterol)
   - Pregnancy status (weeks pregnant)
6. **MedicationRequest** - Prescriptions ordered
7. **MedicationDispense** - Medications dispensed
8. **DocumentReference** - Photos/images from the encounter

## Data Flow

### 1. SQL Parsing
- Download `.sql` or `.sql.gz` file from S3
- Decompress if needed
- Parse INSERT statements
- Extract data for:
  - Patients
  - Encounters
  - Vitals
  - Prescriptions
  - Diagnoses
  - Users (practitioners)
  - Photos

### 2. FHIR Transformation
- Group data by encounter
- For each encounter:
  - Create Bundle (type="document")
  - Create Composition (first entry)
  - Create Patient resource
  - Create Practitioner resources
  - Create Encounter resource
  - Create Observation resources (vitals, flags)
  - Create Medication resources
  - Create DocumentReference resources (photos)
- Validate FHIR resources

### 3. Database Storage
- Connect to RDS PostgreSQL
- **UPSERT logic** - Check if `legacy_id` exists, then UPDATE or INSERT
- Store FHIR resources in Django model tables:
  - `central_api_patient` - Patient demographics
  - `central_api_user` - Practitioners (nurses, doctors, pharmacists)
  - `central_api_patientencounter` - Encounter details with medical history flags
  - `central_api_patientencountervital` - Vital signs and observations
  - `central_api_patientprescriptions` - Medication requests and dispensing
  - `central_api_photo` - Document references (photos)
- Maintain referential integrity using central DB IDs
- **Deduplication** - Track processed records to avoid redundant operations
- Commit transaction (rollback on error)

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `RDS_HOST` | RDS database hostname | Yes |
| `RDS_PORT` | RDS database port | No (default: 5432) |
| `RDS_DB_NAME` | Database name | Yes |
| `RDS_USERNAME` | Database username | Yes |
| `RDS_PASSWORD` | Database password | Yes |

## FHIR Resource Mapping

### fEMR Model → FHIR Resource

| fEMR Django Model | FHIR R5 Resource | Notes |
|-------------------|------------------|-------|
| `Patient` | `Patient` | Demographics, identifiers, contact info |
| `User` (with roles) | `Practitioner` | Healthcare providers |
| `PatientEncounter` | `Encounter` + `Composition` | Visit details |
| `PatientEncounterVital` | `Observation` | Vitals, measurements |
| Encounter flags (smoking, diabetes, etc.) | `Observation` | Social history observations |
| `PatientPrescriptions` | `MedicationRequest` + `MedicationDispense` | Prescriptions and dispensing |
| `Diagnosis` | `Condition` | Clinical findings / diagnoses |
| `Photo` | `DocumentReference` | Clinical images |

## Code Structure

### Classes

#### `SQLParser`
Parses SQL dump files and extracts structured data.

**Methods:**
- `parse()` - Main parsing method
- `_parse_insert_statement()` - Extract data from INSERT statements
- `_extract_values()` - Parse VALUES clause
- `_parse_value()` - Parse individual values (strings, numbers, NULLs)

#### `FHIRTransformer`
Transforms parsed SQL data into FHIR R5 resources.

**Methods:**
- `create_bundles()` - Create FHIR Bundles (one per encounter)
- `_create_encounter_bundle()` - Create single Bundle for an encounter
- `_create_composition()` - Create Composition resource (first in Bundle)
- `_create_patient()` - Create Patient resource
- `_create_practitioners()` - Create Practitioner resources
- `_create_encounter()` - Create Encounter resource
- `_create_observations()` - Create Observation resources for vitals and flags
- `_create_conditions()` - Create Condition resources for clinical findings/diagnoses
- `_create_blood_pressure_observation()` - Special handling for BP (systolic/diastolic)
- `_create_vital_observation()` - Create simple vital sign observation
- `_create_medication_resources()` - Create MedicationRequest and MedicationDispense
- `_create_document_references()` - Create DocumentReference for photos

#### `RDSManager`
Manages database connections and differential storage.

**Key Features**:
- **UPSERT Operations** - Insert new or update existing records
- **Deduplication** - Track processed `legacy_id`s within execution
- **Transaction Management** - Atomic commits with rollback
- **Foreign Key Resolution** - Map `legacy_id` to central DB IDs

**Methods:**
- `connect()` - Establish RDS connection
- `close()` - Close connection
- `set_kit_id()` - Track which kit data came from
- `store_fhir_bundle()` - Main storage orchestration with differential logic
- `_extract_legacy_id()` - Extract `legacy_id` from FHIR identifiers
- `_upsert_patient()` - Insert or update patient (checks existing by `legacy_id`)
- `_upsert_practitioner()` - Insert or update user/practitioner
- `_upsert_encounter()` - Insert or update encounter with all relationships
- `_upsert_observation()` - Insert or update vital signs
- `_upsert_medication()` - Insert or update prescriptions
- `_upsert_document()` - Insert or update photo metadata
- `_get_*_db_id()` - Retrieve central DB ID for existing records

**Differential Logic Example**:
```python
# Check if patient exists by legacy_id
cursor.execute("SELECT id FROM central_api_patient WHERE legacy_id = %s", (101,))
existing = cursor.fetchone()

if existing:
    # UPDATE existing patient
    cursor.execute("UPDATE central_api_patient SET ... WHERE legacy_id = %s", (..., 101))
else:
    # INSERT new patient
    cursor.execute("INSERT INTO central_api_patient (..., legacy_id) VALUES (..., %s)", (..., 101))
```

### Lambda Handler
`lambda_handler(event, context)` - Main entry point triggered by S3 events

**Process Flow**:
1. Extract `kit_id` from S3 key path
2. Download and parse SQL dump
3. Create FHIR bundles (one per encounter)
4. Store bundles using differential UPSERT logic
5. Track processed encounters
6. Return summary with encounter IDs

**Differential Processing**:
- Maintains `processed_records` set to avoid duplicate operations
- Only processes each unique `legacy_id` once per execution
- Safe to reprocess same SQL dump (idempotent)

## LOINC Codes Used

| Code | Display | Use |
|------|---------|-----|
| 11506-3 | Progress note | Composition type |
| 85354-9 | Blood pressure panel | Blood pressure observation |
| 8480-6 | Systolic blood pressure | BP component |
| 8462-4 | Diastolic blood pressure | BP component |
| 8867-4 | Heart rate | Heart rate vital |
| 9279-1 | Respiratory rate | Respiratory rate vital |
| 8310-5 | Body temperature | Temperature vital |
| 2708-6 | Oxygen saturation | O2 saturation vital |
| 2339-0 | Glucose | Glucose level |
| 8302-2 | Body height | Height measurement |
| 29463-7 | Body weight | Weight measurement |
| 49051-6 | Gestational age in weeks | Pregnancy status |
| 72166-2 | Tobacco smoking status | Smoking flag |
| 44877-9 | History of diabetes | Diabetes flag |
| 55284-4 | History of hypertension | Hypertension flag |
| 74013-4 | Alcohol use | Alcohol flag |
| 55283-6 | History of high cholesterol | Cholesterol flag |

## Deployment

The Lambda is automatically deployed via CDK when you deploy the `FemrAwsStack`.

### CDK Configuration

```typescript
const fhirProcessor = new Function(scope, "FHIRProcessorLambda", {
  runtime: Runtime.PYTHON_3_13,
  timeout: Duration.minutes(15),
  memorySize: 1024,
  environment: {
    RDS_HOST: props.rdsHost,
    // ... other env vars
  }
});

// S3 trigger
const s3EventSource = new S3EventSource(dbDumpBucket, {
  events: [EventType.OBJECT_CREATED],
  filters: [
    { suffix: ".sql" },
    { suffix: ".sql.gz" }
  ]
});

fhirProcessor.addEventSource(s3EventSource);
```

## Testing

### Local Testing

1. Create a sample SQL dump file
2. Upload to S3 bucket: `aws s3 cp sample_dump.sql s3://femr-kit-db-dumps/kit_001/dump.sql`
3. Monitor CloudWatch logs for processing details
4. Verify records in RDS database

### Differential Update Testing

**Test Case 1: Initial Upload**
```sql
-- Upload dump with 10 patients, 20 encounters
Result: 10 patients INSERTED, 20 encounters INSERTED
```

**Test Case 2: Same Dump Reprocessed**
```sql
-- Upload same dump again
Result: 10 patients UPDATED (no duplicates), 20 encounters UPDATED
```

**Test Case 3: Incremental Changes**
```sql
-- Upload dump with:
-- - 10 existing patients (some data changed)
-- - 5 new patients
-- - 20 existing encounters
-- - 10 new encounters
Result: 
  - 10 patients UPDATED
  - 5 patients INSERTED
  - 20 encounters UPDATED
  - 10 encounters INSERTED
Total: 15 patients, 30 encounters (no duplicates)
```

### Validation

The Lambda validates:
- ✅ SQL parsing completeness
- ✅ FHIR resource validity (R5 compliance)
- ✅ Database integrity (foreign keys)
- ✅ Reference consistency (legacy_id mapping)
- ✅ No duplicate records (UPSERT verification)
- ✅ Transaction atomicity (all-or-nothing)

## Error Handling

- **SQL Parsing Errors**: Logged, skipped records continue processing
- **FHIR Validation Errors**: Logged, bundle skipped
- **Database Errors**: Transaction rollback, error logged
- **S3 Errors**: Lambda retries automatically

## Monitoring

### CloudWatch Metrics
- Invocations
- Duration
- Errors
- Throttles

### CloudWatch Logs
- SQL parsing progress
- FHIR bundle creation
- Database operations
- Errors and warnings

## Future Enhancements

1. **Change Detection Optimization** - Store checksums to skip truly unchanged records
2. **Audit Trail** - Log all INSERT/UPDATE operations for compliance
3. **Conflict Resolution** - Handle concurrent updates from multiple kits
4. **Soft Deletes** - Handle deleted records from field kits
5. **Bulk Operations** - PostgreSQL COPY for large batches
6. **Async Processing** - SQS queue for very large dumps
7. **Snapshot Comparison** - Explicit diff between consecutive uploads
8. **Data Validation** - Business rule validation before storage
9. **Rollback Capability** - Undo entire upload if needed
10. **Metrics Dashboard** - Real-time monitoring of sync status

## Key Files

- **process_sql_to_fhir.py** - Main Lambda function (1400+ lines)
- **DIFFERENTIAL_UPDATES.md** - Comprehensive differential update strategy guide
- **README.md** - This file
- **requirements.txt** - Python dependencies
- **sample_dump.sql** - Sample test data

## Documentation

- [Differential Updates Strategy](./DIFFERENTIAL_UPDATES.md) - Detailed explanation of UPSERT logic
- [HL7 FHIR R5 Specification](http://hl7.org/fhir/R5/)
- [FHIR Document Exchange](http://hl7.org/fhir/R5/documents.html)

## Dependencies

```
fhir.resources>=7.1.0     # FHIR R5 Python models
psycopg2-binary>=2.9.9    # PostgreSQL driver
sqlparse>=0.4.4           # SQL parsing
boto3>=1.34.0             # AWS SDK
```

## References

- [HL7 FHIR R5 Specification](http://hl7.org/fhir/R5/)
- [FHIR Document Exchange](http://hl7.org/fhir/R5/documents.html)
- [FHIR Bundle Resource](http://hl7.org/fhir/R5/bundle.html)
- [FHIR Composition Resource](http://hl7.org/fhir/R5/composition.html)
- [LOINC Codes](https://loinc.org/)

## Contact

For questions or issues, contact the fEMR development team.
