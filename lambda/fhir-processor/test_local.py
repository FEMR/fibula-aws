"""
Local test suite for FHIR SQL Processor.

Tests two main things WITHOUT needing a CDK deployment, AWS, or PostgreSQL:
  1. FHIR formatting - validates every resource in the generated Bundles
  2. Differential update logic - verifies INSERT vs UPDATE behaviour using
     an in-process SQLite database as a stand-in for RDS

Run:
    pip install -r requirements-dev.txt
    python test_local.py          # pretty output
    pytest test_local.py -v       # pytest output with coverage
"""

import sys
import os
import json
import sqlite3
import logging
import unittest
from datetime import datetime
from typing import Dict, List, Optional, Any
from unittest.mock import MagicMock, patch

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: Mock AWS / DB imports BEFORE importing the Lambda module
# (boto3 and psycopg2 are top-level imports in process_sql_to_fhir.py, so we
#  stub them out in sys.modules first)
# ─────────────────────────────────────────────────────────────────────────────
sys.modules['boto3'] = MagicMock()
sys.modules['psycopg2'] = MagicMock()
sys.modules['psycopg2.extras'] = MagicMock()

# Add the lambda folder to the path so we can import it directly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from process_sql_to_fhir import SQLParser, FHIRTransformer  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# Logging – make test output readable
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,          # suppress INFO noise from the Lambda code
    format='%(levelname)s: %(message)s',
)
logger = logging.getLogger('test_local')
logger.setLevel(logging.DEBUG)


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def load_sql(filename: str) -> str:
    """Read a .sql file from the same directory as this test."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(path, 'r') as f:
        return f.read()


def get_resource_types(bundle) -> List[str]:
    """Return a list of resource types in bundle entry order."""
    return [e.resource.get_resource_type() for e in bundle.entry]


def find_observations_by_loinc(bundle, loinc_code: str):
    """Return all Observation entries whose code matches a LOINC code."""
    result = []
    for entry in bundle.entry:
        if entry.resource.get_resource_type() != 'Observation':
            continue
        for coding in (entry.resource.code.coding or []):
            if coding.code == loinc_code:
                result.append(entry.resource)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# SQLite-backed Differential Manager
# (mirrors the RDSManager upsert logic, but with SQLite so no Postgres needed)
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS patient (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_id INTEGER UNIQUE NOT NULL,
    first_name TEXT,
    last_name  TEXT,
    middle_name TEXT,
    phone      TEXT,
    email      TEXT,
    address1   TEXT,
    address2   TEXT,
    city       TEXT,
    zip_code   TEXT,
    sex        TEXT,
    age        INTEGER,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS user_ (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_id  INTEGER UNIQUE NOT NULL,
    first_name TEXT,
    last_name  TEXT,
    email      TEXT,
    deleted    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS encounter (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_id     INTEGER UNIQUE NOT NULL,
    patient_id    INTEGER REFERENCES patient(id),
    nurse_id      INTEGER,
    doctor_id     INTEGER,
    pharmacist_id INTEGER,
    triage_date   TEXT,
    smoking       INTEGER DEFAULT 0,
    diabetes      INTEGER DEFAULT 0,
    hypertension  INTEGER DEFAULT 0,
    cholesterol   INTEGER DEFAULT 0,
    alcohol       INTEGER DEFAULT 0,
    weeks_pregnant INTEGER,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS vital (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_id      INTEGER UNIQUE NOT NULL,
    encounter_id   INTEGER REFERENCES encounter(id),
    systolic_bp    REAL,
    diastolic_bp   REAL,
    heart_rate     REAL,
    resp_rate      REAL,
    body_temp      REAL,
    oxygen         REAL,
    glucose        REAL,
    height         REAL,
    weight         REAL,
    date_taken     TEXT
);

CREATE TABLE IF NOT EXISTS prescription (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_id        INTEGER UNIQUE NOT NULL,
    encounter_id     INTEGER REFERENCES encounter(id),
    medication_id    INTEGER,
    physician_id     INTEGER,
    amount           INTEGER,
    date_taken       TEXT,
    special_instr    TEXT,
    is_counseled     INTEGER,
    date_dispensed   TEXT
);

CREATE TABLE IF NOT EXISTS photo (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_id    INTEGER UNIQUE NOT NULL,
    description  TEXT,
    file_path    TEXT,
    imaging_link TEXT,
    insert_ts    TEXT
);
"""


class SQLiteDifferentialManager:
    """
    Replicates the RDSManager upsert logic against an in-process SQLite DB.
    
    Use this to verify:
      - INSERT on first appearance of a legacy_id
      - UPDATE (not duplicate INSERT) on subsequent appearances
      - No record duplication across multiple dumps
    """

    def __init__(self, db_path: str = ':memory:'):
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._processed: Dict[str, set] = {
            'patients': set(),
            'encounters': set(),
            'users': set(),
        }

    def reset_session_tracking(self):
        """Call between separate dump files to reset in-memory dedup tracking."""
        self._processed = {'patients': set(), 'encounters': set(), 'users': set()}

    # ── counts (for assertions) ────────────────────────────────────────────

    def count(self, table: str) -> int:
        row = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return row[0]

    def get_patient(self, legacy_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM patient WHERE legacy_id=?", (legacy_id,)
        ).fetchone()

    def get_encounter(self, legacy_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM encounter WHERE legacy_id=?", (legacy_id,)
        ).fetchone()

    def get_vital(self, legacy_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM vital WHERE legacy_id=?", (legacy_id,)
        ).fetchone()

    # ── upsert helpers ─────────────────────────────────────────────────────

    def upsert_patient(self, p: Dict) -> int:
        legacy_id = p.get('id') or p.get('legacy_id')
        if legacy_id in self._processed['patients']:
            row = self.get_patient(legacy_id)
            return row['id']

        existing = self.get_patient(legacy_id)
        now = datetime.now().isoformat()

        if existing:
            self.conn.execute("""
                UPDATE patient
                SET first_name=?, last_name=?, middle_name=?,
                    phone=?, email=?, address1=?, address2=?,
                    city=?, zip_code=?, sex=?, age=?, updated_at=?
                WHERE legacy_id=?
            """, (
                p.get('first_name'), p.get('last_name'), p.get('middle_name'),
                p.get('phone_number'), p.get('email_address'),
                p.get('address1'), p.get('address2'),
                p.get('city'), p.get('zip_code'),
                p.get('sex_assigned_at_birth'), p.get('age'),
                now, legacy_id
            ))
            db_id = existing['id']
        else:
            cur = self.conn.execute("""
                INSERT INTO patient
                    (legacy_id, first_name, last_name, middle_name,
                     phone, email, address1, address2, city, zip_code,
                     sex, age, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                legacy_id,
                p.get('first_name'), p.get('last_name'), p.get('middle_name'),
                p.get('phone_number'), p.get('email_address'),
                p.get('address1'), p.get('address2'),
                p.get('city'), p.get('zip_code'),
                p.get('sex_assigned_at_birth'), p.get('age'), now
            ))
            db_id = cur.lastrowid

        self.conn.commit()
        self._processed['patients'].add(legacy_id)
        return db_id

    def upsert_user(self, u: Dict) -> int:
        legacy_id = u.get('id') or u.get('legacy_id')
        if legacy_id in self._processed['users']:
            row = self.conn.execute(
                "SELECT id FROM user_ WHERE legacy_id=?", (legacy_id,)
            ).fetchone()
            return row['id']

        existing = self.conn.execute(
            "SELECT * FROM user_ WHERE legacy_id=?", (legacy_id,)
        ).fetchone()

        if existing:
            self.conn.execute("""
                UPDATE user_
                SET first_name=?, last_name=?, email=?, deleted=?
                WHERE legacy_id=?
            """, (
                u.get('firstName', u.get('first_name')),
                u.get('lastName', u.get('last_name')),
                u.get('email'), int(u.get('deleted', 0)),
                legacy_id
            ))
            db_id = existing['id']
        else:
            cur = self.conn.execute("""
                INSERT INTO user_ (legacy_id, first_name, last_name, email, deleted)
                VALUES (?,?,?,?,?)
            """, (
                legacy_id,
                u.get('firstName', u.get('first_name')),
                u.get('lastName', u.get('last_name')),
                u.get('email'), int(u.get('deleted', 0))
            ))
            db_id = cur.lastrowid

        self.conn.commit()
        self._processed['users'].add(legacy_id)
        return db_id

    def upsert_encounter(self, e: Dict, patient_db_id: int,
                         user_legacy_to_db: Dict[int, int]) -> int:
        legacy_id = e.get('id') or e.get('legacy_id')
        if legacy_id in self._processed['encounters']:
            row = self.get_encounter(legacy_id)
            return row['id']

        existing = self.get_encounter(legacy_id)
        now = datetime.now().isoformat()

        nurse_db  = user_legacy_to_db.get(e.get('nurse_id'))
        doctor_db = user_legacy_to_db.get(e.get('doctor_id'))
        pharm_db  = user_legacy_to_db.get(e.get('pharmacist_id'))

        if existing:
            self.conn.execute("""
                UPDATE encounter
                SET patient_id=?, nurse_id=?, doctor_id=?, pharmacist_id=?,
                    triage_date=?, smoking=?, diabetes=?, hypertension=?,
                    cholesterol=?, alcohol=?, weeks_pregnant=?, updated_at=?
                WHERE legacy_id=?
            """, (
                patient_db_id, nurse_db, doctor_db, pharm_db,
                e.get('date_of_triage_visit'), int(e.get('smoking') or 0),
                int(e.get('history_of_diabetes') or 0),
                int(e.get('history_of_hypertension') or 0),
                int(e.get('history_of_high_cholesterol') or 0),
                int(e.get('alcohol') or 0),
                e.get('weeks_pregnant'), now,
                legacy_id
            ))
            db_id = existing['id']
        else:
            cur = self.conn.execute("""
                INSERT INTO encounter
                    (legacy_id, patient_id, nurse_id, doctor_id, pharmacist_id,
                     triage_date, smoking, diabetes, hypertension,
                     cholesterol, alcohol, weeks_pregnant, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                legacy_id, patient_db_id, nurse_db, doctor_db, pharm_db,
                e.get('date_of_triage_visit'),
                int(e.get('smoking') or 0),
                int(e.get('history_of_diabetes') or 0),
                int(e.get('history_of_hypertension') or 0),
                int(e.get('history_of_high_cholesterol') or 0),
                int(e.get('alcohol') or 0),
                e.get('weeks_pregnant'), now
            ))
            db_id = cur.lastrowid

        self.conn.commit()
        self._processed['encounters'].add(legacy_id)
        return db_id

    def upsert_vital(self, v: Dict, encounter_db_id: int) -> int:
        legacy_id = v.get('id') or v.get('legacy_id')
        if not legacy_id:
            return None

        existing = self.conn.execute(
            "SELECT * FROM vital WHERE legacy_id=?", (legacy_id,)
        ).fetchone()

        if existing:
            self.conn.execute("""
                UPDATE vital
                SET encounter_id=?, systolic_bp=?, diastolic_bp=?,
                    heart_rate=?, resp_rate=?, body_temp=?, oxygen=?,
                    glucose=?, height=?, weight=?, date_taken=?
                WHERE legacy_id=?
            """, (
                encounter_db_id,
                v.get('systolic_blood_pressure'),
                v.get('diastolic_blood_pressure'),
                v.get('heart_rate'), v.get('respiratory_rate'),
                v.get('body_temperature'), v.get('oxygen_concentration'),
                v.get('glucose_level'), v.get('body_height_primary'),
                v.get('body_weight'), v.get('dateTaken'),
                legacy_id
            ))
            db_id = existing['id']
        else:
            cur = self.conn.execute("""
                INSERT INTO vital
                    (legacy_id, encounter_id, systolic_bp, diastolic_bp,
                     heart_rate, resp_rate, body_temp, oxygen,
                     glucose, height, weight, date_taken)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                legacy_id, encounter_db_id,
                v.get('systolic_blood_pressure'),
                v.get('diastolic_blood_pressure'),
                v.get('heart_rate'), v.get('respiratory_rate'),
                v.get('body_temperature'), v.get('oxygen_concentration'),
                v.get('glucose_level'), v.get('body_height_primary'),
                v.get('body_weight'), v.get('dateTaken')
            ))
            db_id = cur.lastrowid

        self.conn.commit()
        return db_id

    def upsert_prescription(self, rx: Dict, encounter_db_id: int,
                            user_legacy_to_db: Dict[int, int]) -> Optional[int]:
        legacy_id = rx.get('id') or rx.get('legacy_id')
        if not legacy_id:
            return None

        existing = self.conn.execute(
            "SELECT * FROM prescription WHERE legacy_id=?", (legacy_id,)
        ).fetchone()

        physician_db = user_legacy_to_db.get(rx.get('physician_id'))

        if existing:
            self.conn.execute("""
                UPDATE prescription
                SET encounter_id=?, medication_id=?, physician_id=?,
                    amount=?, special_instr=?, is_counseled=?, date_dispensed=?
                WHERE legacy_id=?
            """, (
                encounter_db_id, rx.get('medication_id'), physician_db,
                rx.get('amount'), rx.get('specialInstructions'),
                int(rx.get('isCounseled') or 0),
                rx.get('dateDispensed'), legacy_id
            ))
            db_id = existing['id']
        else:
            cur = self.conn.execute("""
                INSERT INTO prescription
                    (legacy_id, encounter_id, medication_id, physician_id,
                     amount, date_taken, special_instr, is_counseled, date_dispensed)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (
                legacy_id, encounter_db_id, rx.get('medication_id'), physician_db,
                rx.get('amount'), rx.get('dateTaken'),
                rx.get('specialInstructions'),
                int(rx.get('isCounseled') or 0),
                rx.get('dateDispensed')
            ))
            db_id = cur.lastrowid

        self.conn.commit()
        return db_id

    def process_dump(self, parsed_data: Dict):
        """
        Run the full differential upsert for one parsed SQL dump.
        Returns a summary dict of inserts vs updates across all tables.
        """
        self.reset_session_tracking()

        # Track counts before
        before = {
            'patients':      self.count('patient'),
            'users':         self.count('user_'),
            'encounters':    self.count('encounter'),
            'vitals':        self.count('vital'),
            'prescriptions': self.count('prescription'),
        }

        user_legacy_to_db: Dict[int, int] = {}
        patient_legacy_to_db: Dict[int, int] = {}

        # 1. Users first (practitioners)
        for u in parsed_data.get('users', []):
            db_id = self.upsert_user(u)
            uid = u.get('id') or u.get('legacy_id')
            user_legacy_to_db[uid] = db_id

        # 2. Patients
        for p in parsed_data.get('patients', []):
            db_id = self.upsert_patient(p)
            pid = p.get('id') or p.get('legacy_id')
            patient_legacy_to_db[pid] = db_id

        # 3. Encounters + their vitals/prescriptions
        for enc in parsed_data.get('encounters', []):
            patient_legacy_id = enc.get('patient_id') or enc.get('patient')
            patient_db_id = patient_legacy_to_db.get(patient_legacy_id)
            if not patient_db_id:
                logger.warning(f"No patient DB ID for legacy {patient_legacy_id}, skipping encounter")
                continue

            encounter_db_id = self.upsert_encounter(enc, patient_db_id, user_legacy_to_db)
            enc_legacy_id = enc.get('id') or enc.get('legacy_id')

            # 4. Vitals linked to this encounter
            for v in parsed_data.get('vitals', []):
                v_enc_id = v.get('patientEncounterId') or v.get('encounter_id')
                if v_enc_id == enc_legacy_id:
                    self.upsert_vital(v, encounter_db_id)

            # 5. Prescriptions linked to this encounter
            for rx in parsed_data.get('prescriptions', []):
                rx_enc_id = rx.get('patientEncounterId') or rx.get('encounter_id')
                if rx_enc_id == enc_legacy_id:
                    self.upsert_prescription(rx, encounter_db_id, user_legacy_to_db)

        # Track counts after
        after = {
            'patients':      self.count('patient'),
            'users':         self.count('user_'),
            'encounters':    self.count('encounter'),
            'vitals':        self.count('vital'),
            'prescriptions': self.count('prescription'),
        }

        return {
            'inserted': {k: after[k] - before[k] for k in before},
            'totals':   after,
        }


# ─────────────────────────────────────────────────────────────────────────────
# TEST CASES
# ─────────────────────────────────────────────────────────────────────────────

class TestSQLParser(unittest.TestCase):
    """Verify the SQL parser extracts the correct data from a dump."""

    @classmethod
    def setUpClass(cls):
        sql = load_sql('sample_dump.sql')
        cls.data = SQLParser(sql).parse()

    def test_patients_parsed(self):
        self.assertEqual(len(self.data['patients']), 2)

    def test_encounters_parsed(self):
        self.assertEqual(len(self.data['encounters']), 2)

    def test_vitals_parsed(self):
        self.assertEqual(len(self.data['vitals']), 2)

    def test_prescriptions_parsed(self):
        self.assertEqual(len(self.data['prescriptions']), 3)

    def test_users_parsed(self):
        self.assertEqual(len(self.data['users']), 3)

    def test_patient_fields(self):
        p = self.data['patients'][0]
        self.assertEqual(p['first_name'], 'Pedro')
        self.assertEqual(p['last_name'], 'Rodriguez')
        self.assertEqual(p['sex_assigned_at_birth'], 'm')
        self.assertEqual(p['age'], 45)

    def test_encounter_flags(self):
        enc = self.data['encounters'][0]
        # From sample_dump.sql: smoking=1, history_of_diabetes=1
        self.assertEqual(enc['smoking'], 1)
        self.assertEqual(enc['history_of_diabetes'], 1)

    def test_vital_values(self):
        v = self.data['vitals'][0]
        self.assertEqual(v['systolic_blood_pressure'], 120)
        self.assertEqual(v['diastolic_blood_pressure'], 80)
        self.assertEqual(v['heart_rate'], 72)


class TestFHIRFormatting(unittest.TestCase):
    """Verify the FHIR R5 Bundles are built correctly."""

    @classmethod
    def setUpClass(cls):
        sql = load_sql('sample_dump.sql')
        parsed = SQLParser(sql).parse()
        cls.bundles = FHIRTransformer(parsed).create_bundles()
        cls.b0 = cls.bundles[0]  # first bundle (encounter 1001)
        cls.b1 = cls.bundles[1]  # second bundle (encounter 1002)

    # ── Bundle basics ──────────────────────────────────────────────────────

    def test_bundle_count(self):
        """One bundle per encounter."""
        self.assertEqual(len(self.bundles), 2)

    def test_bundle_type_is_document(self):
        self.assertEqual(self.b0.type, 'document')

    def test_bundle_has_identifier(self):
        self.assertIsNotNone(self.b0.identifier)
        self.assertIn('encounter-', self.b0.identifier.value)

    # ── Entry order ────────────────────────────────────────────────────────

    def test_composition_is_first_entry(self):
        """FHIR spec requires Composition to be first in a document Bundle."""
        types = get_resource_types(self.b0)
        self.assertEqual(types[0], 'Composition',
                         f'Expected Composition as first resource, got: {types}')

    def test_all_required_resource_types_present(self):
        types = set(get_resource_types(self.b0))
        for required in ['Composition', 'Patient', 'Practitioner', 'Encounter']:
            self.assertIn(required, types, f'Missing resource type: {required}')

    # ── Composition ────────────────────────────────────────────────────────

    def test_composition_status(self):
        comp = self.b0.entry[0].resource
        self.assertEqual(comp.status, 'final')

    def test_composition_type_loinc(self):
        comp = self.b0.entry[0].resource
        codes = [c.code for c in comp.type.coding]
        self.assertIn('11506-3', codes)

    def test_composition_references_patient(self):
        comp = self.b0.entry[0].resource
        self.assertIn('urn:uuid:patient-', comp.subject[0].reference)

    # ── Patient ────────────────────────────────────────────────────────────

    def _find_patient(self, bundle):
        return next(e.resource for e in bundle.entry
                    if e.resource.get_resource_type() == 'Patient')

    def test_patient_name(self):
        pt = self._find_patient(self.b0)
        self.assertEqual(pt.name[0].family, 'Rodriguez')
        self.assertIn('Pedro', pt.name[0].given)

    def test_patient_gender(self):
        pt = self._find_patient(self.b0)
        self.assertEqual(pt.gender, 'male')

    def test_patient_gender_female(self):
        pt = self._find_patient(self.b1)
        self.assertEqual(pt.gender, 'female')

    def test_patient_identifier_system(self):
        pt = self._find_patient(self.b0)
        self.assertEqual(pt.identifier[0].system, 'http://femr.org/patient-id')

    def test_patient_phone_telecom(self):
        pt = self._find_patient(self.b0)
        phones = [t for t in pt.telecom if t.system == 'phone']
        self.assertTrue(phones, 'Expected phone telecom entry')
        self.assertEqual(phones[0].value, '555-0101')

    def test_patient_address(self):
        pt = self._find_patient(self.b0)
        self.assertIsNotNone(pt.address)
        self.assertEqual(pt.address[0].city, 'San Salvador')

    def test_patient_active(self):
        pt = self._find_patient(self.b0)
        self.assertTrue(pt.active)

    # ── Encounter ──────────────────────────────────────────────────────────

    def _find_encounter(self, bundle):
        return next(e.resource for e in bundle.entry
                    if e.resource.get_resource_type() == 'Encounter')

    def test_encounter_status(self):
        enc = self._find_encounter(self.b0)
        self.assertEqual(enc.status, 'finished')

    def test_encounter_subject_references_patient(self):
        enc = self._find_encounter(self.b0)
        self.assertIn('patient-', enc.subject.reference)

    def test_encounter_has_participants(self):
        enc = self._find_encounter(self.b0)
        self.assertIsNotNone(enc.participant)
        self.assertGreater(len(enc.participant), 0)

    # ── Observations / Vitals ──────────────────────────────────────────────

    def test_blood_pressure_observation_present(self):
        obs_list = find_observations_by_loinc(self.b0, '85354-9')
        self.assertEqual(len(obs_list), 1)

    def test_blood_pressure_has_two_components(self):
        obs = find_observations_by_loinc(self.b0, '85354-9')[0]
        self.assertEqual(len(obs.component), 2)
        component_codes = [c.code.coding[0].code for c in obs.component]
        self.assertIn('8480-6', component_codes, 'Missing systolic code')
        self.assertIn('8462-4', component_codes, 'Missing diastolic code')

    def test_heart_rate_observation(self):
        obs_list = find_observations_by_loinc(self.b0, '8867-4')
        self.assertEqual(len(obs_list), 1)
        self.assertEqual(obs_list[0].valueQuantity.value, 72.0)
        self.assertEqual(obs_list[0].valueQuantity.unit, 'beats/minute')

    def test_respiratory_rate_observation(self):
        obs_list = find_observations_by_loinc(self.b0, '9279-1')
        self.assertEqual(len(obs_list), 1)

    def test_temperature_observation(self):
        obs_list = find_observations_by_loinc(self.b0, '8310-5')
        self.assertEqual(len(obs_list), 1)
        self.assertEqual(obs_list[0].valueQuantity.value, 98.6)

    def test_oxygen_observation(self):
        obs_list = find_observations_by_loinc(self.b0, '2708-6')
        self.assertEqual(len(obs_list), 1)

    def test_glucose_observation(self):
        obs_list = find_observations_by_loinc(self.b0, '2339-0')
        self.assertEqual(len(obs_list), 1)

    def test_height_observation(self):
        obs_list = find_observations_by_loinc(self.b0, '8302-2')
        self.assertEqual(len(obs_list), 1)

    def test_weight_observation(self):
        obs_list = find_observations_by_loinc(self.b0, '29463-7')
        self.assertEqual(len(obs_list), 1)
        self.assertEqual(obs_list[0].valueQuantity.value, 75.5)

    # ── Flag Observations (social history) ────────────────────────────────

    def test_smoking_flag_observation(self):
        """Encounter 1001 has smoking=1, should appear as Observation."""
        obs_list = find_observations_by_loinc(self.b0, '72166-2')
        self.assertEqual(len(obs_list), 1)
        self.assertTrue(obs_list[0].valueBoolean)

    def test_diabetes_flag_observation(self):
        """Encounter 1001 has history_of_diabetes=1."""
        obs_list = find_observations_by_loinc(self.b0, '44877-9')
        self.assertEqual(len(obs_list), 1)
        self.assertTrue(obs_list[0].valueBoolean)

    def test_no_smoking_when_flag_false(self):
        """Encounter 1002 has smoking=0 — no smoking observation expected."""
        obs_list = find_observations_by_loinc(self.b1, '72166-2')
        self.assertEqual(len(obs_list), 0)

    def test_pregnancy_observation(self):
        """Encounter 1002 has weeks_pregnant=12."""
        obs_list = find_observations_by_loinc(self.b1, '49051-6')
        self.assertEqual(len(obs_list), 1)
        self.assertEqual(obs_list[0].valueQuantity.value, 12.0)
        self.assertEqual(obs_list[0].valueQuantity.unit, 'weeks')

    # ── Medications ────────────────────────────────────────────────────────

    def test_medication_request_present(self):
        types = get_resource_types(self.b0)
        self.assertIn('MedicationRequest', types)

    def test_observation_category_vital_signs(self):
        """All vital observations should be categorised as vital-signs."""
        for entry in self.b0.entry:
            if entry.resource.get_resource_type() != 'Observation':
                continue
            if not entry.resource.category:
                continue
            cats = [c.coding[0].code for c in entry.resource.category
                    if c.coding]
            # vital-signs or social-history — both are valid
            for cat in cats:
                self.assertIn(cat, ['vital-signs', 'social-history', 'exam'])

    # ── FHIR JSON serialisability ──────────────────────────────────────────

    def test_bundle_serialises_to_valid_json(self):
        """Make sure the Bundle can be serialised without error."""
        json_str = self.b0.json()
        data = json.loads(json_str)
        self.assertEqual(data['resourceType'], 'Bundle')
        self.assertEqual(data['type'], 'document')

    def test_bundle_entry_count_is_sane(self):
        """Bundle should have at minimum 4 entries (comp+patient+enc+1 obs)."""
        self.assertGreaterEqual(len(self.b0.entry), 4)


class TestDifferentialUpdates(unittest.TestCase):
    """
    Verify the differential (UPSERT) database logic.

    Uses a shared SQLite database so state persists across tests within each
    sub-scenario.  Tests run in order:
      1. First snapshot → all records are INSERTed
      2. Same snapshot  → records UPDATEd, counts unchanged
      3. Second snapshot → new records INSERTed, changed data UPDATEd
    """

    @classmethod
    def setUpClass(cls):
        # Shared in-memory DB for all differential tests
        cls.db = SQLiteDifferentialManager()

        # Parse both snapshots up front
        cls.snap1 = SQLParser(load_sql('sample_dump.sql')).parse()
        cls.snap2 = SQLParser(load_sql('sample_dump_snapshot2.sql')).parse()

        # Run snapshot 1
        cls.snap1_result = cls.db.process_dump(cls.snap1)

        # Run snapshot 1 AGAIN (idempotency test)
        cls.snap1_again_result = cls.db.process_dump(cls.snap1)

        # Run snapshot 2 (incremental update)
        cls.snap2_result = cls.db.process_dump(cls.snap2)

    # ── Snapshot 1: initial load ───────────────────────────────────────────

    def test_snap1_patients_inserted(self):
        self.assertEqual(self.snap1_result['inserted']['patients'], 2)

    def test_snap1_users_inserted(self):
        self.assertEqual(self.snap1_result['inserted']['users'], 3)

    def test_snap1_encounters_inserted(self):
        self.assertEqual(self.snap1_result['inserted']['encounters'], 2)

    def test_snap1_vitals_inserted(self):
        self.assertEqual(self.snap1_result['inserted']['vitals'], 2)

    def test_snap1_prescriptions_inserted(self):
        self.assertEqual(self.snap1_result['inserted']['prescriptions'], 3)

    # ── Snapshot 1 re-run: idempotency ────────────────────────────────────

    def test_idempotent_patients_no_new_rows(self):
        """Re-processing same dump must not create duplicate patients."""
        self.assertEqual(self.snap1_again_result['inserted']['patients'], 0)

    def test_idempotent_encounters_no_new_rows(self):
        self.assertEqual(self.snap1_again_result['inserted']['encounters'], 0)

    def test_idempotent_vitals_no_new_rows(self):
        self.assertEqual(self.snap1_again_result['inserted']['vitals'], 0)

    def test_idempotent_prescriptions_no_new_rows(self):
        self.assertEqual(self.snap1_again_result['inserted']['prescriptions'], 0)

    # ── Snapshot 2: differential update ───────────────────────────────────

    def test_snap2_new_patient_inserted(self):
        """Snapshot 2 adds patient 103 — should be inserted."""
        self.assertEqual(self.snap2_result['inserted']['patients'], 1)

    def test_snap2_new_encounter_inserted(self):
        """Snapshot 2 adds encounter 1003 — should be inserted."""
        self.assertEqual(self.snap2_result['inserted']['encounters'], 1)

    def test_snap2_patient_age_updated(self):
        """snapshot2 changes patient 101 age from 45 → 47."""
        row = self.db.get_patient(101)
        self.assertIsNotNone(row)
        self.assertEqual(row['age'], 47,
                         f'Expected age=47 after snap2 update, got {row["age"]}')

    def test_snap2_phone_updated(self):
        """snapshot2 changes patient 102 phone number."""
        row = self.db.get_patient(102)
        self.assertIsNotNone(row)
        self.assertEqual(row['phone'], '555-9999')

    def test_snap2_encounter_smoking_flag_updated(self):
        """snapshot2 changes enc 1001 from smoking=1 to smoking=0."""
        row = self.db.get_encounter(1001)
        self.assertIsNotNone(row)
        self.assertEqual(row['smoking'], 0)

    def test_snap2_new_vital_inserted(self):
        """snapshot2 adds vital 5003 for encounter 1003."""
        self.assertEqual(self.snap2_result['inserted']['vitals'], 1)

    def test_snap2_new_prescription_inserted(self):
        """snapshot2 adds a new prescription for encounter 1003."""
        self.assertEqual(self.snap2_result['inserted']['prescriptions'], 1)

    def test_total_patients_after_all_snapshots(self):
        """After all three runs: 3 unique patients total."""
        self.assertEqual(self.db.count('patient'), 3)

    def test_total_encounters_after_all_snapshots(self):
        """After all three runs: 3 unique encounters total."""
        self.assertEqual(self.db.count('encounter'), 3)

    def test_no_duplicate_vitals(self):
        """5001 and 5002 from snap1 + 5003 from snap2 = 3 vitals, no dups."""
        self.assertEqual(self.db.count('vital'), 3)

    def test_no_duplicate_prescriptions(self):
        """3 from snap1 + 1 new from snap2 = 4 prescriptions total."""
        self.assertEqual(self.db.count('prescription'), 4)


# ─────────────────────────────────────────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def run_tests():
    """Run all tests with a clear, readable console summary."""
    suite = unittest.TestSuite()

    for cls in [TestSQLParser, TestFHIRFormatting, TestDifferentialUpdates]:
        suite.addTests(unittest.TestLoader().loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print('\n' + '='*60)
    if result.wasSuccessful():
        print(f'  ✅  ALL {result.testsRun} TESTS PASSED')
    else:
        print(f'  ❌  {len(result.failures)} FAILURES, '
              f'{len(result.errors)} ERRORS '
              f'out of {result.testsRun} tests')
    print('='*60 + '\n')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    exit(run_tests())
