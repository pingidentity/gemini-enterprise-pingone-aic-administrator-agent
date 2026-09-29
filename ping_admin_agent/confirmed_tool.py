"""ADK confirmation gate plus a consumed server approval for A2A invocations."""

import json
from contextvars import ContextVar

from google.adk.tools import FunctionTool

# None permits ADK's native local developer confirmation UI. Every A2A model
# invocation sets a dictionary; only consumed approvals populate that dictionary.
APPROVED_CALLS = ContextVar("pingaic_approved_calls", default=None)


def fingerprint(name, args):
    return json.dumps(
        [name, {k: v for k, v in args.items() if k != "confirm"}], sort_keys=True
    )


class ConfirmedFunctionTool(FunctionTool):
    def __init__(self, func):
        super().__init__(func, require_confirmation=True)
        self._ignore_params.append("confirm")

    async def run_async(self, *, args, tool_context):
        confirmation = tool_context.tool_confirmation
        grants = APPROVED_CALLS.get()
        if confirmation and confirmation.confirmed and grants is not None:
            expected = grants.pop(tool_context.function_call_id, None)
            if expected != fingerprint(self.name, args):
                return {
                    "status": "error",
                    "message": "A current, unused approval for this exact change is required.",
                }
        return await super().run_async(
            args={**args, "confirm": True}, tool_context=tool_context
        )
