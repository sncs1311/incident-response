"""
agent/core/llm_client.py

Talks to Ollama running locally with Llama 3.
Takes raw log lines + alert context, returns a plain-English diagnosis.
"""

import json
import logging
import os
import requests
from typing import Optional

logger = logging.getLogger("incident-agent.llm")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "120"))


DIAGNOSIS_PROMPT = """You are an expert DevOps engineer and SRE on call. 
You will be given log lines and alert data from a production system.
Your job is to write a short, clear incident diagnosis.

Respond with ONLY a JSON object in this exact format:
{{
  "what_broke": "one sentence describing what failed",
  "why_it_broke": "one sentence on the likely root cause",
  "severity": "critical|warning|info",
  "try_first": "the single most important thing to try to fix it",
  "confidence": "high|medium|low"
}}

Do not include any text outside the JSON object.

SERVICE: {service}
ALERT TITLE: {title}

LOG LINES (most recent first):
{log_lines}
"""


def is_ollama_healthy() -> bool:
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def model_is_available() -> bool:
    """Check if the llama3 model is pulled and ready."""
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        if r.status_code != 200:
            return False
        models = [m["name"] for m in r.json().get("models", [])]
        return any(OLLAMA_MODEL in m for m in models)
    except Exception:
        return False


def diagnose(service: str, title: str, raw_logs: str) -> Optional[dict]:
    """
    Send logs to Ollama and get back a structured diagnosis.
    Returns a dict with keys: what_broke, why_it_broke, severity, try_first, confidence
    Returns None if Ollama is unavailable or response can't be parsed.
    """
    if not is_ollama_healthy():
        logger.warning("Ollama is not reachable — skipping diagnosis")
        return None

    # Parse raw_logs — could be a JSON string or plain text
    try:
        log_list = json.loads(raw_logs) if raw_logs else []
        if isinstance(log_list, list):
            # Each item might be a dict (structured log) or a string
            lines = []
            for item in log_list[:30]:  # Cap at 30 lines to stay within context
                if isinstance(item, dict):
                    lines.append(json.dumps(item))
                else:
                    lines.append(str(item))
            log_text = "\n".join(lines)
        else:
            log_text = str(log_list)
    except Exception:
        log_text = str(raw_logs)[:3000]  # Fallback: truncate raw string

    prompt = DIAGNOSIS_PROMPT.format(
        service=service,
        title=title,
        log_lines=log_text or "No log lines available."
    )

    logger.info(f"Sending logs to Ollama for diagnosis (service={service})")

    try:
        response = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.1,   # Low temperature = more deterministic, less creative
                    "num_predict": 300,   # Max tokens to generate
                }
            },
            timeout=OLLAMA_TIMEOUT
        )
        response.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error("Ollama request timed out")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Ollama request failed: {e}")
        return None

    raw_response = response.json().get("response", "")
    logger.debug(f"Raw LLM response: {raw_response}")

    # Parse the JSON out of the response
    # Sometimes the model wraps it in markdown fences — strip those
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()

    try:
        diagnosis = json.loads(cleaned)
        # Validate expected keys are present
        required = {"what_broke", "why_it_broke", "severity", "try_first", "confidence"}
        if not required.issubset(diagnosis.keys()):
            logger.warning(f"LLM response missing keys: {diagnosis.keys()}")
            return None
        logger.info(f"Diagnosis complete: {diagnosis['what_broke']} (confidence={diagnosis['confidence']})")
        return diagnosis
    except json.JSONDecodeError as e:
        logger.error(f"Could not parse LLM response as JSON: {e}\nResponse was: {raw_response}")
        return None


def format_diagnosis_text(diagnosis: dict) -> str:
    """Convert diagnosis dict to a readable string for storage and notifications."""
    return (
        f"WHAT BROKE: {diagnosis['what_broke']}\n"
        f"WHY: {diagnosis['why_it_broke']}\n"
        f"TRY FIRST: {diagnosis['try_first']}\n"
        f"SEVERITY: {diagnosis['severity']} | CONFIDENCE: {diagnosis['confidence']}"
    )