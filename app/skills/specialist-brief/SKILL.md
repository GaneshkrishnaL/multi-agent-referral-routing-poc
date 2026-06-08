---
name: specialist-brief
description: >
  Use this skill to write a clinician-grade Specialist Brief for an eConsult or
  virtual referral. Provides the required section structure, depth expectations,
  and grounding rules so the brief reads like real outpatient documentation a
  specialist can act on. Trigger whenever a Specialist Brief is being produced.
license: Apache-2.0
---

# Specialist Brief

Write a comprehensive, clinician-grade Specialist Brief that a specialist can act on asynchronously. Use only the facts in the provided patient context and assessment. Never invent labs, findings, or history. Do not give treatment orders, but rather frame open questions for the specialist.

All sections MUST be beautifully formatted in premium, clean Markdown. Use standard Markdown headers (###), bold styling for numbers/metrics/dates, and concise bullet points or clean list spacing so it is extremely readable and professional. Always use double newlines (\n\n) to separate sections.

## Required structure

### REASON FOR REFERRAL
A concise, professionally phrased clinical query specifying the primary clinical question driving this referral (e.g., "How should glycemic control be optimized for this 67-year-old female with uncontrolled type 2 diabetes and rising HbA1c despite maximal-dose metformin?").

### PERTINENT HISTORY
A clinical summary synthesizing the primary condition's progression, relevant comorbidities, active problems, and diagnostic findings into a cohesive story. Integrate longitudinal context from progress notes and historical records (e.g., ischemic heart disease with abnormal cardiac imaging, chronic kidney disease stages, obesity, and cardiovascular risk factors).

### CURRENT MEDICATIONS
Group the patient's active medications logically by therapeutic class (e.g., cardiovascular, anti-seizure, etc.) in a beautifully structured bulleted list. Highlight exact dosages, frequencies, and maximum-dose states.

### PERTINENT LABS & TRENDS
Analyze laboratory trends and metabolic markers over time. Present them in an incredibly readable bulleted list or table with precise dates, values, and trends. Highlight critical safety metrics (such as eGFR, liver function tests, or lipid panels) and explain their physiological implications.

### WHAT HAS BEEN TRIED / CARE CONTINUITY
A summary of prior interventions, lifestyle modification trials, clinical trials, or self-management counseling. Explicitly account for prior referral status, care compliance, and any missed appointments or no-shows.

### PERTINENT NEGATIVES
A clear section ruling out specific symptoms, red-flag indicators, complications, or absolute contraindications relevant to the referral and specialty (e.g., absence of diabetic retinopathy symptoms, no overt bleeding, or lack of cardiovascular symptoms).

## Rules

- Ground statement details strictly in the provided context; if a fact is not present in the patient record, do not invent or assume it.
- Use standard Markdown (`###` headers, bullet lists, bold numbers) and ensure there is clear double-newline spacing (`\n\n`) between all sections and list items.
- Surface longitudinal trends with precise dates and values.
- Do not make final treatment recommendations or issue dosing orders; that remains the specialist's role.
- Be comprehensive and thorough. Present a complete clinical picture.


