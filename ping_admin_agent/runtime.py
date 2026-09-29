"""Serializable A2A service deployed to the customer's managed Agent Runtime."""

from __future__ import annotations

from copy import deepcopy

from a2a.types import AgentCard
from starlette.responses import JSONResponse
from starlette.routing import Route
from vertexai.agent_engines.templates.a2a import A2aAgent

from .a2a import (
    A2uiExtensionHeaders,
    build_agent_card,
    build_executor,
    gemini_agent_card,
)
from .ui_runtime import UiRequestHandler
from .ui_store import UiTaskStore


class PingAdminA2aAgent(A2aAgent):
    def __init__(self, *, agent_card: AgentCard | None = None):
        super().__init__(
            agent_card=agent_card or build_agent_card("http://localhost:8080/a2a"),
            agent_executor_builder=build_executor,
            task_store_builder=UiTaskStore,
        )

    def clone(self) -> PingAdminA2aAgent:
        # The SDK clones before packaging. Its base clone returns A2aAgent,
        # which would otherwise discard our URL and card-route fixes.
        return type(self)(agent_card=deepcopy(self.agent_card))

    def set_up(self) -> None:
        super().set_up()
        self.request_handler = UiRequestHandler(
            agent_executor=self.agent_executor,
            task_store=self.task_store,
            agent_card=self.agent_card,
        )
        self._tmpl_attrs["request_handler"] = self.request_handler
        # A2aAgent 2.1.0 only rewrites the primary interface. Both versions are
        # actually served at this same managed endpoint, on different paths.
        url = self.agent_card.supported_interfaces[0].url
        for interface in self.agent_card.supported_interfaces:
            interface.url = url

        async def serve_gemini_card(request):
            return JSONResponse(gemini_agent_card(self.agent_card))

        # Native discovery remains /a2a/card; 0.3 discovery is /a2a/v1/card.
        # Agent Runtime's IAM authenticates both before forwarding to us.
        self.rest_routes = [
            route for route in self.rest_routes if route.path != "/a2a/v1/card"
        ]
        self.rest_routes.insert(0, Route("/a2a/v1/card", serve_gemini_card))
        # The managed runtime mounts these routes in its own ASGI application.
        for route in self.rest_routes:
            route.app = A2uiExtensionHeaders(route.app)


# Module-level instance for container-style deployments (Terraform
# provisioning, Marketplace packages): Agent Engine's source deployment uses
# the entrypoint module/object directly, and Agent Engine delivers the
# serving environment as GOOGLE_CLOUD_PROJECT/LOCATION before import.
# Constructing here captures those values the same way the SDK packaging
# path does in deployment/deploy.py.
agent = PingAdminA2aAgent()
