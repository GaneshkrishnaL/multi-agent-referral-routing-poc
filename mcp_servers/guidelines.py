# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Condition-keyed clinical guideline store for the Smart Care Triage PoC.

Each entry is a short, clinically strong summary grounded in a current,
authoritative guideline (cited inline). Lookup is by the patient's actual
condition first (so a prediabetic gets prediabetes guidance, not type 2 diabetes
guidance), falling back to a general specialty statement when the condition is
not in the library. In production this is replaced by RAG over the full guideline
PDFs plus MedGemma; the sources below are the same documents that corpus uses.

Coverage matches the conditions present in the synthetic dataset across the seven
referral specialties.
"""

from __future__ import annotations

# Ordered most-specific first; the first entry whose keyword appears in the
# condition string wins (so "iron deficiency" beats "anemia", "neuropathy" and
# "kidney" are matched before the diabetes entries, etc.).
CONDITION_GUIDELINES: list[tuple[tuple[str, ...], str]] = [
    (
        ("iron deficiency",),
        "Iron deficiency anemia: confirm with a low ferritin or low transferrin "
        "saturation. In an adult without an obvious source, evaluate the GI tract "
        "(EGD and colonoscopy) for occult blood loss before attributing it to diet. "
        "Replace iron orally first-line; use IV iron for intolerance, malabsorption, "
        "or inadequate response. (ASH / AGA guidance.)",
    ),
    (
        ("neuropathy",),
        "Painful diabetic polyneuropathy: optimize glycemic control, then offer a "
        "first-line agent from one of four classes chosen by comorbidity and "
        "tolerability: gabapentinoids (gabapentin, pregabalin), SNRIs (duloxetine, "
        "venlafaxine), sodium-channel blockers, or tricyclic antidepressants. Topical "
        "capsaicin is an option; avoid opioids. (2022 AAN Painful Diabetic "
        "Polyneuropathy Guideline.)",
    ),
    (
        ("retinopathy",),
        "Diabetic retinopathy: ensure tight glycemic and blood-pressure control and "
        "timely ophthalmology evaluation with dilated exam. Treatment of "
        "vision-threatening disease (anti-VEGF, laser) is specialist-directed. "
        "(ADA Standards of Care 2025.)",
    ),
    (
        ("kidney", "renal", "nephropathy", "esrd"),
        "Chronic kidney disease: stage by eGFR (G1-G5) and albuminuria (A1-A3). Slow "
        "progression with a maximally tolerated ACE inhibitor or ARB for albuminuria, "
        "an SGLT2 inhibitor when eGFR is >=20, blood pressure toward <120 systolic as "
        "tolerated, and avoidance of nephrotoxins; in diabetic kidney disease add "
        "finerenone. Refer to nephrology for eGFR <30, rapid decline, or UACR >=300, "
        "and monitor potassium on RAAS/SGLT2 therapy. (KDIGO 2024.)",
    ),
    (
        ("prediabetes",),
        "Prediabetes (HbA1c 5.7-6.4%, impaired fasting glucose, or impaired glucose "
        "tolerance) is managed primarily with intensive lifestyle change: about 7% "
        "weight loss and >=150 min/week of activity, which reduces progression to "
        "diabetes. Metformin is considered mainly for the highest-risk patients "
        "(BMI >=35, age <60, prior gestational diabetes, or rising A1c). Treat "
        "cardiovascular risk factors and reassess glycemic status at least annually. "
        "Diabetes-specific drugs (SGLT2 inhibitors, GLP-1 agonists) are not indicated "
        "for prediabetes itself. (ADA Standards of Care 2025.)",
    ),
    (
        ("type 2 diabetes", "diabetes mellitus type 2"),
        "Type 2 diabetes: set an individualized A1c target (commonly <7%, relaxed for "
        "older or frail adults). Metformin plus lifestyle change is first-line. In "
        "established ASCVD, heart failure, or CKD, add an SGLT2 inhibitor and/or GLP-1 "
        "receptor agonist with proven cardiorenal benefit independent of A1c. Reassess "
        "A1c about 3 months after any change. (ADA Standards of Care 2025.)",
    ),
    (
        ("ischemic heart", "coronary", "myocardial", "cabg"),
        "Chronic coronary / ischemic heart disease: guideline-directed secondary "
        "prevention includes a high-intensity statin (LDL-C reduction >=50%, target "
        "<55-70 mg/dL for very high risk, adding ezetimibe then a PCSK9 inhibitor if "
        "above goal), antiplatelet therapy, an ACE inhibitor or ARB when there is "
        "hypertension, diabetes, or LVEF <=40%, a beta-blocker after MI or with "
        "reduced EF, and an SGLT2 inhibitor or GLP-1 agonist when diabetes coexists. "
        "Add lifestyle change and cardiac rehab. (2023 AHA/ACC Chronic Coronary "
        "Disease Guideline.)",
    ),
    (
        ("atrial fibrillation",),
        "Atrial fibrillation: estimate stroke risk with CHA2DS2-VASc and offer "
        "anticoagulation (a DOAC preferred over warfarin) at a score >=2 in men or >=3 "
        "in women. Choose rate versus rhythm control by symptoms, with early rhythm "
        "control reasonable, and treat modifiable drivers (weight, sleep apnea, "
        "alcohol, blood pressure). (2023 ACC/AHA Atrial Fibrillation Guideline.)",
    ),
    (
        ("heart failure",),
        "Heart failure with reduced ejection fraction: start the four foundational "
        "therapies (an ARNI or ACEi/ARB, a beta-blocker, an MRA, and an SGLT2 "
        "inhibitor) and titrate to target doses, with diuretics for congestion. "
        "Reassess ejection fraction and manage comorbidities. (2022 AHA/ACC Heart "
        "Failure Guideline.)",
    ),
    (
        ("hypertension",),
        "Hypertension is stage 1 at >=130/80 and stage 2 at >=140/90 mmHg, with a "
        "target <130/80 for most adults (including older adults, balanced against "
        "frailty). First-line agents are thiazide-type diuretics, ACE inhibitors or "
        "ARBs, and calcium channel blockers; begin two agents when blood pressure is "
        ">=20/10 above goal, alongside sodium restriction, the DASH diet, activity, "
        "and weight loss. (2017 ACC/AHA Hypertension Guideline.)",
    ),
    (
        ("hyperlipidemia", "cholesterol", "lipid", "dyslipidemia"),
        "Lipid management is matched to ASCVD risk. Clinical ASCVD warrants a "
        "high-intensity statin targeting >=50% LDL-C reduction (LDL <55-70 mg/dL for "
        "very high risk), adding ezetimibe and then a PCSK9 inhibitor if not at goal. "
        "For primary prevention, use 10-year risk and risk enhancers (including "
        "coronary artery calcium) to decide on a statin. (2018 AHA/ACC Blood "
        "Cholesterol Guideline.)",
    ),
    (
        ("asthma",),
        "Asthma: confirm with variable expiratory airflow limitation (spirometry with "
        "reversibility). GINA recommends ICS-containing therapy for all adults, with "
        "ICS-formoterol as the preferred reliever and maintenance (rather than "
        "SABA-only). Step up for poor control only after checking adherence, inhaler "
        "technique, and triggers; step down when stable. (GINA 2025.)",
    ),
    (
        ("obstructive", "emphysema", "copd"),
        "COPD: confirm with a post-bronchodilator FEV1/FVC <0.70, then assess by "
        "symptoms (mMRC/CAT) and exacerbation history (GOLD groups A/B/E). Start "
        "long-acting bronchodilators (LABA+LAMA for most symptomatic patients) and "
        "add an inhaled corticosteroid when blood eosinophils are high or asthma "
        "overlaps. Offer smoking cessation, vaccination, and pulmonary rehabilitation. "
        "(GOLD 2025.)",
    ),
    (
        ("anemia",),
        "Anemia: classify by MCV and the reticulocyte response and identify the cause "
        "before treating. Microcytic anemia is usually iron deficiency (confirm with "
        "ferritin and transferrin saturation) and, in an adult without an obvious "
        "source, warrants evaluation for GI blood loss. Normocytic anemia includes "
        "anemia of chronic disease and of CKD. Treat the underlying cause and "
        "transfuse based on symptoms, not a single number. (ASH guidance.)",
    ),
    (
        ("osteoarthritis",),
        "Osteoarthritis: first-line management is non-pharmacologic (exercise, weight "
        "loss, physical and occupational therapy). Preferred pharmacotherapy is "
        "topical or oral NSAIDs (topical for knee and hand); intra-articular "
        "corticosteroid injections help knee and hip flares. Opioids and "
        "intra-articular hyaluronic acid are not recommended; refer for surgery when "
        "structural disease limits function despite optimal care. (2019 ACR/Arthritis "
        "Foundation Osteoarthritis Guideline.)",
    ),
    (
        ("rheumatoid",),
        "Rheumatoid arthritis: treat to a target of low disease activity or "
        "remission. Methotrexate is the preferred initial DMARD; escalate to "
        "combination DMARDs or a biologic / targeted synthetic DMARD (such as a TNF "
        "inhibitor) if the target is not met, and minimize glucocorticoids to the "
        "lowest dose and shortest duration. (2021 ACR Rheumatoid Arthritis "
        "Guideline.)",
    ),
    (
        ("migraine",),
        "Migraine: treat acute attacks early with a triptan, NSAID, or a gepant/ditan, "
        "limiting frequency to avoid medication-overuse headache. Offer preventive "
        "therapy when attacks are frequent or disabling; first-line prevention now "
        "includes CGRP-targeting therapies (monoclonal antibodies, gepants) alongside "
        "established agents (topiramate, beta-blockers, candesartan, amitriptyline). "
        "(American Headache Society 2024.)",
    ),
    (
        ("epilepsy", "seizure"),
        "Epilepsy: classify the seizure type and syndrome before choosing an "
        "antiseizure medication, matched to seizure type, comorbidities, age, and "
        "childbearing potential. After a first unprovoked seizure, weigh recurrence "
        "risk (EEG, imaging) against treatment, and aim for monotherapy at the lowest "
        "effective dose. (AAN guidance.)",
    ),
    (
        ("obesity",),
        "Obesity: manage with intensive lifestyle intervention (nutrition, >=150 "
        "min/week activity, behavioral support). Pharmacotherapy with a GLP-1 or "
        "GIP/GLP-1 receptor agonist is indicated at BMI >=30, or >=27 with a "
        "weight-related comorbidity, and reduces cardiovascular events in patients "
        "with established cardiovascular disease. Consider bariatric surgery at BMI "
        ">=40, or >=35 with comorbidity. (Obesity management guidance.)",
    ),
]

# Used when the specific condition is not in the library above.
SPECIALTY_FALLBACK: dict[str, str] = {
    "Endocrinology": "Anchor to the patient's documented endocrine problem; "
    "optimize glycemic and metabolic control and treat cardiovascular risk "
    "factors per ADA Standards of Care.",
    "Nephrology": "Stage CKD by eGFR and albuminuria, slow progression with RAAS "
    "blockade and an SGLT2 inhibitor, and avoid nephrotoxins (KDIGO 2024).",
    "Cardiology": "Apply guideline-directed therapy for the documented cardiac "
    "problem: secondary prevention for coronary disease, BP targets for "
    "hypertension, and statin intensity matched to ASCVD risk.",
    "Pulmonology": "Confirm the diagnosis with spirometry and step therapy to "
    "symptom and exacerbation burden (GINA for asthma, GOLD for COPD).",
    "Neurology": "Classify the disorder before selecting therapy; match treatment "
    "to the specific neurologic diagnosis and comorbidities.",
    "Hematology": "Classify the cytopenia and identify the cause before treating; "
    "work up iron deficiency for a GI source in adults.",
    "Rheumatology": "Favor non-pharmacologic care plus topical/oral NSAIDs for "
    "osteoarthritis; treat inflammatory arthritis to target with DMARDs.",
}

_GENERIC = (
    "Use general best practice for the referral specialty and defer "
    "specialty-specific management to the consultant."
)


def lookup(specialty: str, condition: str = "") -> str:
    """Return the guideline for the patient's condition, falling back to a
    general specialty statement, then a generic note."""
    c = (condition or "").lower()
    for keywords, text in CONDITION_GUIDELINES:
        if any(k in c for k in keywords):
            return text
    return SPECIALTY_FALLBACK.get(specialty, _GENERIC)
