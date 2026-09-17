"""Static prompt registry with reproducible hashing and rendering."""

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class PromptDefinition:
    name: str
    version: str
    system_template: str
    user_template: str
    response_schema_name: str
    sha256: str

    @classmethod
    def create(
        cls,
        *,
        name: str,
        version: str,
        system_template: str,
        user_template: str,
        response_schema_name: str,
    ) -> "PromptDefinition":
        payload = "\n---SYSTEM---\n".join((system_template, user_template))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return cls(
            name=name,
            version=version,
            system_template=system_template,
            user_template=user_template,
            response_schema_name=response_schema_name,
            sha256=digest,
        )

    def render_user(self, *, context_json: str, user_message_json: str) -> str:
        return self.user_template.format(
            context_json=context_json,
            user_message_json=user_message_json,
        )


class PromptRegistry:
    def __init__(self) -> None:
        self._definitions: dict[tuple[str, str], PromptDefinition] = {}

    def register(self, definition: PromptDefinition) -> None:
        key = (definition.name, definition.version)
        if key in self._definitions:
            raise ValueError(f"prompt {definition.name}:{definition.version} exists")
        self._definitions[key] = definition

    def get(self, name: str, version: str) -> PromptDefinition:
        try:
            return self._definitions[(name, version)]
        except KeyError as exc:
            raise KeyError(f"prompt {name}:{version} is not registered") from exc
