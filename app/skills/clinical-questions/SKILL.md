---
name: clinical-questions
description: >
  Use this skill to write 2-3 specific, answerable clinical questions for an
  eConsult specialist, tailored to the patient. Provides gold-standard examples
  and rules. Trigger whenever clinical questions for a referral are being generated.
license: Apache-2.0
---

# Clinical Question Generation

Write 2-3 questions the PCP genuinely needs the specialist to answer for THIS
patient. Each question must be specific, decision-focused, answerable
asynchronously from the chart, and grounded in the patient's actual findings.

## Rules

- Anchor each question in the patient's real data (specific labs, meds tried,
  comorbidities). No generic textbook questions.
- Focus on the decision the PCP is stuck on: next-line therapy, need for a workup,
  procedure vs referral, dosing in the setting of a comorbidity.
- Make them answerable without seeing the patient (this is an async eConsult).
- Do not ask the specialist to simply "evaluate the patient" — ask a precise question.

## Gold-standard examples

**Uncontrolled type 2 diabetes** (metformin + glipizide, A1c 9.2%, no insulin,
no documented complications): "Given persistent hyperglycemia despite dual oral
therapy, would you recommend initiation of insulin versus addition of a GLP-1
receptor agonist or SGLT2 inhibitor in this patient?"

**Iron deficiency anemia** (no overt bleeding, low-iron diet, Hgb 9.8, MCV 72,
ferritin 8, no prior GI workup): "In a patient with confirmed iron deficiency
anemia and no overt bleeding, what is the recommended next step for evaluation?
Should a GI workup (EGD/colonoscopy) be initiated at this stage?"

**Knee osteoarthritis** (>1 year, mild-moderate joint space narrowing on X-ray,
partial relief with NSAIDs and PT, functional limitation): "For symptomatic knee
osteoarthritis not adequately controlled with NSAIDs and PT, are intra-articular
injections or orthopedic referral appropriate at this stage?"

When a comorbidity changes the answer (e.g., eGFR affecting SGLT2 dosing, or a
prior no-show affecting choice of a once-weekly injectable), build that into the
question.
