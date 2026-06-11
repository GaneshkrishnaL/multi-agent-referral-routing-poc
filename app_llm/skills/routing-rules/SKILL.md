---
name: routing-rules
description: >
  Use this skill to determine the care path (eConsult, Virtual, or In-person)
  and the patient engagement tag for a specialist referral. Encodes
  the health system's internal routing rules. Trigger whenever a referral route or
  patient tag is being decided.
license: Apache-2.0
---

# PCP Assist Referral Routing Rules

Apply these rules EXACTLY, in order. The first matching rule wins. Do not
improvise, do not weigh other factors, do not let clinical severity change the
route — severity is handled by the urgency flag, not the care path.

## Step 1 — Existing relationship check (In-person)

IF the patient has ANY claim to the TARGET specialty within the last 12 months
(check claim `Specialty` == referral specialty and `ServiceDateFrom` within 12
months of today) THEN:

- care_path = "In-person"
- patient_tag = "Existing Specialist Relationship"
- STOP. Do not evaluate further rules.

The claim proves the relationship. A prior referral WITHOUT a claim (e.g. a
no-show) does NOT count — no claim, no relationship.

## Step 2 — Classify the patient tag

(Only reached when there is NO target-specialty claim in the last 12 months.)
Claims to OTHER specialties play no role here.

- IF the patient has >= 1 recorded encounter: tag = "Established Patient"
- ELSE IF the patient has a checked-out appointment: tag = "New Patient"
- ELSE IF an appointment is scheduled in the next 1 month: tag = "New Patient - Needs first visit"
- ELSE: tag = "Unengaged Patient - Needs first visit"

## Step 3 — Route by specialty

- IF the referral specialty is Cardiology: care_path = "Virtual"
  (synchronous video visit with an internal cardiologist)
- ELSE IF the specialty is one of Endocrinology, Nephrology, Rheumatology,
  Neurology, Hematology, Pulmonology: care_path = "eConsult"
- ELSE: the specialty is NOT covered — say so explicitly and stop. Never
  default an uncovered specialty into eConsult.

## Output contract

Always answer with exactly this JSON and nothing else:

```json
{
  "care_path": "eConsult | Virtual | In-person",
  "patient_tag": "<one of the five exact tag strings above>",
  "rationale": "<one sentence citing the rule that fired and the evidence (claim date / encounters / appointments)>"
}
```
