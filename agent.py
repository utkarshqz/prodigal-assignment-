"""
Core AI Agent — uses Google Gemini with function calling to drive the
candidate info-collection conversation.

Architecture:
  agent_loop()
    ↓ fetch candidate, compute missing fields
    ↓ if nothing missing → exit (no conversation)
    ↓ build system prompt with candidate context
    ↓ Gemini chat with tool functions:
        • get_candidate_info   – re-read candidate from API
        • update_candidate     – PATCH with validated data
        • finish_conversation  – set final status and exit
    ↓ terminal chat loop: agent prints, human types
    ↓ on exit → PATCH final status + write run log
"""
import os
import sys
import json
import argparse
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import google.generativeai as genai
from dotenv import load_dotenv

import api_client as db
from validator import validate_field, MANDATORY_FIELDS

load_dotenv()

genai.configure(api_key=os.environ["GEMINI_API_KEY"])

# ── Tool definitions (function calling) ──────────────────────────────────────

TOOLS = [
    {
        "name": "get_candidate_info",
        "description": (
            "Fetch the current state of the candidate record from the database. "
            "Call this to refresh data or check what fields are still missing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "candidate_id": {
                    "type": "string",
                    "description": "The candidate's ID, e.g. c001"
                }
            },
            "required": ["candidate_id"]
        }
    },
    {
        "name": "validate_and_stage_field",
        "description": (
            "Validate a single field value the candidate just provided. "
            "Returns whether it is valid, an error message if not, and the coerced/normalised value. "
            "Call this before updating the database."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "description": "Field name, e.g. 'email', 'cgpa', 'degree'"
                },
                "value": {
                    "type": "string",
                    "description": "The raw value from the candidate's message"
                }
            },
            "required": ["field", "value"]
        }
    },
    {
        "name": "update_candidate",
        "description": (
            "Write validated fields back to the database. "
            "Only pass fields that have been validated. "
            "Also updates collection_status, attempt_count, last_contacted_at and notes if provided."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "candidate_id": {
                    "type": "string",
                    "description": "Candidate ID"
                },
                "updates": {
                    "type": "object",
                    "description": "Dict of field → validated value to write"
                },
                "collection_status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "no_answer", "refused", "needs_human"],
                    "description": "Current collection status"
                },
                "notes": {
                    "type": "string",
                    "description": "Optional notes for a human agent or the next conversation"
                }
            },
            "required": ["candidate_id", "updates"]
        }
    },
    {
        "name": "finish_conversation",
        "description": (
            "End the conversation and record the final outcome. "
            "Call this when the conversation is complete, the candidate has left, "
            "refused to continue, or needs human assistance."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "candidate_id": {
                    "type": "string"
                },
                "status": {
                    "type": "string",
                    "enum": ["completed", "in_progress", "no_answer", "refused", "needs_human"],
                    "description": "Final outcome status"
                },
                "notes": {
                    "type": "string",
                    "description": "What happened, what's still needed, any context for a human"
                }
            },
            "required": ["candidate_id", "status"]
        }
    }
]

# ── Tool executor ─────────────────────────────────────────────────────────────

class AgentState:
    def __init__(self, candidate_id: str):
        self.candidate_id = candidate_id
        self.candidate = None
        self.finished = False
        self.final_status = None
        self.staged = {}           # validated values waiting to be written
        self.tool_log = []         # for the run file
        self.chat_log = []         # (role, text) pairs


state: AgentState = None   # set by agent_loop


def execute_tool(name: str, args: dict) -> str:
    global state
    state.tool_log.append({"tool": name, "args": args})

    if name == "get_candidate_info":
        try:
            cand = db.get_candidate(args["candidate_id"])
            state.candidate = cand
            missing = db.missing_fields(cand)
            result = {
                "candidate": cand,
                "missing_mandatory_fields": missing,
                "complete": len(missing) == 0
            }
            state.tool_log[-1]["result"] = result
            return json.dumps(result)
        except Exception as e:
            err = {"error": str(e)}
            state.tool_log[-1]["result"] = err
            return json.dumps(err)

    elif name == "validate_and_stage_field":
        field = args["field"]
        value = args["value"]
        result = validate_field(field, value)
        if result["ok"]:
            state.staged[field] = result["coerced"]
        state.tool_log[-1]["result"] = result
        return json.dumps(result)

    elif name == "update_candidate":
        cand_id = args["candidate_id"]
        updates = dict(args.get("updates", {}))

        # merge any staged values
        updates.update(state.staged)
        state.staged.clear()

        if not updates and not args.get("collection_status") and not args.get("notes"):
            return json.dumps({"skipped": "no updates to write"})

        # always stamp last_contacted_at
        updates["last_contacted_at"] = db.now_iso()

        if "collection_status" in args and args["collection_status"]:
            updates["collection_status"] = args["collection_status"]

        if "notes" in args and args.get("notes"):
            updates["notes"] = args["notes"]

        # Increment attempt_count
        current = state.candidate or db.get_candidate(cand_id)
        updates["attempt_count"] = (current.get("attempt_count") or 0) + 1
        version = current.get("version", 1)

        try:
            updated = db.patch_candidate(cand_id, updates, version)
            state.candidate = updated
            result = {"success": True, "updated": updated}
            state.tool_log[-1]["result"] = result
            return json.dumps(result)
        except db.ConflictError as e:
            # re-read and retry once
            try:
                current = db.get_candidate(cand_id)
                state.candidate = current
                updated = db.patch_candidate(cand_id, updates, current["version"])
                state.candidate = updated
                result = {"success": True, "updated": updated, "note": "retried after 409"}
                state.tool_log[-1]["result"] = result
                return json.dumps(result)
            except Exception as e2:
                err = {"error": str(e2)}
                state.tool_log[-1]["result"] = err
                return json.dumps(err)
        except Exception as e:
            err = {"error": str(e)}
            state.tool_log[-1]["result"] = err
            return json.dumps(err)

    elif name == "finish_conversation":
        cand_id = args["candidate_id"]
        status = args["status"]
        notes = args.get("notes", "")

        current = state.candidate or db.get_candidate(cand_id)
        updates = {
            "collection_status": status,
            "last_contacted_at": db.now_iso(),
            "attempt_count": (current.get("attempt_count") or 0) + 1,
        }
        if notes:
            updates["notes"] = notes

        # Flush any remaining staged values
        updates.update(state.staged)
        state.staged.clear()

        version = current.get("version", 1)
        try:
            db.patch_candidate(cand_id, updates, version)
        except db.ConflictError:
            current = db.get_candidate(cand_id)
            updates["attempt_count"] = (current.get("attempt_count") or 0) + 1
            db.patch_candidate(cand_id, updates, current["version"])
        except Exception as e:
            print(f"\n⚠️  Could not write final status: {e}")

        state.finished = True
        state.final_status = status
        result = {"done": True, "status": status}
        state.tool_log[-1]["result"] = result
        return json.dumps(result)

    return json.dumps({"error": f"Unknown tool: {name}"})


# ── System prompt builder ─────────────────────────────────────────────────────

def build_system_prompt(candidate: dict, missing: list[str]) -> str:
    name = candidate.get("full_name", "the candidate")
    existing = {k: v for k, v in candidate.items() if v is not None and k not in
                ("id", "phone", "version", "updated_at", "collection_status",
                 "attempt_count", "last_contacted_at", "notes")}

    return textwrap.dedent(f"""
    You are a friendly, professional outreach assistant for ACME Corp's internship programme.
    You are having a WhatsApp-style chat with a candidate to complete their application.

    CANDIDATE ID: {candidate["id"]}
    CANDIDATE NAME: {name}

    EXISTING DATA (already on file — do NOT re-ask unless it looks wrong):
    {json.dumps(existing, indent=2)}

    FIELDS STILL NEEDED (mandatory — you must collect all of them):
    {json.dumps(missing)}

    FIELD RULES (enforce strictly before writing):
    - email: valid email address
    - degree: one of B.Tech, B.E., B.Sc, BCA, MBA Tech, MCA, M.Tech, Other
    - graduation_year: integer 2026–2028
    - cgpa: number 0.0–10.0 on a 10-point scale (2 decimal places stored)
    - preferred_location: one of Mumbai, Bengaluru, Remote, No preference
    - available_from: date YYYY-MM-DD between 2026-01-01 and 2027-12-31
    - internship_duration_months: one of 2, 3, 6
    - college: 2–150 chars
    - branch: 2–100 chars (specialisation, e.g. Computer Engineering)

    BEHAVIOUR RULES:
    1. Confirm you're talking to the right person by greeting them by name. NEVER ask for an ID.
    2. Collect missing fields conversationally — don't fire a form at them.
    3. Group related questions when natural (e.g. college + degree + branch together).
    4. Always call validate_and_stage_field before writing any value.
    5. If a value is invalid, ask the candidate to correct it and explain why.
    6. Save progress mid-chat with update_candidate (status=in_progress).
    7. If the candidate says "continue later" / stops replying → finish_conversation(status=in_progress, notes=what's left).
    8. If the candidate refuses a field → note it, still try to collect others, then finish with refused if applicable.
    9. If you detect a conflict or the candidate disputes existing data → finish with needs_human and explain.
    10. When ALL mandatory fields are collected and validated → update_candidate, then finish_conversation(completed).
    11. Keep messages short and friendly, like a real WhatsApp chat.
    12. The candidate can type /quit or /done to exit early.

    EXISTING DATA INTEGRITY:
    - If existing data looks suspicious (e.g. CGPA > 10, graduation_year < 2026), gently confirm it.
    - Do NOT overwrite clean existing values without the candidate correcting them.

    Start by greeting the candidate warmly and confirming their identity, then naturally
    ask for the first missing piece of information.
    """).strip()


# ── Main agent loop ───────────────────────────────────────────────────────────

def agent_loop(candidate_id: str, run_output_dir: Path | None = None):
    global state
    state = AgentState(candidate_id)

    # 1. Fetch candidate
    print(f"\n🔍 Fetching candidate {candidate_id}...")
    try:
        candidate = db.get_candidate(candidate_id)
        state.candidate = candidate
    except Exception as e:
        print(f"❌ Could not fetch candidate {candidate_id}: {e}")
        sys.exit(1)

    missing = db.missing_fields(candidate)
    name = candidate.get("full_name", candidate_id)

    print(f"👤 Candidate: {name}")
    print(f"📋 Missing fields: {missing if missing else '(none — record is complete)'}")

    # 2. If nothing missing, skip
    if not missing:
        print(f"\n✅ {name}'s application is already complete. No conversation needed.")
        return

    # 3. Build model with function calling
    tools_spec = genai.protos.Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name=t["name"],
                description=t["description"],
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        k: genai.protos.Schema(
                            type=genai.protos.Type.STRING
                            if v.get("type") == "string"
                            else (genai.protos.Type.OBJECT if v.get("type") == "object" else genai.protos.Type.STRING),
                            description=v.get("description", ""),
                            enum=v.get("enum", []) or [],
                        )
                        for k, v in t["parameters"].get("properties", {}).items()
                    },
                    required=t["parameters"].get("required", []),
                )
            )
            for t in TOOLS
        ]
    )

    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=build_system_prompt(candidate, missing),
        tools=[tools_spec],
        generation_config=genai.GenerationConfig(temperature=0.4),
    )

    chat = model.start_chat(enable_automatic_function_calling=False)

    print("\n" + "═"*60)
    print("💬  CHAT SESSION STARTED")
    print("    Type your replies below. Type /quit or /done to exit.")
    print("═"*60 + "\n")

    def handle_model_turn(response):
        """Process a model response: print text parts and execute function calls."""
        for part in response.parts:
            if part.text:
                agent_text = part.text.strip()
                if agent_text:
                    print(f"\n🤖 Agent: {agent_text}\n")
                    state.chat_log.append({"role": "agent", "text": agent_text})
            elif part.function_call:
                fc = part.function_call
                tool_name = fc.name
                tool_args = dict(fc.args)
                print(f"   [🔧 Tool: {tool_name}({json.dumps(tool_args, default=str)[:80]}...)]")
                tool_result = execute_tool(tool_name, tool_args)
                # Feed tool result back
                tool_response_part = genai.protos.Part(
                    function_response=genai.protos.FunctionResponse(
                        name=tool_name,
                        response={"result": tool_result},
                    )
                )
                return tool_response_part
        return None

    # 4. Kick off the conversation
    response = chat.send_message("Begin the conversation now.")

    # Agentic loop: keep processing until finished or no more function calls
    while not state.finished:
        tool_response_part = handle_model_turn(response)

        if state.finished:
            break

        if tool_response_part:
            # Send tool result back to model
            response = chat.send_message(tool_response_part)
            continue

        # Model produced text — wait for human input
        user_input = input("You: ").strip()
        if not user_input:
            continue

        state.chat_log.append({"role": "candidate", "text": user_input})

        if user_input.lower() in ("/quit", "/done", "/exit"):
            print("\n[Candidate ended the session]")
            # Let the model handle the graceful close
            response = chat.send_message(
                "The candidate has typed /quit and ended the chat. "
                "Save any collected data and call finish_conversation with the appropriate status."
            )
            continue

        response = chat.send_message(user_input)

    # 5. Final summary
    print("\n" + "═"*60)
    print(f"📌 Conversation ended. Final status: {state.final_status}")
    print("═"*60)

    # 6. Save run log
    if run_output_dir:
        run_output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_file = run_output_dir / f"{candidate_id}_{ts}.json"
        run_data = {
            "candidate_id": candidate_id,
            "candidate_name": name,
            "timestamp": db.now_iso(),
            "final_status": state.final_status,
            "chat_log": state.chat_log,
            "tool_calls": state.tool_log,
        }
        run_file.write_text(json.dumps(run_data, indent=2, default=str))
        print(f"📄 Run saved to: {run_file}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="ACME Corp Internship Application Completion Agent"
    )
    parser.add_argument(
        "--candidate-id", "-c",
        required=True,
        help="Candidate ID to process (e.g. c001)"
    )
    parser.add_argument(
        "--runs-dir", "-r",
        default="runs",
        help="Directory to save run logs (default: runs/)"
    )
    args = parser.parse_args()

    if not os.getenv("GEMINI_API_KEY"):
        print("❌ GEMINI_API_KEY not set. Copy .env.example → .env and fill it in.")
        sys.exit(1)
    if not os.getenv("CANDIDATE_API_KEY"):
        print("❌ CANDIDATE_API_KEY not set. Copy .env.example → .env and fill it in.")
        sys.exit(1)

    agent_loop(
        candidate_id=args.candidate_id,
        run_output_dir=Path(args.runs_dir)
    )


if __name__ == "__main__":
    main()
