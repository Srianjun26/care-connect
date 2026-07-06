from mcp.server.fastmcp import FastMCP
import os

mcp = FastMCP("CareConnect")

@mcp.tool()
def lookup_patient_record(patient_id: str) -> str:
    """Retrieves patient medical history and metadata.
    
    Args:
        patient_id: The unique ID of the patient (e.g. P-101).
    """
    records = {
        "P-101": "Patient: Alice Smith. Age: 34. History: Hypertension, Asthma. Allergies: Penicillin.",
        "P-102": "Patient: John Doe. Age: 45. History: Type 2 Diabetes. Allergies: Sulfa drugs.",
        "P-103": "Patient: Emma Johnson. Age: 28. History: None. Allergies: None."
    }
    return records.get(patient_id, f"Patient with ID {patient_id} not found in the EHR database.")

@mcp.tool()
def save_clinical_note(patient_id: str, note: str) -> str:
    """Saves a drafted and approved EHR clinical note to the local medical database.
    
    Args:
        patient_id: The patient ID to associate with the note.
        note: The clinical note content.
    """
    db_path = "clinical_notes.db.txt"
    # Ensure directory is writable and files are written to project directory
    with open(db_path, "a", encoding="utf-8") as f:
        f.write(f"=== PATIENT: {patient_id} ===\n{note}\n\n")
    return f"Successfully saved clinical note to {db_path}."

@mcp.tool()
def get_doctor_schedule() -> str:
    """Retrieves the schedules of on-call primary care physicians."""
    return (
        "On-Call Schedule today:\n"
        "- Dr. Sarah Jenkins (Cardiology): 08:00 - 16:00\n"
        "- Dr. Robert Carter (Pediatrics): 12:00 - 20:00\n"
        "- Dr. Evelyn Zhao (General Medicine): 16:00 - 24:00"
    )

if __name__ == "__main__":
    mcp.run(transport="stdio")
