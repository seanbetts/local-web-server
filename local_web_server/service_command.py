"""Validation and expansion for declared service command templates."""

from pathlib import Path


_PLACEHOLDERS = ("{port}", "{release}", "{repository}")


class ServiceCommandTemplateError(ValueError):
    """A declared service command is not a supported fixed template."""


def validate_service_command_template(argv: tuple[str, ...]) -> None:
    for argument in argv:
        remainder = argument
        for placeholder in _PLACEHOLDERS:
            remainder = remainder.replace(placeholder, "")
        if "{" in remainder or "}" in remainder:
            raise ServiceCommandTemplateError(
                "service.startCommand contains an unsupported placeholder"
            )
    if not any("{port}" in argument for argument in argv):
        raise ServiceCommandTemplateError(
            "service.startCommand must contain {port}"
        )


def expand_service_command_template(
    argv: tuple[str, ...], *, port: int, release: Path, repository: Path
) -> tuple[str, ...]:
    validate_service_command_template(argv)
    values = (
        ("{port}", str(port)),
        ("{release}", str(release)),
        ("{repository}", str(repository)),
    )
    expanded = []
    for argument in argv:
        for placeholder, value in values:
            argument = argument.replace(placeholder, value)
        if "{" in argument or "}" in argument:
            raise ServiceCommandTemplateError(
                "service.startCommand contains an unsupported placeholder"
            )
        expanded.append(argument)
    return tuple(expanded)
