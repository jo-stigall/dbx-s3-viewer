"""Errors that tell the user what went wrong and what to do next.

Each error has three parts:

- `message`: one sentence that says what went wrong.
- `hint`: what the user can do next.
- `detail`: the server message, if there is one, for the user to read or report.

The CLI prints all three. The TUI shows all three in the main panel.
"""

from __future__ import annotations


class ViewerError(Exception):
    """Base class for all errors that the user must see."""

    title = "Error"

    def __init__(self, message: str, *, hint: str = "", detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.detail = detail

    def __str__(self) -> str:
        return self.message


class InvalidPathError(ViewerError):
    """The text is not a usable s3:// path."""

    title = "Invalid path"


class ConfigError(ViewerError):
    """The config file or a command-line value is wrong."""

    title = "Configuration problem"


class MissingWarehouseError(ConfigError):
    """No SQL warehouse ID is set anywhere."""

    title = "No SQL warehouse ID"


class AuthError(ViewerError):
    """Databricks did not accept the login, or no login is set up."""

    title = "Authentication failed"


class PermissionDeniedError(ViewerError):
    """The user cannot list this path or use this resource."""

    title = "Permission denied"


class PathNotFoundError(ViewerError):
    """The path does not exist."""

    title = "Path not found"


class ListTimeoutError(ViewerError):
    """The listing did not finish in time."""

    title = "Timed out"


class BackendError(ViewerError):
    """Any other error from Databricks or the network."""

    title = "Databricks error"


class UnreachableError(BackendError):
    """The workspace does not answer at all, for example because the VPN is off."""

    title = "Cannot reach Databricks"


class DownloadError(ViewerError):
    """A download cannot start or cannot finish, for example because the file exists."""

    title = "Download failed"


class DownloadCancelled(ViewerError):
    """The user cancelled a download."""

    title = "Download cancelled"


def missing_warehouse_error(config_file: object) -> MissingWarehouseError:
    """Build the error for a missing warehouse ID, with the three ways to set one."""
    return MissingWarehouseError(
        "No SQL warehouse ID is set. dbx-s3-viewer needs a SQL warehouse to run LIST.",
        hint=(
            "Pass --warehouse-id ID, or set DATABRICKS_WAREHOUSE_ID, or add "
            f'warehouse_id = "ID" to {config_file}. To find the ID, open SQL Warehouses '
            "in Databricks and select the warehouse. The ID is on the Overview tab "
            "and at the end of the HTTP path under Connection details."
        ),
    )


def auth_error(profile: str | None, detail: str = "") -> AuthError:
    """Build the error for a failed or missing login."""
    login = "databricks auth login --host https://<your-workspace-url>"
    if profile:
        login += f" --profile {profile}"
        then = f"Then start dbx-s3-viewer again with --profile {profile}."
    else:
        then = "Then start dbx-s3-viewer again. Use --profile NAME to pick a saved profile."
    where = f" for profile {profile!r}" if profile else ""
    return AuthError(
        f"Databricks did not accept your login{where}, or no login is set up.",
        hint=f"Run `{login}`. {then}",
        detail=detail,
    )
