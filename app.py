"""
Flask UI Application for ACME Corp Candidate Agent Evaluation.
Provides:
  - Interactive WhatsApp-style chat interface
  - Live Candidate DB state inspector
  - Real-time LLM decision & tool call timeline
"""
import os
import json
from pathlib import Path
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

import api_client as db
from validator import validate_field

import google.generativeai as genai

genai.configure(api_key=os.environ["GEMINI_API_KEY"])

app = Flask(__name__)

# Active session state storage
SESSION = {
    "candidate_id": None,
    "candidate": None,
    "missing": [],
    "chat_history": [],      # [{"role": "agent"|"candidate", "text": "..."}]
    "timeline": [],          # [{"type": "tool"|"decision"|"api", "title": "...", "detail": ...}]
    "staged": {},
    "finished": False,
    "gemini_chat": None,
}

from agent import TOOLS, build_system_prompt

def init_agent_session(candidate_id: str):
    cand = db.get_candidate(candidate_id)
    missing = db.missing_fields(cand)
    
    SESSION["candidate_id"] = candidate_id
    SESSION["candidate"] = cand
    SESSION["missing"] = missing
    SESSION["chat_history"] = []
    SESSION["timeline"] = [
        {
            "type": "decision",
            "title": f"Fetched Candidate: {cand.get('full_name')}",
            "detail": f"Status: {cand.get('collection_status')} | Missing fields: {missing if missing else 'None'}"
        }
    ]
    SESSION["staged"] = {}
    SESSION["finished"] = False

    if not missing:
        SESSION["finished"] = True
        SESSION["timeline"].append({
            "type": "decision",
            "title": "Application Already Complete",
            "detail": "All mandatory fields are filled. No outreach conversation needed."
        })
        SESSION["chat_history"].append({
            "role": "agent",
            "text": f"Hello {cand.get('full_name')}! Your application with ACME Corp is already complete and verified. No further information is needed at this time."
        })
        SESSION["gemini_chat"] = None
        return

    # Initialize Gemini model with tools
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
        model_name="gemini-3.5-flash-lite",
        system_instruction=build_system_prompt(cand, missing),
        tools=[tools_spec],
        generation_config=genai.GenerationConfig(temperature=0.3),
    )

    chat = model.start_chat(enable_automatic_function_calling=False)
    SESSION["gemini_chat"] = chat

    # Start conversation
    _run_gemini_turn("Begin the conversation now. Greet the candidate by name and confirm identity.")


def execute_tool_call(tool_name: str, tool_args: dict):
    cand_id = SESSION["candidate_id"]

    if tool_name == "get_candidate_info":
        cand = db.get_candidate(cand_id)
        SESSION["candidate"] = cand
        missing = db.missing_fields(cand)
        res = {"candidate": cand, "missing": missing, "complete": len(missing) == 0}
        SESSION["timeline"].append({
            "type": "tool",
            "title": "Tool: get_candidate_info",
            "detail": res
        })
        return res

    elif tool_name == "validate_and_stage_field":
        field = tool_args.get("field")
        value = tool_args.get("value")
        res = validate_field(field, value)
        if res["ok"]:
            SESSION["staged"][field] = res["coerced"]
        SESSION["timeline"].append({
            "type": "tool",
            "title": f"Tool: validate_and_stage_field ('{field}')",
            "detail": res
        })
        return res

    elif tool_name == "update_candidate":
        updates = dict(tool_args.get("updates", {}))
        updates.update(SESSION["staged"])
        SESSION["staged"].clear()

        if "collection_status" in tool_args and tool_args["collection_status"]:
            updates["collection_status"] = tool_args["collection_status"]
        if "notes" in tool_args and tool_args["notes"]:
            updates["notes"] = tool_args["notes"]

        current = SESSION["candidate"] or db.get_candidate(cand_id)
        updates["attempt_count"] = (current.get("attempt_count") or 0) + 1
        updates["last_contacted_at"] = db.now_iso()
        version = current.get("version", 1)

        try:
            updated = db.patch_candidate(cand_id, updates, version)
            SESSION["candidate"] = updated
            SESSION["missing"] = db.missing_fields(updated)
            SESSION["timeline"].append({
                "type": "api",
                "title": f"API PATCH /v1/candidates/{cand_id}",
                "detail": {"updates": updates, "version_sent": version, "response": updated}
            })
            return {"success": True, "updated": updated}
        except db.ConflictError:
            current = db.get_candidate(cand_id)
            updated = db.patch_candidate(cand_id, updates, current["version"])
            SESSION["candidate"] = updated
            SESSION["missing"] = db.missing_fields(updated)
            SESSION["timeline"].append({
                "type": "api",
                "title": f"API PATCH (Retry after 409) /v1/candidates/{cand_id}",
                "detail": {"updates": updates, "version_sent": current["version"], "response": updated}
            })
            return {"success": True, "updated": updated, "recovered": True}
        except Exception as e:
            SESSION["timeline"].append({
                "type": "api",
                "title": f"API PATCH Failed: {e}",
                "detail": str(e)
            })
            return {"error": str(e)}

    elif tool_name == "finish_conversation":
        status = tool_args.get("status")
        notes = tool_args.get("notes", "")
        current = SESSION["candidate"] or db.get_candidate(cand_id)
        
        updates = {
            "collection_status": status,
            "last_contacted_at": db.now_iso(),
            "attempt_count": (current.get("attempt_count") or 0) + 1,
        }
        if notes:
            updates["notes"] = notes
        updates.update(SESSION["staged"])
        SESSION["staged"].clear()

        version = current.get("version", 1)
        try:
            updated = db.patch_candidate(cand_id, updates, version)
            SESSION["candidate"] = updated
            SESSION["missing"] = db.missing_fields(updated)
        except Exception as e:
            pass

        SESSION["finished"] = True
        SESSION["timeline"].append({
            "type": "decision",
            "title": f"Session Finished: status = {status}",
            "detail": {"status": status, "notes": notes}
        })
        return {"done": True, "status": status}

    return {"error": f"Unknown tool: {tool_name}"}


def _run_gemini_turn(prompt_or_response):
    chat = SESSION.get("gemini_chat")
    if not chat:
        return

    response = chat.send_message(prompt_or_response)

    # Process until plain text for user is emitted
    max_tool_hops = 6
    hops = 0

    while hops < max_tool_hops:
        tool_call_to_exec = None
        for part in response.parts:
            if part.function_call:
                tool_call_to_exec = part.function_call
                break

        if tool_call_to_exec:
            tname = tool_call_to_exec.name
            targs = dict(tool_call_to_exec.args)
            result = execute_tool_call(tname, targs)

            tool_resp = genai.protos.Part(
                function_response=genai.protos.FunctionResponse(
                    name=tname,
                    response={"result": json.dumps(result, default=str)}
                )
            )
            response = chat.send_message(tool_resp)
            hops += 1
            continue

        # No more tool calls, extract text for the candidate
        for part in response.parts:
            if part.text and part.text.strip():
                SESSION["chat_history"].append({
                    "role": "agent",
                    "text": part.text.strip()
                })
        break


@app.route("/")
def index():
    candidates = db.list_candidates()
    return render_template("index.html", candidates=candidates)


@app.route("/api/candidates", methods=["GET"])
def get_candidates():
    candidates = db.list_candidates()
    return jsonify({"candidates": candidates})


@app.route("/api/select_candidate", methods=["POST"])
def select_candidate():
    data = request.json or {}
    cid = data.get("candidate_id")
    if not cid:
        return jsonify({"error": "candidate_id required"}), 400
    try:
        init_agent_session(cid)
        return jsonify({
            "candidate": SESSION["candidate"],
            "missing": SESSION["missing"],
            "chat_history": SESSION["chat_history"],
            "timeline": SESSION["timeline"],
            "finished": SESSION["finished"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/send_message", methods=["POST"])
def send_message():
    data = request.json or {}
    text = (data.get("message") or "").strip()
    if not text:
        return jsonify({"error": "Empty message"}), 400

    SESSION["chat_history"].append({"role": "candidate", "text": text})

    if text.lower() in ("/quit", "/done", "/exit"):
        SESSION["timeline"].append({
            "type": "decision",
            "title": "Candidate Requested /quit",
            "detail": "Ending conversation and finalizing status."
        })
        _run_gemini_turn("The candidate typed /quit and ended the chat. Save any staged progress and call finish_conversation.")
    else:
        _run_gemini_turn(text)

    # Refresh candidate record
    try:
        if SESSION["candidate_id"]:
            SESSION["candidate"] = db.get_candidate(SESSION["candidate_id"])
            SESSION["missing"] = db.missing_fields(SESSION["candidate"])
    except Exception:
        pass

    return jsonify({
        "candidate": SESSION["candidate"],
        "missing": SESSION["missing"],
        "chat_history": SESSION["chat_history"],
        "timeline": SESSION["timeline"],
        "finished": SESSION["finished"],
    })


@app.route("/api/reset_workspace", methods=["POST"])
def reset_workspace():
    try:
        db.reset_workspace()
        if SESSION.get("candidate_id"):
            init_agent_session(SESSION["candidate_id"])
        return jsonify({"success": True, "message": "Workspace successfully reset to initial state!"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("\n" + "="*60)
    print("🚀 ACME Corp Agent Evaluation Server running!")
    print("👉 Open in browser: http://127.0.0.1:5000")
    print("="*60 + "\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
