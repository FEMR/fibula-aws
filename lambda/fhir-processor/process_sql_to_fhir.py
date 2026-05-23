"""
Lambda function to process SQL dumps from S3, convert to FHIR R5 format,
and store in RDS database.

Architecture:
- Triggered by S3 upload of .sql/.sql.gz files to femr-kit-db-dumps bucket
- Parses SQL dump and extracts patient encounter data
- Creates FHIR R5 Bundles (one per encounter) with Composition-first structure
- Stores data in Django RDS database
"""

import boto3
import gzip
import json
import os
import re
import sqlparse
import base64
import pyaes
from datetime import datetime
from typing import Dict, List, Any, Optional
import logging

# FHIR R5 imports
from fhir.resources.bundle import Bundle, BundleEntry
from fhir.resources.composition import Composition, CompositionSection
from fhir.resources.patient import Patient, PatientContact
from fhir.resources.humanname import HumanName
from fhir.resources.contactpoint import ContactPoint
from fhir.resources.address import Address
from fhir.resources.practitioner import Practitioner
from fhir.resources.encounter import Encounter, EncounterParticipant
from fhir.resources.observation import Observation, ObservationComponent
from fhir.resources.condition import Condition
from fhir.resources.medicationrequest import MedicationRequest
from fhir.resources.medicationdispense import MedicationDispense
from fhir.resources.documentreference import DocumentReference, DocumentReferenceContent
from fhir.resources.attachment import Attachment
from fhir.resources.reference import Reference
from fhir.resources.codeableconcept import CodeableConcept
from fhir.resources.coding import Coding
from fhir.resources.identifier import Identifier
from fhir.resources.quantity import Quantity
from fhir.resources.codeablereference import CodeableReference

# Database imports
import pymysql
import pymysql.cursors

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS clients
s3 = boto3.client('s3')
kms = boto3.client('kms')

# Environment variables
RDS_HOST = os.environ.get('RDS_HOST')
RDS_PORT = os.environ.get('RDS_PORT', '3306')
RDS_DB_NAME = os.environ.get('RDS_DB_NAME')
RDS_USERNAME = os.environ.get('RDS_USERNAME')
RDS_PASSWORD = os.environ.get('RDS_PASSWORD')


class SQLParser:
    """Parse SQL dump files and extract structured data."""
    
    def __init__(self, sql_content: str):
        self.sql_content = sql_content
        self.table_columns = self._extract_table_columns(sql_content)
        self.parsed_data = {
            'patients': [],
            'encounters': [],
            'vitals': [],
            'prescriptions': [],
            'diagnoses': [],
            'users': [],
            'photos': [],
            'mission_trips': []
        }
    
    def parse(self) -> Dict[str, List[Dict]]:
        """Parse SQL and extract all relevant data."""
        logger.info("Starting SQL parsing...")
        
        # Parse SQL statements
        statements = sqlparse.parse(self.sql_content)
        
        for statement in statements:
            if statement.get_type() == 'INSERT':
                self._parse_insert_statement(str(statement))
        
        logger.info(f"Parsed {len(self.parsed_data['encounters'])} encounters")
        return self.parsed_data

    def _extract_table_columns(self, sql_content: str) -> Dict[str, List[str]]:
        """Extract ordered column names from CREATE TABLE statements."""
        table_columns: Dict[str, List[str]] = {}
        create_table_pattern = re.compile(
            r'CREATE TABLE\s+`?(\w+)`?\s*\((.*?)\)\s*ENGINE=',
            re.IGNORECASE | re.DOTALL
        )

        for table_name, table_def in create_table_pattern.findall(sql_content):
            columns: List[str] = []
            for raw_line in table_def.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if line.upper().startswith((
                    'PRIMARY KEY', 'UNIQUE KEY', 'KEY ', 'CONSTRAINT', 'INDEX ', 'FULLTEXT'
                )):
                    continue
                column_match = re.match(r'`([^`]+)`\s+', line)
                if column_match:
                    columns.append(column_match.group(1))

            if columns:
                table_columns[table_name.lower()] = columns

        return table_columns
    
    def _parse_insert_statement(self, statement: str):
        """Parse INSERT statement and extract ALL rows (handles multi-row VALUES)."""
        # Extract table name
        table_match = re.search(r'INSERT INTO\s+`?(\w+)`?', statement, re.IGNORECASE)
        if not table_match:
            return

        table_name = table_match.group(1)

        # Extract column names (everything between first '(' and 'VALUES')
        columns_match = re.search(r'\((.*?)\)\s+VALUES', statement, re.IGNORECASE | re.DOTALL)
        columns: List[str]
        if columns_match:
            columns = [col.strip('`" ') for col in columns_match.group(1).split(',')]
        else:
            columns = self.table_columns.get(table_name.lower(), [])
            if not columns:
                return

        # Find the VALUES section and extract EVERY row '(...)'
        values_section_match = re.search(r'VALUES\s*(.*)', statement, re.IGNORECASE | re.DOTALL)
        if not values_section_match:
            return

        all_rows = self._extract_all_value_rows(values_section_match.group(1))

        for row_str in all_rows:
            values = self._extract_values(row_str)
            if len(columns) != len(values):
                if table_name.lower() == 'play_evolutions':
                    continue
                logger.warning(f"Column/value mismatch for table {table_name}: "
                               f"{len(columns)} cols vs {len(values)} values")
                continue

            row_data = dict(zip(columns, values))

            if table_name.lower() in ['patient', 'patients']:
                self.parsed_data['patients'].append(row_data)
            elif table_name.lower() in ['patientencounter', 'patient_encounter', 'patient_encounters']:
                self.parsed_data['encounters'].append(row_data)
            elif table_name.lower() in ['patientencountervital', 'patient_encounter_vital', 'patient_encounter_vitals']:
                self.parsed_data['vitals'].append(row_data)
            elif table_name.lower() in ['patientprescriptions', 'patient_prescriptions']:
                self.parsed_data['prescriptions'].append(row_data)
            elif table_name.lower() in ['diagnosis', 'diagnoses']:
                self.parsed_data['diagnoses'].append(row_data)
            elif table_name.lower() in ['user', 'users']:
                self.parsed_data['users'].append(row_data)
            elif table_name.lower() in ['photo', 'photos']:
                self.parsed_data['photos'].append(row_data)
            elif table_name.lower() in ['missiontrip', 'mission_trip', 'mission_trips']:
                self.parsed_data['mission_trips'].append(row_data)

    def _extract_all_value_rows(self, values_section: str) -> List[str]:
        """
        Given the text after VALUES, return the inner content of each '(...)' row.
        Handles nested parentheses (e.g. function calls) and quoted strings.
        """
        rows: List[str] = []
        depth = 0
        current = ''
        in_string = False
        escape_next = False

        for char in values_section:
            if escape_next:
                if in_string:
                    current += char
                escape_next = False
                continue

            if char == '\\':
                escape_next = True
                if in_string:
                    current += char
                continue

            if char == "'":
                in_string = not in_string
                current += char
                continue

            if in_string:
                current += char
                continue

            if char == '(':
                depth += 1
                if depth == 1:
                    current = ''   # start fresh - don't include outer paren
                    continue
                current += char  # keep inner parens for nested calls
            elif char == ')':
                depth -= 1
                if depth == 0:
                    rows.append(current)
                    current = ''
                else:
                    current += char
            else:
                if depth > 0:
                    current += char

        return rows
    
    def _extract_values(self, values_str: str) -> List[Any]:
        """Extract values from VALUES clause."""
        values = []
        current_value = ''
        in_string = False
        escape_next = False
        
        for char in values_str:
            if escape_next:
                current_value += char
                escape_next = False
                continue
            
            if char == '\\':
                escape_next = True
                continue
            
            if char == "'" and not escape_next:
                in_string = not in_string
                continue
            
            if char == ',' and not in_string:
                values.append(self._parse_value(current_value.strip()))
                current_value = ''
                continue
            
            current_value += char
        
        # Add last value
        if current_value.strip():
            values.append(self._parse_value(current_value.strip()))
        
        return values
    
    def _parse_value(self, value: str) -> Any:
        """Parse individual value."""
        value = value.strip()
        
        if value.upper() == 'NULL':
            return None
        
        # Remove quotes
        if (value.startswith("'") and value.endswith("'")) or \
           (value.startswith('"') and value.endswith('"')):
            return value[1:-1]
        
        # Try to parse as number
        try:
            if '.' in value:
                return float(value)
            return int(value)
        except ValueError:
            return value


class FHIRTransformer:
    """Transform parsed SQL data into FHIR R5 Bundles."""
    
    def __init__(self, parsed_data: Dict[str, List[Dict]]):
        self.data = parsed_data
        self.bundles = []
    
    def create_bundles(self) -> List[Bundle]:
        """Create FHIR Bundles - one per encounter."""
        logger.info("Creating FHIR Bundles...")
        
        for encounter_data in self.data['encounters']:
            try:
                bundle = self._create_encounter_bundle(encounter_data)
                if bundle:
                    self.bundles.append(bundle)
            except Exception as e:
                logger.error(f"Error creating bundle for encounter: {e}", exc_info=True)
        
        logger.info(f"Created {len(self.bundles)} FHIR bundles")
        return self.bundles
    
    def _create_encounter_bundle(self, encounter_data: Dict) -> Optional[Bundle]:
        """Create a single FHIR Bundle for an encounter."""
        encounter_id = encounter_data.get('id') or encounter_data.get('legacy_id')
        patient_id = encounter_data.get('patient_id') or encounter_data.get('patient')
        
        if not encounter_id or not patient_id:
            logger.warning("Missing encounter or patient ID")
            return None
        
        # Find associated patient
        patient_data = self._find_patient(patient_id)
        if not patient_data:
            logger.warning(f"Patient {patient_id} not found")
            return None
        
        # Create Bundle
        bundle = Bundle(
            type="document",
            identifier=Identifier(
                system="http://femr.org/bundle-id",
                value=f"encounter-{encounter_id}"
            )
        )
        
        entries = []
        
        # 1. COMPOSITION (MUST BE FIRST!)
        composition = self._create_composition(encounter_data, patient_id, encounter_id)
        entries.append(BundleEntry(
            fullUrl=f"urn:uuid:composition-{encounter_id}",
            resource=composition
        ))
        
        # 2. PATIENT
        patient = self._create_patient(patient_data)
        entries.append(BundleEntry(
            fullUrl=f"urn:uuid:patient-{patient_id}",
            resource=patient
        ))
        
        # 3. PRACTITIONERS
        practitioners = self._create_practitioners(encounter_data)
        for pract in practitioners:
            entries.append(pract)
        
        # 4. ENCOUNTER
        encounter = self._create_encounter(encounter_data, patient_id, encounter_id)
        entries.append(BundleEntry(
            fullUrl=f"urn:uuid:encounter-{encounter_id}",
            resource=encounter
        ))
        
        # 5. OBSERVATIONS (Vitals, Flags, etc.)
        observations = self._create_observations(encounter_id, encounter_data)
        entries.extend(observations)

        # 6. CONDITIONS (Clinical findings / diagnoses)
        conditions = self._create_conditions(encounter_id, patient_id, encounter_data)
        entries.extend(conditions)
        
        # 7. MEDICATION REQUEST & DISPENSE
        medications = self._create_medication_resources(encounter_id)
        entries.extend(medications)
        
        # 8. DOCUMENT REFERENCES (Photos)
        documents = self._create_document_references(encounter_id)
        entries.extend(documents)
        
        bundle.entry = entries
        return bundle
    
    @staticmethod
    def _to_fhir_datetime(value) -> str:
        """
        Convert a SQL datetime string to a FHIR-valid dateTime.

        FHIR DateTime requires a timezone designator (Z or +/-hh:mm) whenever a
        time component is present. Bare dates (YYYY-MM-DD) are valid without one.
        """
        if not value:
            return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S+00:00')
        s = str(value).strip()
        # Normalise space separator to T  (MySQL DATETIME format)
        if len(s) >= 16 and s[10] == ' ':
            s = s[:10] + 'T' + s[11:]
        # If a time component is present and no timezone yet, assume UTC
        if 'T' in s and 'Z' not in s and '+' not in s[10:] and s.count('-') <= 2:
            s += '+00:00'
        return s

    def _create_composition(self, encounter_data: Dict, patient_id: int, encounter_id: int) -> Composition:
        """Create FHIR Composition resource."""
        composition = Composition(
            status="final",
            type=CodeableConcept(
                coding=[Coding(
                    system="http://loinc.org",
                    code="11506-3",
                    display="Progress note"
                )]
            ),
            subject=[Reference(reference=f"urn:uuid:patient-{patient_id}")],
            date=self._to_fhir_datetime(encounter_data.get('date_of_triage_visit')),
            author=[Reference(reference=f"urn:uuid:practitioner-{encounter_data.get('nurse_id', 'unknown')}")],
            title="Patient Encounter Record",
            section=[
                CompositionSection(
                    title="Encounter Details",
                    code=CodeableConcept(
                        coding=[Coding(
                            system="http://loinc.org",
                            code="46240-8",
                            display="History of Hospitalizations+Outpatient visits Narrative"
                        )]
                    )
                )
            ]
        )
        return composition
    
    def _create_patient(self, patient_data: Dict) -> Patient:
        """Create FHIR Patient resource."""
        patient = Patient(
            identifier=[Identifier(
                system="http://femr.org/patient-id",
                value=str(patient_data.get('id') or patient_data.get('legacy_id'))
            )]
        )
        
        # Name
        if patient_data.get('first_name') or patient_data.get('last_name'):
            patient.name = [HumanName(
                use="official",
                family=patient_data.get('last_name'),
                given=[patient_data.get('first_name')],
                text=f"{patient_data.get('first_name', '')} {patient_data.get('last_name', '')}".strip()
            )]
            
            if patient_data.get('middle_name'):
                patient.name[0].given.append(patient_data.get('middle_name'))
        
        # Gender
        sex = patient_data.get('sex_assigned_at_birth')
        if sex:
            gender_map = {'f': 'female', 'm': 'male', 'o': 'other'}
            patient.gender = gender_map.get(sex.lower(), 'unknown')
        
        # Contact
        telecom = []
        if patient_data.get('phone_number'):
            telecom.append(ContactPoint(
                system="phone",
                value=patient_data.get('phone_number'),
                use="home"
            ))
        
        if patient_data.get('email_address'):
            telecom.append(ContactPoint(
                system="email",
                value=patient_data.get('email_address')
            ))
        
        if telecom:
            patient.telecom = telecom
        
        # Address
        if any([patient_data.get('address1'), patient_data.get('city'), patient_data.get('zip_code')]):
            zip_raw = patient_data.get('zip_code')
            address = Address(
                use="home",
                line=[str(patient_data.get('address1'))] if patient_data.get('address1') else [],
                city=str(patient_data.get('city')) if patient_data.get('city') else None,
                postalCode=str(zip_raw) if zip_raw is not None else None
            )
            
            if patient_data.get('address2'):
                address.line.append(patient_data.get('address2'))
            
            patient.address = [address]
        
        # Active status
        patient.active = True
        
        return patient
    
    def _create_practitioners(self, encounter_data: Dict) -> List[BundleEntry]:
        """Create FHIR Practitioner resources."""
        entries = []
        practitioner_ids = []
        
        # Collect all practitioner IDs
        for role in ['nurse_id', 'doctor_id', 'pharmacist_id', 'user_id_diabetes_screen']:
            pract_id = encounter_data.get(role)
            if pract_id and pract_id not in practitioner_ids:
                practitioner_ids.append(pract_id)
        
        # Create practitioner resources
        for pract_id in practitioner_ids:
            user_data = self._find_user(pract_id)
            if user_data:
                practitioner = Practitioner(
                    identifier=[Identifier(
                        system="http://femr.org/practitioner-id",
                        value=str(pract_id)
                    )],
                    name=[HumanName(
                        family=user_data.get('lastName'),
                        given=[user_data.get('firstName')],
                        text=f"{user_data.get('firstName', '')} {user_data.get('lastName', '')}".strip()
                    )],
                    active=not user_data.get('deleted', False)
                )
                
                entries.append(BundleEntry(
                    fullUrl=f"urn:uuid:practitioner-{pract_id}",
                    resource=practitioner
                ))
        
        return entries
    
    def _create_encounter(self, encounter_data: Dict, patient_id: int, encounter_id: int) -> Encounter:
        """Create FHIR Encounter resource."""
        encounter = Encounter(
            status="finished",
            class_fhir=[CodeableConcept(
                coding=[Coding(
                    system="http://terminology.hl7.org/CodeSystem/v3-ActCode",
                    code="AMB",
                    display="ambulatory"
                )]
            )],
            subject=Reference(reference=f"urn:uuid:patient-{patient_id}"),
            identifier=[Identifier(
                system="http://femr.org/encounter-id",
                value=str(encounter_id)
            )]
        )
        
        # Participants
        participants = []
        if encounter_data.get('nurse_id'):
            participants.append(EncounterParticipant(
                type=[CodeableConcept(
                    coding=[Coding(
                        system="http://terminology.hl7.org/CodeSystem/v3-ParticipationType",
                        code="PPRF",
                        display="primary performer"
                    )]
                )],
                actor=Reference(reference=f"urn:uuid:practitioner-{encounter_data.get('nurse_id')}")
            ))
        
        if encounter_data.get('doctor_id'):
            participants.append(EncounterParticipant(
                type=[CodeableConcept(
                    coding=[Coding(
                        system="http://terminology.hl7.org/CodeSystem/v3-ParticipationType",
                        code="ATND",
                        display="attender"
                    )]
                )],
                actor=Reference(reference=f"urn:uuid:practitioner-{encounter_data.get('doctor_id')}")
            ))
        
        if participants:
            encounter.participant = participants
        
        return encounter
    
    def _create_observations(self, encounter_id: int, encounter_data: Dict) -> List[BundleEntry]:
        """Create FHIR Observation resources for vitals and flags."""
        entries = []
        
        # Find vitals for this encounter
        vitals = [v for v in self.data['vitals'] if v.get('patientEncounterId') == encounter_id]
        
        for vital in vitals:
            # Blood Pressure (combined systolic/diastolic)
            if vital.get('systolic_blood_pressure') and vital.get('diastolic_blood_pressure'):
                bp_obs = self._create_blood_pressure_observation(vital, encounter_id)
                entries.append(BundleEntry(
                    fullUrl=f"urn:uuid:observation-bp-{vital.get('id')}",
                    resource=bp_obs
                ))
            
            # Heart Rate
            if vital.get('heart_rate'):
                hr_obs = self._create_vital_observation(
                    vital, encounter_id, "heart_rate",
                    "8867-4", "Heart rate", "beats/minute"
                )
                entries.append(BundleEntry(
                    fullUrl=f"urn:uuid:observation-hr-{vital.get('id')}",
                    resource=hr_obs
                ))
            
            # Respiratory Rate
            if vital.get('respiratory_rate'):
                rr_obs = self._create_vital_observation(
                    vital, encounter_id, "respiratory_rate",
                    "9279-1", "Respiratory rate", "breaths/minute"
                )
                entries.append(BundleEntry(
                    fullUrl=f"urn:uuid:observation-rr-{vital.get('id')}",
                    resource=rr_obs
                ))
            
            # Body Temperature
            if vital.get('body_temperature'):
                temp_obs = self._create_vital_observation(
                    vital, encounter_id, "body_temperature",
                    "8310-5", "Body temperature", "Cel"
                )
                entries.append(BundleEntry(
                    fullUrl=f"urn:uuid:observation-temp-{vital.get('id')}",
                    resource=temp_obs
                ))
            
            # Oxygen Saturation
            if vital.get('oxygen_concentration'):
                o2_obs = self._create_vital_observation(
                    vital, encounter_id, "oxygen_concentration",
                    "2708-6", "Oxygen saturation", "%"
                )
                entries.append(BundleEntry(
                    fullUrl=f"urn:uuid:observation-o2-{vital.get('id')}",
                    resource=o2_obs
                ))
            
            # Glucose
            if vital.get('glucose_level'):
                glucose_obs = self._create_vital_observation(
                    vital, encounter_id, "glucose_level",
                    "2339-0", "Glucose", "mg/dL"
                )
                entries.append(BundleEntry(
                    fullUrl=f"urn:uuid:observation-glucose-{vital.get('id')}",
                    resource=glucose_obs
                ))
            
            # Height
            if vital.get('body_height_primary'):
                height_obs = self._create_vital_observation(
                    vital, encounter_id, "body_height_primary",
                    "8302-2", "Body height", "cm"
                )
                entries.append(BundleEntry(
                    fullUrl=f"urn:uuid:observation-height-{vital.get('id')}",
                    resource=height_obs
                ))
            
            # Weight
            if vital.get('body_weight'):
                weight_obs = self._create_vital_observation(
                    vital, encounter_id, "body_weight",
                    "29463-7", "Body weight", "kg"
                )
                entries.append(BundleEntry(
                    fullUrl=f"urn:uuid:observation-weight-{vital.get('id')}",
                    resource=weight_obs
                ))
        
        # Create flag observations from encounter data
        flags = []
        if encounter_data.get('smoking'):
            flags.append(('smoking', '72166-2', 'Tobacco smoking status'))
        if encounter_data.get('history_of_diabetes'):
            flags.append(('diabetes', '44877-9', 'History of diabetes'))
        if encounter_data.get('history_of_hypertension'):
            flags.append(('hypertension', '55284-4', 'History of hypertension'))
        if encounter_data.get('alcohol'):
            flags.append(('alcohol', '74013-4', 'Alcohol use'))
        if encounter_data.get('history_of_high_cholesterol'):
            flags.append(('cholesterol', '55283-6', 'History of high cholesterol'))
        
        for flag_name, loinc_code, display in flags:
            flag_obs = Observation(
                status="final",
                category=[CodeableConcept(
                    coding=[Coding(
                        system="http://terminology.hl7.org/CodeSystem/observation-category",
                        code="social-history",
                        display="Social History"
                    )]
                )],
                code=CodeableConcept(
                    coding=[Coding(
                        system="http://loinc.org",
                        code=loinc_code,
                        display=display
                    )]
                ),
                valueBoolean=True
            )
            entries.append(BundleEntry(
                fullUrl=f"urn:uuid:observation-flag-{flag_name}-{encounter_id}",
                resource=flag_obs
            ))
        
        # Weeks Pregnant
        if encounter_data.get('weeks_pregnant'):
            preg_obs = Observation(
                status="final",
                category=[CodeableConcept(
                    coding=[Coding(
                        system="http://terminology.hl7.org/CodeSystem/observation-category",
                        code="exam",
                        display="Exam"
                    )]
                )],
                code=CodeableConcept(
                    coding=[Coding(
                        system="http://loinc.org",
                        code="49051-6",
                        display="Gestational age in weeks"
                    )]
                ),
                valueQuantity=Quantity(
                    value=float(encounter_data.get('weeks_pregnant')),
                    unit="weeks",
                    system="http://unitsofmeasure.org",
                    code="wk"
                )
            )
            entries.append(BundleEntry(
                fullUrl=f"urn:uuid:observation-pregnancy-{encounter_id}",
                resource=preg_obs
            ))
        
        return entries
    
    def _create_blood_pressure_observation(self, vital: Dict, encounter_id: int) -> Observation:
        """Create blood pressure observation with systolic/diastolic components."""
        return Observation(
            status="final",
            category=[CodeableConcept(
                coding=[Coding(
                    system="http://terminology.hl7.org/CodeSystem/observation-category",
                    code="vital-signs",
                    display="Vital Signs"
                )]
            )],
            code=CodeableConcept(
                coding=[Coding(
                    system="http://loinc.org",
                    code="85354-9",
                    display="Blood pressure panel"
                )]
            ),
            component=[
                ObservationComponent(
                    code=CodeableConcept(
                        coding=[Coding(
                            system="http://loinc.org",
                            code="8480-6",
                            display="Systolic blood pressure"
                        )]
                    ),
                    valueQuantity=Quantity(
                        value=float(vital.get('systolic_blood_pressure')),
                        unit="mmHg",
                        system="http://unitsofmeasure.org",
                        code="mm[Hg]"
                    )
                ),
                ObservationComponent(
                    code=CodeableConcept(
                        coding=[Coding(
                            system="http://loinc.org",
                            code="8462-4",
                            display="Diastolic blood pressure"
                        )]
                    ),
                    valueQuantity=Quantity(
                        value=float(vital.get('diastolic_blood_pressure')),
                        unit="mmHg",
                        system="http://unitsofmeasure.org",
                        code="mm[Hg]"
                    )
                )
            ]
        )
    
    def _create_vital_observation(self, vital: Dict, encounter_id: int,
                                   field_name: str, loinc_code: str,
                                   display: str, unit: str) -> Observation:
        """Create a simple vital sign observation."""
        value = vital.get(field_name)
        
        return Observation(
            status="final",
            category=[CodeableConcept(
                coding=[Coding(
                    system="http://terminology.hl7.org/CodeSystem/observation-category",
                    code="vital-signs",
                    display="Vital Signs"
                )]
            )],
            code=CodeableConcept(
                coding=[Coding(
                    system="http://loinc.org",
                    code=loinc_code,
                    display=display
                )]
            ),
            valueQuantity=Quantity(
                value=float(value),
                unit=unit,
                system="http://unitsofmeasure.org"
            )
        )

    def _create_conditions(self, encounter_id: int, patient_id: int, encounter_data: Dict) -> List[BundleEntry]:
        """Create Condition resources for encounter clinical findings/diagnoses."""
        entries: List[BundleEntry] = []

        diagnosis_data = self._find_diagnoses_for_encounter(encounter_id, patient_id)
        for index, diagnosis in enumerate(diagnosis_data, start=1):
            diagnosis_id = diagnosis.get('legacy_id') or diagnosis.get('id') or f"enc-{encounter_id}-{index}"

            diagnosis_text = (
                diagnosis.get('text')
                or diagnosis.get('diagnosis_text')
                or diagnosis.get('description')
                or diagnosis.get('name')
                or diagnosis.get('value')
            )

            coding = None
            if diagnosis.get('code'):
                coding = Coding(
                    system=diagnosis.get('system') or diagnosis.get('coding_system') or "http://hl7.org/fhir/sid/icd-10",
                    code=str(diagnosis.get('code')),
                    display=diagnosis_text
                )

            condition = Condition(
                clinicalStatus=CodeableConcept(
                    coding=[Coding(
                        system="http://terminology.hl7.org/CodeSystem/condition-clinical",
                        code="active",
                        display="Active"
                    )]
                ),
                verificationStatus=CodeableConcept(
                    coding=[Coding(
                        system="http://terminology.hl7.org/CodeSystem/condition-ver-status",
                        code="confirmed",
                        display="Confirmed"
                    )]
                ),
                category=[CodeableConcept(
                    coding=[Coding(
                        system="http://terminology.hl7.org/CodeSystem/condition-category",
                        code="encounter-diagnosis",
                        display="Encounter Diagnosis"
                    )]
                )],
                code=CodeableConcept(
                    coding=[coding] if coding else None,
                    text=diagnosis_text or "Clinical finding"
                ),
                subject=Reference(reference=f"urn:uuid:patient-{patient_id}"),
                encounter=Reference(reference=f"urn:uuid:encounter-{encounter_id}"),
                recordedDate=self._to_fhir_datetime(
                    encounter_data.get('date_of_medical_visit')
                    or encounter_data.get('date_of_triage_visit')
                    or encounter_data.get('timestamp')
                )
            )

            entries.append(BundleEntry(
                fullUrl=f"urn:uuid:condition-{diagnosis_id}-{encounter_id}",
                resource=condition
            ))

        return entries

    def _find_diagnoses_for_encounter(self, encounter_id: int, patient_id: int) -> List[Dict]:
        """Resolve diagnosis rows relevant to this encounter.

        Supports both direct diagnosis rows (with text) and join-style rows
        (encounter/patient/diagnosis_id references).
        """
        diagnosis_rows = self.data.get('diagnoses', [])
        if not diagnosis_rows:
            return []

        encounter_keys = {'patientEncounterId', 'patient_encounter_id', 'encounter_id', 'patientencounter_id'}
        patient_keys = {'patient_id', 'patientId'}
        diagnosis_ref_keys = {'diagnosis_id', 'diagnosisId'}
        text_keys = {'text', 'diagnosis_text', 'description', 'name', 'value'}

        def _row_value(row: Dict, candidate_keys: set):
            for key in candidate_keys:
                if key in row and row.get(key) is not None:
                    return row.get(key)
            return None

        diagnosis_lookup_by_id: Dict[str, Dict] = {}
        for row in diagnosis_rows:
            row_id = row.get('id') or row.get('legacy_id')
            if row_id is not None and any(k in row and row.get(k) for k in text_keys):
                diagnosis_lookup_by_id[str(row_id)] = row

        resolved: List[Dict] = []
        has_explicit_links = False

        for row in diagnosis_rows:
            row_encounter = _row_value(row, encounter_keys)
            row_patient = _row_value(row, patient_keys)
            row_diagnosis_id = _row_value(row, diagnosis_ref_keys)

            if row_encounter is None and row_patient is None and row_diagnosis_id is None:
                continue

            has_explicit_links = True

            encounter_match = row_encounter is not None and str(row_encounter) == str(encounter_id)
            patient_match = row_patient is not None and str(row_patient) == str(patient_id)
            if not (encounter_match or patient_match):
                continue

            if row_diagnosis_id is not None:
                resolved_row = diagnosis_lookup_by_id.get(str(row_diagnosis_id))
                if resolved_row:
                    merged = dict(resolved_row)
                    merged.update(row)
                    resolved.append(merged)
                else:
                    resolved.append(row)
            else:
                resolved.append(row)

        if not has_explicit_links:
            fallback = [
                row for row in diagnosis_rows
                if any(k in row and row.get(k) for k in text_keys)
            ]
            return fallback

        deduped: List[Dict] = []
        seen = set()
        for row in resolved:
            key = (
                str(row.get('legacy_id') or row.get('id') or ''),
                str(row.get('diagnosis_id') or row.get('diagnosisId') or ''),
                str(row.get('text') or row.get('diagnosis_text') or row.get('description') or row.get('name') or row.get('value') or '')
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)

        return deduped
    
    def _create_medication_resources(self, encounter_id: int) -> List[BundleEntry]:
        """Create MedicationRequest and MedicationDispense resources."""
        entries = []
        
        prescriptions = [p for p in self.data['prescriptions']
                        if p.get('patientEncounterId') == encounter_id]
        
        for rx in prescriptions:
            rx_id = rx.get('id') or rx.get('legacy_id')
            
            med_name = rx.get('medication') or 'Unknown medication'
            med_request = MedicationRequest(
                status="completed",
                intent="order",
                medication=CodeableReference(
                    concept=CodeableConcept(text=med_name)
                ),
                subject=Reference(reference=f"urn:uuid:encounter-{encounter_id}"),
                authoredOn=self._to_fhir_datetime(rx.get('dateTaken')) if rx.get('dateTaken') else None,
                dosageInstruction=[{
                    "text": rx.get('specialInstructions', '')
                }] if rx.get('specialInstructions') else None
            )

            entries.append(BundleEntry(
                fullUrl=f"urn:uuid:medication-request-{rx_id}",
                resource=med_request
            ))

            if rx.get('dateDispensed'):
                med_dispense = MedicationDispense(
                    status="completed",
                    medication=CodeableReference(
                        concept=CodeableConcept(text=med_name)
                    ),
                    subject=Reference(reference=f"urn:uuid:encounter-{encounter_id}"),
                    whenHandedOver=self._to_fhir_datetime(rx.get('dateDispensed'))
                )
                
                entries.append(BundleEntry(
                    fullUrl=f"urn:uuid:medication-dispense-{rx_id}",
                    resource=med_dispense
                ))
        
        return entries
    
    def _create_document_references(self, encounter_id: int) -> List[BundleEntry]:
        """Create DocumentReference resources for photos."""
        entries = []
        
        photos = [p for p in self.data['photos']
                 if p.get('encounter_id') == encounter_id or p.get('patientEncounterId') == encounter_id]
        
        for photo in photos:
            photo_id = photo.get('id') or photo.get('legacy_id')
            
            doc_ref = DocumentReference(
                status="current",
                content=[DocumentReferenceContent(
                    attachment=Attachment(
                        contentType="image/jpeg",
                        url=photo.get('imaging_link') or photo.get('file_path'),
                        title=photo.get('description', 'Patient photo')
                    )
                )]
            )
            
            entries.append(BundleEntry(
                fullUrl=f"urn:uuid:document-{photo_id}",
                resource=doc_ref
            ))
        
        return entries
    
    def _find_patient(self, patient_id: int) -> Optional[Dict]:
        """Find patient data by ID."""
        for patient in self.data['patients']:
            if patient.get('id') == patient_id or patient.get('legacy_id') == patient_id:
                return patient
        return None
    
    def _find_user(self, user_id: int) -> Optional[Dict]:
        """Find user data by ID."""
        for user in self.data['users']:
            if user.get('id') == user_id or user.get('legacy_id') == user_id:
                return user
        return None


class RDSManager:
    """Manage RDS database connections and operations with differential updates."""
    
    def __init__(self):
        self.connection = None
        self.kit_id = None
        self.processed_records = {
            'patients': set(),
            'encounters': set(),
            'vitals': set(),
            'prescriptions': set(),
            'users': set(),
            'mission_trips': set()
        }
        self.mission_trip_id_map = {}  # Map source mission_trip_id to DB id
    
    def connect(self):
        """Establish connection to RDS."""
        try:
            self.connection = pymysql.connect(
                host=RDS_HOST,
                port=int(RDS_PORT),
                db=RDS_DB_NAME,
                user=RDS_USERNAME,
                password=RDS_PASSWORD,
                cursorclass=pymysql.cursors.DictCursor
            )
            self.connection.autocommit = False  # Manual transaction control
            logger.info("Successfully connected to RDS")
        except Exception as e:
            logger.error(f"Failed to connect to RDS: {e}")
            raise
    
    def close(self):
        """Close database connection."""
        if self.connection:
            self.connection.close()
            logger.info("Closed RDS connection")
    
    def set_kit_id(self, kit_id: str):
        """Set the kit ID for tracking which kit this data came from."""
        self.kit_id = kit_id
    
    def store_fhir_bundle(self, bundle: Bundle, parsed_data: Dict):
        """
        Store FHIR bundle in database with differential logic.
        Only inserts/updates records that are new or changed.
        Uses legacy_id to prevent duplicates.
        """
        if not self.connection:
            raise Exception("No database connection")
        
        try:
            cursor = self.connection.cursor()
            
            # Extract resources from bundle
            patient_resource = None
            encounter_resource = None
            practitioner_resources = []
            observation_resources = []
            medication_resources = []
            document_resources = []
            
            for entry in bundle.entry:
                resource_type = getattr(entry.resource, '__resource_type__', entry.resource.__class__.__name__)
                
                if resource_type == "Patient":
                    patient_resource = entry.resource
                elif resource_type == "Encounter":
                    encounter_resource = entry.resource
                elif resource_type == "Practitioner":
                    practitioner_resources.append(entry.resource)
                elif resource_type == "Observation":
                    observation_resources.append(entry.resource)
                elif resource_type in ["MedicationRequest", "MedicationDispense"]:
                    medication_resources.append(entry.resource)
                elif resource_type == "DocumentReference":
                    document_resources.append(entry.resource)
            
            # Get legacy IDs from identifiers
            patient_legacy_id = self._extract_legacy_id(patient_resource)
            encounter_legacy_id = self._extract_legacy_id(encounter_resource)
            
            logger.info(f"Processing bundle: Patient {patient_legacy_id}, Encounter {encounter_legacy_id}")
            
            # 0. UPSERT MISSION TRIPS (before encounters since encounters FK to mission_trips)
            for mission_trip in parsed_data.get('mission_trips', []):
                mission_trip_legacy_id = mission_trip.get('id') or mission_trip.get('legacy_id')
                if mission_trip_legacy_id and mission_trip_legacy_id not in self.processed_records['mission_trips']:
                    mt_db_id = self._upsert_mission_trip(cursor, mission_trip, mission_trip_legacy_id)
                    self.mission_trip_id_map[mission_trip_legacy_id] = mt_db_id
                    self.processed_records['mission_trips'].add(mission_trip_legacy_id)
                    logger.info(f"Processed mission trip {mission_trip_legacy_id} -> DB ID {mt_db_id}")
            
            # 1. UPSERT PATIENT
            if patient_legacy_id and patient_legacy_id not in self.processed_records['patients']:
                patient_db_id = self._upsert_patient(cursor, patient_resource, patient_legacy_id, parsed_data)
                self.processed_records['patients'].add(patient_legacy_id)
                logger.info(f"Processed patient {patient_legacy_id} -> DB ID {patient_db_id}")
            else:
                patient_db_id = self._get_patient_db_id(cursor, patient_legacy_id)
            
            # 2. UPSERT PRACTITIONERS
            practitioner_db_ids = {}
            for pract in practitioner_resources:
                pract_legacy_id = self._extract_legacy_id(pract)
                if pract_legacy_id and pract_legacy_id not in self.processed_records['users']:
                    pract_db_id = self._upsert_practitioner(cursor, pract, pract_legacy_id, parsed_data)
                    practitioner_db_ids[pract_legacy_id] = pract_db_id
                    self.processed_records['users'].add(pract_legacy_id)
                    logger.info(f"Processed practitioner {pract_legacy_id} -> DB ID {pract_db_id}")
                else:
                    practitioner_db_ids[pract_legacy_id] = self._get_user_db_id(cursor, pract_legacy_id)
            
            # 3. UPSERT ENCOUNTER (uses mission_trip_id_map populated above)
            if encounter_legacy_id and encounter_legacy_id not in self.processed_records['encounters']:
                encounter_db_id = self._upsert_encounter(
                    cursor, encounter_resource, encounter_legacy_id,
                    patient_db_id, practitioner_db_ids, parsed_data
                )
                self.processed_records['encounters'].add(encounter_legacy_id)
                logger.info(f"Processed encounter {encounter_legacy_id} -> DB ID {encounter_db_id}")
            else:
                encounter_db_id = self._get_encounter_db_id(cursor, encounter_legacy_id)
            
            # 4. UPSERT OBSERVATIONS
            for obs in observation_resources:
                obs_id = self._upsert_observation(cursor, obs, encounter_db_id, parsed_data)
                logger.info(f"Processed observation -> DB ID {obs_id}")
            
            # 5. UPSERT MEDICATIONS
            for med in medication_resources:
                med_id = self._upsert_medication(cursor, med, encounter_db_id, practitioner_db_ids, parsed_data)
                if med_id:
                    logger.info(f"Processed medication -> DB ID {med_id}")
            
            # 6. UPSERT PHOTOS
            for doc in document_resources:
                doc_id = self._upsert_document(cursor, doc, encounter_db_id, parsed_data)
                if doc_id:
                    logger.info(f"Processed document -> DB ID {doc_id}")
            
            # Commit transaction
            self.connection.commit()
            cursor.close()
            
            logger.info(f"Successfully stored bundle for encounter {encounter_legacy_id}")
            return encounter_db_id
            
        except Exception as e:
            logger.error(f"Error storing FHIR bundle: {e}", exc_info=True)
            self.connection.rollback()
            raise
    
    def _extract_legacy_id(self, resource) -> Optional[int]:
        """Extract legacy_id from FHIR resource identifier."""
        if not resource or not hasattr(resource, 'identifier') or not resource.identifier:
            return None
        
        for identifier in resource.identifier:
            if identifier.system and 'femr.org' in identifier.system:
                try:
                    return int(identifier.value)
                except (ValueError, TypeError):
                    pass
        
        return None
    
    def _upsert_mission_trip(self, cursor, mission_trip: Dict, legacy_id: int) -> int:
        """Insert or update mission trip record. Handles null FK dependencies gracefully."""
        cursor.execute("SELECT id FROM central_api_missiontrip WHERE legacy_id = %s", (legacy_id,))
        existing = cursor.fetchone()
        
        # Extract mission trip fields
        state_date = mission_trip.get('state_date') or mission_trip.get('start_date') or datetime.now().date()
        end_date = mission_trip.get('end_date') or state_date
        
        # For mission_team_id and mission_city_id, try to get from data or use None (will be nullable)
        # In a production system, you'd want to create defaults or handle these relationships carefully
        mission_team_id = None  # Can be set from mission_trip data if available
        mission_city_id = None  # Can be set from mission_trip data if available
        
        if existing:
            cursor.execute("""
                UPDATE central_api_missiontrip 
                SET state_date = %s, end_date = %s
                WHERE legacy_id = %s
            """, (state_date, end_date, legacy_id))
            logger.info(f"Updated existing mission trip {legacy_id}")
            return existing['id']
        else:
            # Note: mission_team_id and mission_city_id may be required by schema
            # If they are, we need to either create defaults or skip mission trip creation
            try:
                cursor.execute("""
                    INSERT INTO central_api_missiontrip (
                        legacy_id, state_date, end_date, mission_team_id, mission_city_id
                    ) VALUES (%s, %s, %s, %s, %s)
                """, (legacy_id, state_date, end_date, mission_team_id, mission_city_id))
                logger.info(f"Inserted new mission trip {legacy_id}")
                return cursor.lastrowid
            except Exception as e:
                logger.warning(f"Failed to insert mission trip {legacy_id}: {e}. "
                               f"Continuing without mission trip link. Mission team/city may be required.")
                return None
    
    def _upsert_patient(self, cursor, patient: Patient, legacy_id: int, parsed_data: Dict) -> int:
        """Insert or update patient record."""
        original_patient = None
        for p in parsed_data.get('patients', []):
            if p.get('id') == legacy_id or p.get('legacy_id') == legacy_id:
                original_patient = p
                break
        
        cursor.execute("SELECT id FROM central_api_patient WHERE legacy_id = %s", (legacy_id,))
        existing = cursor.fetchone()
        
        first_name = patient.name[0].given[0] if patient.name and patient.name[0].given else None
        middle_name = patient.name[0].given[1] if patient.name and len(patient.name[0].given) > 1 else None
        last_name = patient.name[0].family if patient.name else None
        phone = patient.telecom[0].value if patient.telecom else None
        email = next((t.value for t in patient.telecom if t.system == "email"), None) if patient.telecom else None
        address1 = patient.address[0].line[0] if patient.address and patient.address[0].line else None
        address2 = patient.address[0].line[1] if patient.address and len(patient.address[0].line) > 1 else None
        city = patient.address[0].city if patient.address else None
        zip_code = patient.address[0].postalCode if patient.address else None
        gender_map = {'male': 'm', 'female': 'f', 'other': 'o'}
        sex = gender_map.get(patient.gender, None) if patient.gender else None
        age = original_patient.get('age') if original_patient else None
        
        if existing:
            cursor.execute("""
                UPDATE central_api_patient 
                SET first_name = %s, middle_name = %s, last_name = %s,
                    phone_number = %s, email_address = %s,
                    address1 = %s, address2 = %s, city = %s, zip_code = %s,
                    sex_assigned_at_birth = %s, age = %s, timestamp = NOW()
                WHERE legacy_id = %s
            """, (first_name, middle_name, last_name, phone, email,
                  address1, address2, city, zip_code, sex, age, legacy_id))
            logger.info(f"Updated existing patient {legacy_id}")
            cursor.execute("SELECT id FROM central_api_patient WHERE legacy_id = %s", (legacy_id,))
            return cursor.fetchone()['id']
        else:
            cursor.execute("""
                INSERT INTO central_api_patient (
                    legacy_id, first_name, middle_name, last_name,
                    phone_number, email_address,
                    address1, address2, city, zip_code,
                    sex_assigned_at_birth, age, timestamp
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """, (legacy_id, first_name, middle_name, last_name, phone, email,
                  address1, address2, city, zip_code, sex, age))
            logger.info(f"Inserted new patient {legacy_id}")
            return cursor.lastrowid
    
    def _get_patient_db_id(self, cursor, legacy_id: int) -> Optional[int]:
        cursor.execute("SELECT id FROM central_api_patient WHERE legacy_id = %s", (legacy_id,))
        result = cursor.fetchone()
        return result['id'] if result else None
    
    def _upsert_practitioner(self, cursor, practitioner: Practitioner, legacy_id: int, parsed_data: Dict) -> int:
        original_user = None
        for u in parsed_data.get('users', []):
            if u.get('id') == legacy_id or u.get('legacy_id') == legacy_id:
                original_user = u
                break
        
        cursor.execute("SELECT id FROM central_api_user WHERE legacy_id = %s", (legacy_id,))
        existing = cursor.fetchone()
        
        first_name = practitioner.name[0].given[0] if practitioner.name and practitioner.name[0].given else 'Unknown'
        last_name = practitioner.name[0].family if practitioner.name else 'Unknown'
        email = original_user.get('email', f'user{legacy_id}@femr.org') if original_user else f'user{legacy_id}@femr.org'
        
        if existing:
            cursor.execute("""
                UPDATE central_api_user 
                SET firstName = %s, lastName = %s, email = %s, deleted = %s
                WHERE legacy_id = %s
            """, (first_name, last_name, email, not practitioner.active, legacy_id))
            logger.info(f"Updated existing user {legacy_id}")
            return existing['id']
        else:
            password = original_user.get('password', 'migrated_user') if original_user else 'migrated_user'
            last_login = original_user.get('lastLogin', datetime.now()) if original_user else datetime.now()
            cursor.execute("""
                INSERT INTO central_api_user (
                    legacy_id, firstName, lastName, email, password,
                    lastLogin, deleted, passwordReset, notes,
                    passwordCreatedDate, dateCreated, createdBy
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (legacy_id, first_name, last_name, email, password,
                  last_login, not practitioner.active, False, 'Migrated from field kit',
                  datetime.now(), datetime.now(), 1))
            logger.info(f"Inserted new user {legacy_id}")
            return cursor.lastrowid
    
    def _get_user_db_id(self, cursor, legacy_id: int) -> Optional[int]:
        cursor.execute("SELECT id FROM central_api_user WHERE legacy_id = %s", (legacy_id,))
        result = cursor.fetchone()
        return result['id'] if result else None
    
    def _upsert_encounter(self, cursor, encounter: Encounter, legacy_id: int,
                         patient_db_id: int, practitioner_ids: Dict, parsed_data: Dict) -> int:
        original_encounter = None
        for e in parsed_data.get('encounters', []):
            if e.get('id') == legacy_id or e.get('legacy_id') == legacy_id:
                original_encounter = e
                break
        
        cursor.execute("SELECT id FROM central_api_patientencounter WHERE legacy_id = %s", (legacy_id,))
        existing = cursor.fetchone()
        
        nurse_id = practitioner_ids.get(original_encounter.get('nurse_id')) if original_encounter else None
        doctor_id = practitioner_ids.get(original_encounter.get('doctor_id')) if original_encounter else None
        pharmacist_id = practitioner_ids.get(original_encounter.get('pharmacist_id')) if original_encounter else None
        triage_date = original_encounter.get('date_of_triage_visit') if original_encounter else datetime.now()
        smoking = original_encounter.get('smoking', False) if original_encounter else False
        diabetes = original_encounter.get('history_of_diabetes', False) if original_encounter else False
        hypertension = original_encounter.get('history_of_hypertension', False) if original_encounter else False
        high_cholesterol = original_encounter.get('history_of_high_cholesterol', False) if original_encounter else False
        alcohol = original_encounter.get('alcohol', False) if original_encounter else False
        weeks_pregnant = original_encounter.get('weeks_pregnant') if original_encounter else None
        
        # Map mission_trip_id from source data to DB id
        mission_trip_id_from_source = original_encounter.get('mission_trip_id') if original_encounter else None
        mission_trip_db_id = self.mission_trip_id_map.get(mission_trip_id_from_source) if mission_trip_id_from_source else None
        
        if existing:
            cursor.execute("""
                UPDATE central_api_patientencounter 
                SET patient_id = %s, nurse_id = %s, doctor_id = %s, pharmacist_id = %s,
                    date_of_triage_visit = %s, timestamp = NOW(),
                    smoking = %s, history_of_diabetes = %s, history_of_hypertension = %s,
                    history_of_high_cholesterol = %s, alcohol = %s, weeks_pregnant = %s, mission_trip_id = %s
                WHERE legacy_id = %s
            """, (patient_db_id, nurse_id, doctor_id, pharmacist_id,
                  triage_date, smoking, diabetes, hypertension,
                  high_cholesterol, alcohol, weeks_pregnant, mission_trip_db_id, legacy_id))
            logger.info(f"Updated existing encounter {legacy_id}")
            return existing['id']
        else:
            cursor.execute("""
                INSERT INTO central_api_patientencounter (
                    legacy_id, patient_id, nurse_id, doctor_id, pharmacist_id,
                    date_of_triage_visit, timestamp, active,
                    smoking, history_of_diabetes, history_of_hypertension,
                    history_of_high_cholesterol, alcohol, weeks_pregnant, mission_trip_id
                ) VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s)
            """, (legacy_id, patient_db_id, nurse_id, doctor_id, pharmacist_id,
                  triage_date, True, smoking, diabetes, hypertension,
                  high_cholesterol, alcohol, weeks_pregnant, mission_trip_db_id))
            logger.info(f"Inserted new encounter {legacy_id}")
            return cursor.lastrowid
    
    def _get_encounter_db_id(self, cursor, legacy_id: int) -> Optional[int]:
        cursor.execute("SELECT id FROM central_api_patientencounter WHERE legacy_id = %s", (legacy_id,))
        result = cursor.fetchone()
        return result['id'] if result else None
    
    def _upsert_observation(self, cursor, observation: Observation,
                           encounter_db_id: int, parsed_data: Dict) -> Optional[int]:
        original_vitals = [v for v in parsed_data.get('vitals', [])
                          if v.get('patientEncounterId') == encounter_db_id]
        
        if not original_vitals:
            logger.warning(f"No original vital data found for encounter {encounter_db_id}")
            return None
        
        original_vital = original_vitals[0]
        vital_legacy_id = original_vital.get('id') or original_vital.get('legacy_id')
        
        if not vital_legacy_id:
            return None
        
        cursor.execute("SELECT id FROM central_api_patientencountervital WHERE legacy_id = %s", (vital_legacy_id,))
        existing = cursor.fetchone()
        
        systolic_bp = original_vital.get('systolic_blood_pressure')
        diastolic_bp = original_vital.get('diastolic_blood_pressure')
        heart_rate = original_vital.get('heart_rate')
        respiratory_rate = original_vital.get('respiratory_rate')
        body_temp = original_vital.get('body_temperature')
        oxygen = original_vital.get('oxygen_concentration')
        glucose = original_vital.get('glucose_level')
        height = original_vital.get('body_height_primary')
        weight = original_vital.get('body_weight')
        date_taken = original_vital.get('dateTaken', datetime.now())
        user_id = original_vital.get('userId', '1')
        
        if existing:
            cursor.execute("""
                UPDATE central_api_patientencountervital 
                SET patientEncounterId = %s, userId = %s, dateTaken = %s,
                    diastolic_blood_pressure = %s, systolic_blood_pressure = %s,
                    heart_rate = %s, respiratory_rate = %s, body_temperature = %s,
                    oxygen_concentration = %s, glucose_level = %s,
                    body_height_primary = %s, body_weight = %s
                WHERE legacy_id = %s
            """, (encounter_db_id, user_id, date_taken,
                  diastolic_bp, systolic_bp, heart_rate, respiratory_rate, body_temp,
                  oxygen, glucose, height, weight, vital_legacy_id))
            logger.info(f"Updated existing vital {vital_legacy_id}")
            return existing['id']
        else:
            cursor.execute("""
                INSERT INTO central_api_patientencountervital (
                    legacy_id, patientEncounterId, userId, dateTaken,
                    diastolic_blood_pressure, systolic_blood_pressure,
                    heart_rate, respiratory_rate, body_temperature,
                    oxygen_concentration, glucose_level,
                    body_height_primary, body_weight
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (vital_legacy_id, encounter_db_id, user_id, date_taken,
                  diastolic_bp, systolic_bp, heart_rate, respiratory_rate, body_temp,
                  oxygen, glucose, height, weight))
            logger.info(f"Inserted new vital {vital_legacy_id}")
            return cursor.lastrowid
    
    def _upsert_medication(self, cursor, medication, encounter_db_id: int,
                          practitioner_ids: Dict, parsed_data: Dict) -> Optional[int]:
        if getattr(medication, '__resource_type__', medication.__class__.__name__) != "MedicationRequest":
            return None
        
        original_rx = None
        for rx in parsed_data.get('prescriptions', []):
            if rx.get('patientEncounterId') == encounter_db_id:
                original_rx = rx
                break
        
        if not original_rx:
            return None
        
        rx_legacy_id = original_rx.get('id') or original_rx.get('legacy_id')
        if not rx_legacy_id:
            return None
        
        cursor.execute("SELECT id FROM central_api_patientprescriptions WHERE legacy_id = %s", (rx_legacy_id,))
        existing = cursor.fetchone()
        
        medication_id = original_rx.get('medication_id')
        physician_id = practitioner_ids.get(original_rx.get('physician_id'))
        amount = original_rx.get('amount')
        date_taken = original_rx.get('dateTaken', datetime.now())
        special_instructions = original_rx.get('specialInstructions')
        is_counseled = original_rx.get('isCounseled', False)
        date_dispensed = original_rx.get('dateDispensed')
        
        if existing:
            cursor.execute("""
                UPDATE central_api_patientprescriptions 
                SET patientEncounterId = %s, physician_id = %s, amount = %s,
                    dateTaken = %s, specialInstructions = %s, isCounseled = %s,
                    dateDispensed = %s
                WHERE legacy_id = %s
            """, (encounter_db_id, physician_id, amount, date_taken,
                  special_instructions, is_counseled, date_dispensed, rx_legacy_id))
            logger.info(f"Updated existing prescription {rx_legacy_id}")
            return existing['id']
        else:
            if not medication_id:
                logger.warning("Skipping prescription - no medication_id")
                return None
            cursor.execute("""
                INSERT INTO central_api_patientprescriptions (
                    legacy_id, patientEncounterId, medication_id, physician_id,
                    amount, dateTaken, specialInstructions, isCounseled, dateDispensed
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (rx_legacy_id, encounter_db_id, medication_id, physician_id,
                  amount, date_taken, special_instructions, is_counseled, date_dispensed))
            logger.info(f"Inserted new prescription {rx_legacy_id}")
            return cursor.lastrowid
    
    def _upsert_document(self, cursor, document: DocumentReference,
                        encounter_db_id: int, parsed_data: Dict) -> Optional[int]:
        original_photo = None
        for photo in parsed_data.get('photos', []):
            original_photo = photo
            break
        
        if not original_photo:
            return None
        
        photo_legacy_id = original_photo.get('id') or original_photo.get('legacy_id')
        if not photo_legacy_id:
            return None
        
        cursor.execute("SELECT id FROM central_api_photo WHERE legacy_id = %s", (photo_legacy_id,))
        existing = cursor.fetchone()
        
        description = original_photo.get('description', '')
        file_path = original_photo.get('file_path', '')
        imaging_link = original_photo.get('imaging_link', '')
        insert_ts = original_photo.get('insertTS', datetime.now().date())
        
        if existing:
            cursor.execute("""
                UPDATE central_api_photo 
                SET description = %s, file_path = %s, imaging_link = %s, insertTS = %s
                WHERE legacy_id = %s
            """, (description, file_path, imaging_link, insert_ts, photo_legacy_id))
            logger.info(f"Updated existing photo {photo_legacy_id}")
            return existing['id']
        else:
            cursor.execute("""
                INSERT INTO central_api_photo (
                    legacy_id, description, file_path, imaging_link, insertTS
                ) VALUES (%s, %s, %s, %s, %s)
            """, (photo_legacy_id, description, file_path, imaging_link, insert_ts))
            logger.info(f"Inserted new photo {photo_legacy_id}")
            return cursor.lastrowid


def decrypt_file_if_encrypted(bucket: str, key: str, file_content: bytes, s3_object_response: Dict) -> bytes:
    """
    Decrypt file if it was encrypted with KMS envelope encryption.
    
    Encrypted files have .encrypted extension and store the encrypted data key in S3 metadata.
    Uses KMS to decrypt the data key, then AES-256 to decrypt the file.
    
    Args:
        bucket: S3 bucket name
        key: S3 object key
        file_content: Raw file bytes from S3
        s3_object_response: Full S3 get_object response
    
    Returns:
        Decrypted (and decompressed if .gz) file content
    """
    if not key.endswith('.encrypted'):
        # File is not encrypted, return as-is
        return file_content
    
    logger.info(f"File is encrypted (.encrypted extension detected): {key}")
    
    try:
        # Step 1: Extract encrypted data key from S3 user metadata
        metadata = s3_object_response.get('Metadata', {})
        encrypted_data_key_b64 = metadata.get('x-amz-encrypted-data-key')
        
        if not encrypted_data_key_b64:
            logger.error("Missing x-amz-encrypted-data-key in S3 object metadata")
            raise ValueError("File is marked as encrypted but encrypted data key not found in metadata")
        
        encrypted_data_key = base64.b64decode(encrypted_data_key_b64)
        logger.info(f"Retrieved encrypted data key ({len(encrypted_data_key)} bytes)")
        
        # Step 2: Use KMS to decrypt the data key
        logger.info("Requesting KMS to decrypt data key...")
        decrypt_response = kms.decrypt(CiphertextBlob=encrypted_data_key)
        plaintext_data_key = decrypt_response['Plaintext']
        logger.info(f"KMS decrypted data key ({len(plaintext_data_key)} bytes)")
        
        # Step 3: Decrypt file content using AES-256-ECB
        logger.info(f"Decrypting file content ({len(file_content)} bytes)...")
        aes = pyaes.AESModeOfOperationECB(plaintext_data_key)
        decrypted_blocks = []
        for i in range(0, len(file_content), 16):
            block = file_content[i:i + 16]
            if len(block) == 16:
                decrypted_blocks.append(aes.decrypt(block))
        decrypted_content = b"".join(decrypted_blocks)

        # Remove PKCS7 padding used by Java AES encryption
        if decrypted_content:
            pad_len = decrypted_content[-1]
            if 1 <= pad_len <= 16:
                decrypted_content = decrypted_content[:-pad_len]

        logger.info(f"Decrypted to {len(decrypted_content)} bytes")
        
        # Step 4: Handle decompression if needed (content may have been gzipped before encryption)
        # Check if the decrypted content is gzip-compressed (magic bytes: 1f 8b)
        if len(decrypted_content) > 2 and decrypted_content[0] == 0x1f and decrypted_content[1] == 0x8b:
            logger.info("Decrypted content is gzip-compressed, decompressing...")
            decrypted_content = gzip.decompress(decrypted_content)
            logger.info(f"Decompressed to {len(decrypted_content)} bytes")
        
        return decrypted_content
        
    except Exception as e:
        logger.error(f"Decryption failed: {e}", exc_info=True)
        raise


def lambda_handler(event, context):
    """
    Main Lambda handler.
    
    Triggered by S3 upload of SQL dump files.
    Processes differential updates - only new/changed records are inserted/updated.
    """
    logger.info("Lambda function invoked")
    logger.info(f"Event: {json.dumps(event)}")
    
    processed_bundles = []
    kit_id = 'unknown'
    
    try:
        for record in event['Records']:
            bucket = record['s3']['bucket']['name']
            key = record['s3']['object']['key']
            
            logger.info(f"Processing file: s3://{bucket}/{key}")
            
            kit_id = key.split('/')[0] if '/' in key else 'unknown'
            
            response = s3.get_object(Bucket=bucket, Key=key)
            file_content = response['Body'].read()
            
            # Step 1: Handle decryption if file is encrypted with KMS
            file_content = decrypt_file_if_encrypted(bucket, key, file_content, response)
            
            # Step 2: Handle gzip decompression if needed
            if key.endswith('.gz') or key.endswith('.gz.encrypted'):
                # For encrypted files, content may still be gzipped before encryption
                try:
                    file_content = gzip.decompress(file_content)
                except Exception as e:
                    logger.info(f"Content is not gzip-compressed (or already decompressed by decrypt function): {e}")
            
            try:
                sql_content = file_content.decode('utf-8')
            except UnicodeDecodeError:
                logger.warning("SQL dump is not valid UTF-8; falling back to latin-1 decoding")
                sql_content = file_content.decode('latin-1')
            
            parser = SQLParser(sql_content)
            parsed_data = parser.parse()
            
            logger.info(f"Parsed data summary:")
            logger.info(f"  - Patients: {len(parsed_data['patients'])}")
            logger.info(f"  - Encounters: {len(parsed_data['encounters'])}")
            logger.info(f"  - Vitals: {len(parsed_data['vitals'])}")
            logger.info(f"  - Prescriptions: {len(parsed_data['prescriptions'])}")
            logger.info(f"  - Users: {len(parsed_data['users'])}")
            logger.info(f"  - Photos: {len(parsed_data['photos'])}")
            
            transformer = FHIRTransformer(parsed_data)
            bundles = transformer.create_bundles()
            
            logger.info(f"Created {len(bundles)} FHIR bundles")
            
            if not bundles:
                logger.warning("No FHIR bundles were created - check SQL parsing")
                continue
            
            rds = RDSManager()
            rds.connect()
            rds.set_kit_id(kit_id)
            
            try:
                for bundle in bundles:
                    encounter_id = rds.store_fhir_bundle(bundle, parsed_data)
                    processed_bundles.append({
                        'encounter_id': encounter_id,
                        'kit_id': kit_id
                    })
                    logger.info(f"Stored bundle for encounter {encounter_id}")
                
                logger.info(f"Successfully processed {len(bundles)} bundles from {key}")
                
            finally:
                rds.close()
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Successfully processed SQL dump with differential updates',
                'kit_id': kit_id,
                'bundles_processed': len(processed_bundles),
                'encounters': [b['encounter_id'] for b in processed_bundles]
            })
        }
        
    except Exception as e:
        logger.error(f"Error processing file: {e}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': str(e),
                'message': 'Failed to process SQL dump'
            })
        }

