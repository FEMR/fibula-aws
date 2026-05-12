-- Sample fEMR SQL Dump for Testing FHIR Processor
-- This represents a simplified version of what a field kit would upload

-- Users (Healthcare Providers)
INSERT INTO `users` (`id`, `legacy_id`, `firstName`, `lastName`, `email`, `password`, `lastLogin`, `deleted`, `passwordReset`, `notes`, `passwordCreatedDate`, `dateCreated`, `createdBy`) 
VALUES 
(1, 1, 'Jane', 'Doe', 'jane.doe@femr.org', 'hashed_password', '2026-02-10 10:00:00', 0, 0, 'Nurse', '2025-01-01 00:00:00', '2025-01-01 00:00:00', 1),
(2, 2, 'John', 'Smith', 'john.smith@femr.org', 'hashed_password', '2026-02-10 10:00:00', 0, 0, 'Doctor', '2025-01-01 00:00:00', '2025-01-01 00:00:00', 1),
(3, 3, 'Maria', 'Garcia', 'maria.garcia@femr.org', 'hashed_password', '2026-02-10 10:00:00', 0, 0, 'Pharmacist', '2025-01-01 00:00:00', '2025-01-01 00:00:00', 1);

-- Patients
INSERT INTO `patients` (`id`, `legacy_id`, `userid`, `first_name`, `middle_name`, `last_name`, `phone_number`, `age`, `sex_assigned_at_birth`, `current_address`, `address1`, `address2`, `city`, `zip_code`, `email_address`, `social_security_number`) 
VALUES 
(101, 101, NULL, 'Pedro', 'Luis', 'Rodriguez', '555-0101', 45, 'm', '123 Main St', '123 Main St', 'Apt 4', 'San Salvador', '12345', 'pedro@example.com', NULL),
(102, 102, NULL, 'Maria', 'Elena', 'Hernandez', '555-0102', 32, 'f', '456 Oak Ave', '456 Oak Ave', NULL, 'San Salvador', '12346', 'maria@example.com', NULL);

-- Patient Encounters
INSERT INTO `patientencounter` (`id`, `legacy_id`, `patient_id`, `nurse_id`, `date_of_triage_visit`, `date_of_medical_visit`, `date_of_pharmacy_visit`, `doctor_id`, `pharmacist_id`, `smoking`, `history_of_diabetes`, `history_of_hypertension`, `history_of_high_cholesterol`, `alcohol`, `weeks_pregnant`, `timestamp`) 
VALUES 
(1001, 1001, 101, 1, '2026-02-10 09:00:00', '2026-02-10 10:00:00', '2026-02-10 11:00:00', 2, 3, 1, 1, 0, 0, 0, NULL, '2026-02-10 11:30:00'),
(1002, 1002, 102, 1, '2026-02-10 09:30:00', '2026-02-10 10:30:00', '2026-02-10 11:30:00', 2, 3, 0, 0, 1, 1, 0, 12, '2026-02-10 12:00:00');

-- Patient Encounter Vitals
INSERT INTO `patientencountervital` (`id`, `legacy_id`, `userId`, `patientEncounterId`, `vital_id`, `vitalValue`, `dateTaken`, `diastolic_blood_pressure`, `systolic_blood_pressure`, `mean_arterial_pressure`, `heart_rate`, `respiratory_rate`, `body_temperature`, `oxygen_concentration`, `glucose_level`, `body_height_primary`, `body_weight`) 
VALUES 
(5001, 5001, '1', 1001, 1, 120, '2026-02-10 09:15:00', 80, 120, 93.3, 72, 16, 98.6, 98, 110, 170, 75.5),
(5002, 5002, '1', 1002, 1, 130, '2026-02-10 09:45:00', 85, 130, 100, 80, 18, 98.2, 97, NULL, 160, 62.0);

-- Medications
INSERT INTO `medication` (`id`, `legacy_id`, `name`, `text`, `isDeleted`) 
VALUES 
(2001, 2001, 'Metformin', 'Metformin 500mg', 0),
(2002, 2002, 'Lisinopril', 'Lisinopril 10mg', 0),
(2003, 2003, 'Prenatal Vitamin', 'Prenatal multivitamin', 0);

-- Patient Prescriptions
INSERT INTO `patientprescriptions` (`id`, `legacy_id`, `patientEncounterId`, `medication_id`, `physician_id`, `amount`, `dateTaken`, `specialInstructions`, `isCounseled`, `dateDispensed`) 
VALUES 
(3001, 3001, 1001, 2001, 2, 60, '2026-02-10 10:30:00', 'Take one tablet twice daily with meals', 1, '2026-02-10 11:00:00'),
(3002, 3002, 1002, 2002, 2, 30, '2026-02-10 10:45:00', 'Take one tablet daily in the morning', 1, '2026-02-10 11:30:00'),
(3003, 3003, 1002, 2003, 2, 30, '2026-02-10 10:45:00', 'Take one tablet daily', 1, '2026-02-10 11:30:00');

-- Diagnoses
INSERT INTO `diagnosis` (`id`, `text`) 
VALUES 
(4001, 'Type 2 Diabetes Mellitus'),
(4002, 'Hypertension'),
(4003, 'Pregnancy - Normal');

-- Photos (sample)
INSERT INTO `photo` (`id`, `legacy_id`, `description`, `file_path`, `insertTS`, `imaging_link`) 
VALUES 
(6001, 6001, 'Patient wound - left leg', '/photos/2026/02/wound_101.jpg', '2026-02-10', 'https://s3.amazonaws.com/femr-photos/wound_101.jpg');
