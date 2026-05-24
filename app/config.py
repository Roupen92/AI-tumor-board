"""Tumor-board configuration: model, specialists, limits."""
import os
from app import prompts

MODEL_NAME = os.getenv("MEDBOARD_MODEL", "gpt-5.1")

MAX_ROUNDS = 4
PARALLEL_SPECIALISTS = 2
MAX_TOOL_ITERATIONS = 12
CONSENSUS_THRESHOLD = 0.85

# Literature tools that every specialist gets.
BASE_TOOLS = {
    "pubmed_search",
    "pubmed_fetch",
    "europe_pmc_search",
    "semantic_scholar_search",
    "web_search",
}

SPECIALIST_CONFIGS = {
    "rad_onc": {
        "display_name": "Radiation Oncologist",
        "color": "#3b82f6",
        "system_prompt": prompts.RAD_ONC,
        "allowed_tools": BASE_TOOLS,
        "pubmed_bias": {"mesh_terms": ["Radiotherapy", "Dose Fractionation, Radiation"]},
    },
    "med_onc": {
        "display_name": "Medical Oncologist",
        "color": "#0ea5e9",
        "system_prompt": prompts.MED_ONC,
        "allowed_tools": BASE_TOOLS | {
            "clinical_trials_search",
            "fda_approvals_search",
            "dailymed_lookup",
        },
        "pubmed_bias": {
            "mesh_terms": [
                "Antineoplastic Agents",
                "Immunotherapy",
                "Molecular Targeted Therapy",
            ]
        },
    },
    "surg_onc": {
        "display_name": "Surgical Oncologist",
        "color": "#10b981",
        "system_prompt": prompts.SURG_ONC,
        "allowed_tools": BASE_TOOLS,
        "pubmed_bias": {
            "mesh_terms": [
                "Surgical Procedures, Operative",
                "Margins of Excision",
                "Lymph Node Excision",
            ]
        },
    },
    "pharm": {
        "display_name": "Clinical Pharmacist",
        "color": "#a855f7",
        "system_prompt": prompts.PHARM,
        "allowed_tools": BASE_TOOLS | {
            "drug_interactions",
            "fda_approvals_search",
            "dailymed_lookup",
        },
        "pubmed_bias": {
            "mesh_terms": [
                "Drug Interactions",
                "Drug-Related Side Effects and Adverse Reactions",
                "Pharmacokinetics",
            ]
        },
    },
    "molecular": {
        "display_name": "Molecular Oncologist",
        "color": "#f59e0b",
        "system_prompt": prompts.MOLECULAR,
        "allowed_tools": BASE_TOOLS | {
            "clinical_trials_search",
            "fda_approvals_search",
            "oncokb_query",
            "civic_query",
        },
        "pubmed_bias": {
            "mesh_terms": [
                "Mutation",
                "Biomarkers, Tumor",
                "Molecular Targeted Therapy",
                "Precision Medicine",
            ]
        },
        "conditional": True,        # may self-SKIP if no molecular data in case
    },
}

SPECIALIST_IDS = list(SPECIALIST_CONFIGS.keys())


def public_specialist_info() -> list[dict]:
    """Subset of config that's safe to send to the browser."""
    return [
        {"id": sid, "display_name": cfg["display_name"], "color": cfg["color"]}
        for sid, cfg in SPECIALIST_CONFIGS.items()
    ]
