import re
import json
import logging
import sys
from typing import Any
from pydantic import BaseModel, Field

from google.adk.workflow import Workflow, Edge, START, node
from google.adk.agents import LlmAgent, Context
from google.adk.tools import AgentTool, request_input, McpToolset
from google.adk.tools.mcp_tool import StdioConnectionParams
from google.adk.models import Gemini
from google.adk.apps import App
from google.genai import types
from mcp import StdioServerParameters
from .config import config
from .mcp_server import save_clinical_note

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("care_connect")

# EHR State Schema
class CareConnectState(BaseModel):
    patient_query: str = ""
    patient_name: str = "Anonymous"
    patient_id: str = "Unknown"
    triage_level: str = ""
    triage_notes: str = ""
    clinical_summary: str = ""
    doctor_approval: bool = False
    doctor_feedback: str = ""
    security_passed: bool = True
    security_violation_reason: str = ""

# Setup McpToolset
mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_server"],
        )
    )
)

# Specialized Agents with McpToolset wired in
symptom_triage_agent = LlmAgent(
    name="symptom_triage_agent",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=(
        "You are an expert Clinical Triage Assistant. Analyze the patient's symptoms "
        "carefully. Determine the severity level of the situation and assign one of the "
        "following triage categories: EMERGENCY, URGENT, or ROUTINE.\n"
        "- EMERGENCY: Severe chest pain, extreme breathing difficulty, sudden weakness/paralysis, heavy bleeding.\n"
        "- URGENT: High fever, moderate pain, minor breathing trouble, possible fractures.\n"
        "- ROUTINE: Mild cold symptoms, minor cuts, chronic check-ups, general health questions.\n\n"
        "You can use lookup_patient_record to check the patient's medical history when they provide their patient ID.\n"
        "Provide a concise summary of the symptoms and a clear explanation of your triage decision."
    ),
    description="Clinical Triage Assistant for symptom assessment.",
    tools=[mcp_toolset]
)

doctor_comm_agent = LlmAgent(
    name="doctor_comm_agent",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=(
        "You are a professional Medical Documentation Specialist. Your task is to compile the patient's "
        "symptoms and triage assessment into a structured EHR clinical note.\n"
        "The note MUST include:\n"
        "- Patient Name & ID\n"
        "- Reported Symptoms\n"
        "- Triage Level (EMERGENCY, URGENT, ROUTINE)\n"
        "- Clinical Reasoning / Notes\n"
        "- A signature placeholder for the reviewing physician.\n\n"
        "You can use get_doctor_schedule to check doctor availability, and lookup_patient_record "
        "to check patient information. Keep the format professional, concise, and clinical."
    ),
    description="Medical Documentation Specialist for drafting clinical summaries.",
    tools=[mcp_toolset]
)

# Orchestrator Agent
care_orchestrator = LlmAgent(
    name="care_orchestrator",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=(
        "You are the Care Connect Coordinator. You coordinate between the patient "
        "and specialized clinical agents.\n"
        "When a patient query is received, you must:\n"
        "1. Call symptom_triage_agent to analyze symptoms and get a triage category and notes.\n"
        "2. Call doctor_comm_agent with the triage results to draft a professional clinical note.\n"
        "3. Return the drafted clinical note as your final response.\n"
        "Do not write the clinical notes yourself; rely on the specialized sub-agents."
    ),
    tools=[
        AgentTool(agent=symptom_triage_agent),
        AgentTool(agent=doctor_comm_agent)
    ]
)

# Workflow Nodes
@node
async def security_checkpoint(ctx: Context, node_input: str) -> str:
    """Security Checkpoint: Scrub PII and detect prompt injection."""
    query = node_input or ""
    
    # 1. PII Scrubbing (Regex)
    # Replaces Phone numbers, Emails, and SSNs
    email_pattern = r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+'
    phone_pattern = r'\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'
    ssn_pattern = r'\b\d{3}-\d{2}-\d{4}\b'
    
    scrubbed = query
    scrubbed = re.sub(email_pattern, "[EMAIL_REDACTED]", scrubbed)
    scrubbed = re.sub(phone_pattern, "[PHONE_REDACTED]", scrubbed)
    scrubbed = re.sub(ssn_pattern, "[SSN_REDACTED]", scrubbed)
    
    # Extract name/ID if present in query for State (e.g. "My name is John Doe, ID P-102")
    name_match = re.search(r'(?:my name is|i am)\s+([a-zA-Z\s]+?)(?:,|\.|\b)', query, re.IGNORECASE)
    id_match = re.search(r'(?:id|patient id)\s+([a-zA-Z0-9-]+)\b', query, re.IGNORECASE)
    
    if name_match:
        ctx.state["patient_name"] = name_match.group(1).strip()
    if id_match:
        ctx.state["patient_id"] = id_match.group(1).strip()
        
    # 2. Prompt Injection Detection
    injection_keywords = ["ignore previous", "system prompt", "you are now", "override instruction"]
    has_injection = any(kw in query.lower() for kw in injection_keywords)
    
    # Audit Log Entry
    audit_entry = {
        "event": "security_scan",
        "pii_detected": scrubbed != query,
        "injection_detected": has_injection,
        "patient_id": ctx.state.get("patient_id", "Unknown"),
        "severity": "CRITICAL" if has_injection else "INFO"
    }
    logger.info(f"AUDIT_LOG: {json.dumps(audit_entry)}")
    
    if has_injection:
        ctx.state["security_passed"] = False
        ctx.state["security_violation_reason"] = "Prompt injection attempt detected."
        ctx.route = "security_violation"
        return "Security Violation"
        
    # 3. Domain-Specific Rule: Patient ID Validation (must start with P- followed by digits)
    patient_id = ctx.state.get("patient_id", "Unknown")
    if patient_id != "Unknown":
        if not re.match(r'^P-\d+$', patient_id):
            ctx.state["security_passed"] = False
            ctx.state["security_violation_reason"] = f"Invalid Patient ID format: {patient_id}. Format must be P-XXX (e.g., P-102)."
            ctx.route = "security_violation"
            audit_entry["severity"] = "WARNING"
            audit_entry["domain_rule_violated"] = True
            logger.info(f"AUDIT_LOG: {json.dumps(audit_entry)}")
            return "Security Violation"
        
    ctx.state["patient_query"] = scrubbed
    ctx.route = "run_orchestration"
    return scrubbed

@node(rerun_on_resume=True)
async def orchestration_node(ctx: Context, node_input: str) -> str:
    """Orchestrates triage and clinical note preparation."""
    # Run the coordinator agent
    patient_query = ctx.state.get("patient_query", "")
    result = await ctx.run_node(care_orchestrator, node_input=patient_query)
    
    # Save the note
    ctx.state["clinical_summary"] = result
    
    # Parse the triage level from the result text
    triage_level = "ROUTINE"
    if "EMERGENCY" in result.upper():
        triage_level = "EMERGENCY"
    elif "URGENT" in result.upper():
        triage_level = "URGENT"
        
    ctx.state["triage_level"] = triage_level
    ctx.route = "review"
    return result

@node(rerun_on_resume=True)
async def human_approval_node(ctx: Context, node_input: str) -> str:
    """HITL: Ask reviewing physician for approval of clinical note."""
    patient_name = ctx.state.get("patient_name", "Anonymous")
    patient_id = ctx.state.get("patient_id", "Unknown")
    triage_level = ctx.state.get("triage_level", "")
    clinical_summary = ctx.state.get("clinical_summary", "")
    
    message = (
        f"Review needed for patient {patient_name} (ID: {patient_id}).\n"
        f"Triage Level: {triage_level}\n"
        f"Drafted Note:\n{clinical_summary}\n\n"
        "Do you approve this note? Reply with JSON having 'approved' (boolean) and 'feedback' (string)."
    )
    
    approval_result = await ctx.run_node(
        request_input,
        node_input={
            "message": message,
            "response_schema": {
                "type": "object",
                "properties": {
                    "approved": {"type": "boolean"},
                    "feedback": {"type": "string"}
                },
                "required": ["approved"]
            }
        }
    )
    
    # Parse response
    if isinstance(approval_result, str):
        try:
            data = json.loads(approval_result)
        except Exception:
            data = {"approved": False, "feedback": "Failed to parse doctor input."}
    else:
        data = approval_result or {"approved": False, "feedback": ""}
        
    ctx.state["doctor_approval"] = data.get("approved", False)
    ctx.state["doctor_feedback"] = data.get("feedback", "")
    
    return "Reviewed"

@node
async def final_output_node(ctx: Context, node_input: str) -> str:
    """Prepares the final recommendation and response to the client."""
    security_passed = ctx.state.get("security_passed", True)
    security_violation_reason = ctx.state.get("security_violation_reason", "")
    doctor_approval = ctx.state.get("doctor_approval", False)
    patient_id = ctx.state.get("patient_id", "Unknown")
    clinical_summary = ctx.state.get("clinical_summary", "")
    doctor_feedback = ctx.state.get("doctor_feedback", "")
    
    if not security_passed:
        return f"ACCESS DENIED: {security_violation_reason}"
        
    if doctor_approval:
        # Call MCP save tool directly
        save_msg = save_clinical_note(patient_id, clinical_summary)
        
        return (
            f"✅ Clinical note approved by physician.\n"
            f"EHR Status: {save_msg}\n"
            f"Feedback: {doctor_feedback or 'None'}\n\n"
            f"--- Final EHR Clinical Note ---\n"
            f"{clinical_summary}"
        )
    else:
        return (
            f"❌ Clinical note was REJECTED by reviewing physician.\n"
            f"Feedback: {doctor_feedback}\n"
            f"Please update the patient query or address the physician feedback."
        )

# Compile Workflow Graph
workflow = Workflow(
    name="care_connect_workflow",
    state_schema=CareConnectState,
    edges=[
        Edge(from_node=START, to_node=security_checkpoint),
        Edge(from_node=security_checkpoint, to_node=orchestration_node, route="run_orchestration"),
        Edge(from_node=security_checkpoint, to_node=final_output_node, route="security_violation"),
        Edge(from_node=orchestration_node, to_node=human_approval_node, route="review"),
        Edge(from_node=human_approval_node, to_node=final_output_node),
    ]
)

# Export App
app = App(
    root_agent=workflow,
    name="app",
)
